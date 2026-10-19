"""Claiming novels for refresh and finishing a sync (spec sections 4, 6.2 and 6.3)."""

import random
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select, update

from novelhog.repo import Repo, db
from novelhog.repo.novels import NovelClaim, SyncFailed, SyncSucceeded
from novelhog.repo.schema import chapters, novels
from tests.conftest import FakeClock

URL = "https://example.com/novel"
TTL = 300
ME = "worker-a"
OTHER = "worker-b"
MIN = 21600
MAX = 2592000


def u(n: int) -> str:
    return f"{URL}/{n}"


@pytest.fixture
def novel_id(repo: Repo) -> str:
    repo.hosts.publish(["example.com"])
    return repo.novels.create(URL, followed=True).id


def novel_of(repo: Repo, novel_id: str) -> Any:
    with db.read(repo.engine) as conn:
        return conn.execute(select(novels).where(novels.c.id == novel_id)).one()


def chapter_rows(repo: Repo, novel_id: str) -> list[Any]:
    with db.read(repo.engine) as conn:
        statement = select(chapters).where(chapters.c.novel_id == novel_id)
        return list(conn.execute(statement.order_by(chapters.c.url)).all())


def success(urls: list[str], **overrides: Any) -> SyncSucceeded:
    values: dict[str, Any] = {
        "urls": urls,
        "title": "Lantern",
        "language": "en",
        "cover_key": None,
        "min_s": MIN,
        "max_s": MAX,
        "rng": random.Random(1),  # noqa: S311
    }
    return SyncSucceeded(**(values | overrides))


def claimed(repo: Repo, worker: str = ME) -> NovelClaim:
    claim = repo.novels.claim_due_refresh(worker, TTL)
    assert claim is not None
    return claim


def novel_events(repo: Repo, novel_id: str) -> list[Any]:
    return [e for e in repo.events.since(0, novel_id=novel_id) if e.entity_id == novel_id]


class TestClaimDueRefresh__OnDueNovels:
    def test_claim_due_refresh__claims_oldest_due_and_appends_syncing_event(
        self, repo: Repo, clock: FakeClock
    ) -> None:
        repo.hosts.publish(["example.com"])
        first = repo.novels.create(URL, followed=True).id
        clock.advance(seconds=10)
        repo.novels.create(f"{URL}-2", followed=False)
        clock.advance(seconds=10)

        claim = claimed(repo)

        assert claim == NovelClaim(first, "example.com", URL, None, None, True, True, MIN)
        last = novel_events(repo, first)[-1]
        assert last.payload["syncing"] is True
        assert novel_of(repo, first).claimed_by == ME

    def test_claim_due_refresh__reports_not_initial_after_a_refresh(
        self, repo: Repo, novel_id: str, clock: FakeClock
    ) -> None:
        claim = claimed(repo)
        repo.novels.finish_sync(novel_id, ME, success([u(1)]))
        clock.advance(days=40)

        assert claimed(repo).initial is False
        assert claim.initial is True


class TestClaimDueRefresh__OnNothingAvailable:
    def test_claim_due_refresh__returns_none_when_nothing_is_due(
        self, repo: Repo, novel_id: str
    ) -> None:
        claimed(repo)
        repo.novels.finish_sync(novel_id, ME, success([u(1)]))

        assert repo.novels.claim_due_refresh(ME, TTL) is None

    def test_claim_due_refresh__skips_a_live_claim(self, repo: Repo, novel_id: str) -> None:
        claimed(repo)

        assert repo.novels.claim_due_refresh(OTHER, TTL) is None

    def test_claim_due_refresh__takes_over_an_expired_claim(
        self, repo: Repo, novel_id: str, clock: FakeClock
    ) -> None:
        claimed(repo)
        clock.advance(seconds=TTL + 1)

        assert claimed(repo, OTHER).novel_id == novel_id
        assert novel_of(repo, novel_id).claimed_by == OTHER


class TestRenewClaim:
    def test_renew_claim__extends_a_live_claim(
        self, repo: Repo, novel_id: str, clock: FakeClock
    ) -> None:
        claimed(repo)
        clock.advance(seconds=TTL - 1)

        assert repo.novels.renew_claim(novel_id, ME, TTL) is True
        assert novel_of(repo, novel_id).claimed_until == clock() + timedelta(seconds=TTL)

    def test_renew_claim__returns_false_once_lost(
        self, repo: Repo, novel_id: str, clock: FakeClock
    ) -> None:
        claimed(repo)
        clock.advance(seconds=TTL + 1)

        assert repo.novels.renew_claim(novel_id, ME, TTL) is False
        assert repo.novels.renew_claim(novel_id, OTHER, TTL) is False


class TestFinishSync__Succeeded:
    def test_finish_sync__applies_toc_and_sets_novel_fields(
        self, repo: Repo, novel_id: str, clock: FakeClock
    ) -> None:
        claimed(repo)
        clock.advance(seconds=5)
        with db.write(repo.engine) as conn:
            conn.execute(update(novels).values(import_error="old", language="xx"))

        report = repo.novels.finish_sync(
            novel_id, ME, success([u(1), u(2)], cover_key="covers/x/a.png")
        )

        assert report is not None
        assert len(report.diff.inserted) == 2
        row = novel_of(repo, novel_id)
        assert (row.title, row.language, row.cover_key) == ("Lantern", "en", "covers/x/a.png")
        assert row.import_error is None
        assert row.last_refreshed_at == clock()
        assert [c.position for c in chapter_rows(repo, novel_id)] == [1, 2]

    def test_finish_sync__keeps_an_existing_title_and_cover(
        self, repo: Repo, novel_id: str
    ) -> None:
        with db.write(repo.engine) as conn:
            conn.execute(update(novels).values(title="Mine", cover_key="covers/x/old.png"))
        claimed(repo)

        repo.novels.finish_sync(novel_id, ME, success([u(1)], cover_key="covers/x/new.png"))

        row = novel_of(repo, novel_id)
        assert (row.title, row.cover_key) == ("Mine", "covers/x/new.png")

    def test_finish_sync__keeps_title_and_language_when_none_found(
        self, repo: Repo, novel_id: str
    ) -> None:
        claimed(repo)

        repo.novels.finish_sync(novel_id, ME, success([u(1)], title=None, language=None))

        row = novel_of(repo, novel_id)
        assert (row.title, row.language) == (None, None)

    def test_finish_sync__interval_resets_to_min_on_new_chapters_after_initial(
        self, repo: Repo, novel_id: str, clock: FakeClock
    ) -> None:
        claimed(repo)
        repo.novels.finish_sync(novel_id, ME, success([u(1)]))
        clock.advance(days=40)
        claimed(repo)
        with db.write(repo.engine) as conn:
            conn.execute(update(novels).values(refresh_interval=MIN * 8))

        repo.novels.finish_sync(novel_id, ME, success([u(1), u(2)]))

        assert novel_of(repo, novel_id).refresh_interval == MIN

    def test_finish_sync__interval_doubles_to_max_without_new_chapters(
        self, repo: Repo, novel_id: str, clock: FakeClock
    ) -> None:
        claimed(repo)
        repo.novels.finish_sync(novel_id, ME, success([u(1)]))
        assert novel_of(repo, novel_id).refresh_interval == MIN * 2
        clock.advance(days=40)
        claimed(repo)
        with db.write(repo.engine) as conn:
            conn.execute(update(novels).values(refresh_interval=MAX - 1))

        repo.novels.finish_sync(novel_id, ME, success([u(1)]))

        assert novel_of(repo, novel_id).refresh_interval == MAX

    def test_finish_sync__schedules_a_jittered_next_refresh(
        self, repo: Repo, novel_id: str, clock: FakeClock
    ) -> None:
        claimed(repo)

        repo.novels.finish_sync(novel_id, ME, success([u(1)]))

        row = novel_of(repo, novel_id)
        wait = (row.next_refresh_at - clock()).total_seconds()
        assert row.refresh_interval * 0.9 <= wait <= row.refresh_interval * 1.1

    def test_finish_sync__is_due_now_when_a_sync_was_requested_and_clears_the_flag(
        self, repo: Repo, novel_id: str
    ) -> None:
        claimed(repo)
        repo.novels.request_sync(novel_id)

        repo.novels.finish_sync(novel_id, ME, success([u(1)]))

        row = novel_of(repo, novel_id)
        assert row.next_refresh_at == repo.clock()
        assert row.sync_requested is False

    def test_finish_sync__releases_the_claim(self, repo: Repo, novel_id: str) -> None:
        claimed(repo)

        repo.novels.finish_sync(novel_id, ME, success([u(1)]))

        row = novel_of(repo, novel_id)
        assert (row.claimed_by, row.claimed_until) == (None, None)

    def test_finish_sync__event_has_syncing_false_and_empty_released_on_initial(
        self, repo: Repo, novel_id: str
    ) -> None:
        claimed(repo)

        repo.novels.finish_sync(novel_id, ME, success([u(1), u(2)]))

        payload = novel_events(repo, novel_id)[-1].payload
        assert payload["syncing"] is False
        assert payload["released"] == []

    def test_finish_sync__event_lists_positions_of_new_chapters(
        self, repo: Repo, novel_id: str, clock: FakeClock
    ) -> None:
        claimed(repo)
        repo.novels.finish_sync(novel_id, ME, success([u(1), u(2)]))
        clock.advance(days=40)
        claimed(repo)

        repo.novels.finish_sync(novel_id, ME, success([u(1), u(2), u(3), u(4)]))

        payload = novel_events(repo, novel_id)[-1].payload
        assert payload["released"] == [3, 4]
        assert payload["followed"] is True

    def test_finish_sync__emits_chapter_events_before_the_novel_event(
        self, repo: Repo, novel_id: str
    ) -> None:
        claimed(repo)
        before = repo.events.latest_seq()

        repo.novels.finish_sync(novel_id, ME, success([u(1), u(2)]))

        kinds = [e.kind for e in repo.events.since(before, novel_id=novel_id)]
        assert kinds == ["chapter", "chapter", "novel"]


class TestFinishSync__Failed:
    def test_finish_sync__sets_error_interval_and_next_time_and_releases(
        self, repo: Repo, novel_id: str, clock: FakeClock
    ) -> None:
        claimed(repo)
        later = clock() + timedelta(hours=12)

        report = repo.novels.finish_sync(
            novel_id, ME, SyncFailed("SiteError: boom", MIN * 2, later)
        )

        assert report is not None
        assert report.diff.changed_ids == []
        row = novel_of(repo, novel_id)
        assert row.import_error == "SiteError: boom"
        assert (row.refresh_interval, row.next_refresh_at) == (MIN * 2, later)
        assert (row.claimed_by, row.last_refreshed_at) == (None, None)

    def test_finish_sync__keeps_import_error_when_none_given(
        self, repo: Repo, novel_id: str, clock: FakeClock
    ) -> None:
        claimed(repo)
        repo.novels.finish_sync(novel_id, ME, SyncFailed("SiteError: first", MIN, clock()))
        clock.advance(seconds=1)
        claimed(repo)

        repo.novels.finish_sync(novel_id, ME, SyncFailed(None, MIN, clock()))

        assert novel_of(repo, novel_id).import_error == "SiteError: first"

    def test_finish_sync__event_has_empty_released_and_syncing_false(
        self, repo: Repo, novel_id: str, clock: FakeClock
    ) -> None:
        claimed(repo)

        repo.novels.finish_sync(novel_id, ME, SyncFailed(None, MIN, clock()))

        payload = novel_events(repo, novel_id)[-1].payload
        assert payload["released"] == []
        assert payload["syncing"] is False

    def test_finish_sync__is_due_now_when_a_sync_was_requested(
        self, repo: Repo, novel_id: str, clock: FakeClock
    ) -> None:
        claimed(repo)
        repo.novels.request_sync(novel_id)

        repo.novels.finish_sync(novel_id, ME, SyncFailed(None, MIN, clock() + timedelta(days=1)))

        assert novel_of(repo, novel_id).next_refresh_at == clock()


class TestFinishSync__OnLostClaim:
    def test_finish_sync__changes_nothing_when_the_claim_expired(
        self, repo: Repo, novel_id: str, clock: FakeClock
    ) -> None:
        claimed(repo)
        clock.advance(seconds=TTL + 1)
        seq = repo.events.latest_seq()
        before = novel_of(repo, novel_id)

        assert repo.novels.finish_sync(novel_id, ME, success([u(1)])) is None

        assert novel_of(repo, novel_id) == before
        assert chapter_rows(repo, novel_id) == []
        assert repo.events.latest_seq() == seq

    def test_finish_sync__changes_nothing_when_another_worker_took_over(
        self, repo: Repo, novel_id: str, clock: FakeClock
    ) -> None:
        claimed(repo)
        clock.advance(seconds=TTL + 1)
        claimed(repo, OTHER)

        assert repo.novels.finish_sync(novel_id, ME, success([u(1)])) is None
        assert novel_of(repo, novel_id).claimed_by == OTHER

    def test_finish_sync__returns_none_for_a_deleted_novel(self, repo: Repo, novel_id: str) -> None:
        claimed(repo)
        repo.novels.delete(novel_id)

        assert repo.novels.finish_sync(novel_id, ME, success([u(1)])) is None
