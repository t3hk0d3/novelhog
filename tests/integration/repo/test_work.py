"""Download work: claims, the pipeline's writes and recovery (spec sections 4, 6.3 and 6.4)."""

import threading
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import insert, select, update

from novelhog.domain.chapters import ChapterState
from novelhog.domain.errors import NotFound
from novelhog.repo import Repo, db
from novelhog.repo.schema import chapters, events, novels
from novelhog.repo.work import ChapterClaim
from tests.conftest import FakeClock
from tests.integration.helpers import chapter_row

URL = "https://example.com/novel"
TTL = 300
ME = "worker-a"
OTHER = "worker-b"


@pytest.fixture
def novel_id(repo: Repo) -> str:
    repo.hosts.publish(["example.com"])
    return repo.novels.create(URL, followed=True).id


def cid(position: int, novel: int = 0) -> str:
    return f"CH{novel:04d}{position:08d}"


def add(repo: Repo, novel_id: str, at: int, **overrides: Any) -> str:
    """Insert one chapter; chapters are `pending` with `queued_at` unless overridden."""
    values: dict[str, Any] = {
        "state": "pending",
        "queued_at": repo.clock(),
    } | overrides
    row = chapter_row(id=cid(at), novel_id=novel_id, url=f"{URL}/{at}", position=at)
    with db.write(repo.engine) as conn:
        conn.execute(insert(chapters).values(**(row | values)))
    return str((row | values)["id"])


def row_of(repo: Repo, chapter_id: str) -> Any:
    with db.read(repo.engine) as conn:
        return conn.execute(select(chapters).where(chapters.c.id == chapter_id)).one()


def event_count(repo: Repo) -> int:
    return repo.events.latest_seq()


def chapter_events(repo: Repo, chapter_id: str) -> list[Any]:
    with db.read(repo.engine) as conn:
        return list(
            conn.execute(
                select(events).where(events.c.entity_id == chapter_id).order_by(events.c.seq)
            ).all()
        )


def claim_of(repo: Repo, novel_id: str, position: int, worker: str = ME) -> ChapterClaim:
    return ChapterClaim(
        chapter_id=cid(position),
        novel_id=novel_id,
        host="example.com",
        toc_url=URL,
        position=position,
        url=f"{URL}/{position}",
    )


def started(repo: Repo, novel_id: str, position: int = 1, worker: str = ME) -> str:
    """A chapter claimed and started by `worker`."""
    chapter_id = add(repo, novel_id, position)
    (claim,) = repo.work.claim_batch(worker, 1, TTL)
    assert repo.work.start(claim, worker)
    return chapter_id


class TestClaimBatch:
    def test_claim_batch__returns_pending_chapters_in_priority_queued_at_position_order(
        self, repo: Repo, novel_id: str, clock: FakeClock
    ) -> None:
        t0 = clock.current
        add(repo, novel_id, 1, queued_at=t0 + timedelta(seconds=2))
        add(repo, novel_id, 2, queued_at=t0 + timedelta(seconds=1))
        add(repo, novel_id, 3, queued_at=t0 + timedelta(seconds=1))
        add(repo, novel_id, 4, queued_at=t0 + timedelta(seconds=9), priority=True)
        claims = repo.work.claim_batch(ME, 10, TTL)
        assert [c.position for c in claims] == [4, 2, 3, 1]

    def test_claim_batch__claims_at_most_size_chapters(self, repo: Repo, novel_id: str) -> None:
        for position in (1, 2, 3):
            add(repo, novel_id, position)
        assert [c.position for c in repo.work.claim_batch(ME, 2, TTL)] == [1, 2]

    def test_claim_batch__returns_what_the_worker_needs_to_download(
        self, repo: Repo, novel_id: str
    ) -> None:
        add(repo, novel_id, 1)
        assert repo.work.claim_batch(ME, 1, TTL) == [
            ChapterClaim(cid(1), novel_id, "example.com", URL, 1, f"{URL}/1")
        ]

    def test_claim_batch__sets_the_claim_and_leaves_chapters_pending(
        self, repo: Repo, novel_id: str, clock: FakeClock
    ) -> None:
        chapter_id = add(repo, novel_id, 1)
        repo.work.claim_batch(ME, 1, TTL)
        row = row_of(repo, chapter_id)
        assert (row.state, row.claimed_by) == ("pending", ME)
        assert row.claimed_until == clock.current + timedelta(seconds=TTL)

    def test_claim_batch__appends_no_events(self, repo: Repo, novel_id: str) -> None:
        add(repo, novel_id, 1)
        before = event_count(repo)
        repo.work.claim_batch(ME, 1, TTL)
        assert event_count(repo) == before

    def test_claim_batch__auto_queued_release_comes_before_older_bulk_queued_chapters(
        self, repo: Repo, novel_id: str, clock: FakeClock
    ) -> None:
        add(repo, novel_id, 1, queued_at=clock.current - timedelta(days=1))
        add(repo, novel_id, 2, priority=True)
        assert [c.position for c in repo.work.claim_batch(ME, 2, TTL)] == [2, 1]

    def test_claim_batch__never_claims_removed_chapters(
        self, repo: Repo, novel_id: str, clock: FakeClock
    ) -> None:
        add(repo, novel_id, 1)
        add(repo, novel_id, 2, position=None, removed_at=clock.current)
        assert [c.chapter_id for c in repo.work.claim_batch(ME, 5, TTL)] == [cid(1)]

    def test_claim_batch__skips_chapters_under_a_live_claim_but_takes_expired_ones(
        self, repo: Repo, novel_id: str, clock: FakeClock
    ) -> None:
        add(repo, novel_id, 1)
        repo.work.claim_batch(OTHER, 1, TTL)
        assert repo.work.claim_batch(ME, 1, TTL) == []
        clock.advance(seconds=TTL + 1)
        assert [c.position for c in repo.work.claim_batch(ME, 1, TTL)] == [1]

    def test_claim_batch__nothing_pending_returns_empty_list(
        self, repo: Repo, novel_id: str
    ) -> None:
        add(repo, novel_id, 1, state="not_requested", queued_at=None)
        assert repo.work.claim_batch(ME, 5, TTL) == []

    def test_claim_batch__two_workers_never_claim_the_same_chapter(
        self, repo: Repo, novel_id: str, migrated_database: str, clock: FakeClock
    ) -> None:
        for position in range(1, 21):
            add(repo, novel_id, position)
        other = Repo(migrated_database, clock=clock)
        results: dict[str, list[ChapterClaim]] = {}
        barrier = threading.Barrier(2)

        def run(name: str, target: Repo) -> None:
            barrier.wait()
            results[name] = target.work.claim_batch(name, 15, TTL)

        threads = [
            threading.Thread(target=run, args=(ME, repo)),
            threading.Thread(target=run, args=(OTHER, other)),
        ]
        try:
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        finally:
            other.engine.dispose()
        mine = {c.chapter_id for c in results[ME]}
        theirs = {c.chapter_id for c in results[OTHER]}
        assert not mine & theirs
        assert len(mine) + len(theirs) == 20


class TestStart:
    def test_start__owned_pending_chapter_becomes_downloading_with_events(
        self, repo: Repo, novel_id: str
    ) -> None:
        add(repo, novel_id, 1)
        (claim,) = repo.work.claim_batch(ME, 1, TTL)
        before = event_count(repo)
        assert repo.work.start(claim, ME) is True
        assert row_of(repo, cid(1)).state == "downloading"
        assert event_count(repo) == before + 2  # CH + NV
        assert chapter_events(repo, cid(1))[-1].payload["state"] == "downloading"

    def test_start__dequeued_after_the_claim_returns_false_and_changes_nothing(
        self, repo: Repo, novel_id: str
    ) -> None:
        add(repo, novel_id, 1)
        (claim,) = repo.work.claim_batch(ME, 1, TTL)
        repo.chapters.patch(novel_id, ChapterState.NOT_REQUESTED, [1])
        before = event_count(repo)
        assert repo.work.start(claim, ME) is False
        assert row_of(repo, cid(1)).state == "not_requested"
        assert event_count(repo) == before

    def test_start__claim_taken_by_another_worker_returns_false_and_changes_nothing(
        self, repo: Repo, novel_id: str, clock: FakeClock
    ) -> None:
        add(repo, novel_id, 1)
        (claim,) = repo.work.claim_batch(ME, 1, TTL)
        clock.advance(seconds=TTL + 1)
        repo.work.claim_batch(OTHER, 1, TTL)
        before = event_count(repo)
        assert repo.work.start(claim, ME) is False
        row = row_of(repo, cid(1))
        assert (row.state, row.claimed_by) == ("pending", OTHER)
        assert event_count(repo) == before

    def test_start__removed_chapter_returns_false(
        self, repo: Repo, novel_id: str, clock: FakeClock
    ) -> None:
        add(repo, novel_id, 1)
        (claim,) = repo.work.claim_batch(ME, 1, TTL)
        with db.write(repo.engine) as conn:
            conn.execute(update(chapters).values(position=None, removed_at=clock.current))
        assert repo.work.start(claim, ME) is False
        assert row_of(repo, cid(1)).state == "pending"

    def test_start__deleted_novel_returns_false(self, repo: Repo, novel_id: str) -> None:
        add(repo, novel_id, 1)
        (claim,) = repo.work.claim_batch(ME, 1, TTL)
        repo.novels.delete(novel_id)
        assert repo.work.start(claim, ME) is False


class TestComplete:
    def test_complete__stores_content_and_marks_the_chapter_downloaded(
        self, repo: Repo, novel_id: str
    ) -> None:
        chapter_id = started(repo, novel_id)
        applied = repo.work.complete(chapter_id, ME, markdown="# Hi", title="One", has_images=True)
        assert applied is True
        row = row_of(repo, chapter_id)
        assert (row.state, row.title, row.has_images) == ("downloaded", "One", True)
        assert (row.queued_at, row.priority, row.claimed_by, row.claimed_until) == (
            None,
            False,
            None,
            None,
        )
        assert repo.contents.get(chapter_id) == "# Hi"

    def test_complete__appends_events_whose_counters_match_the_novel(
        self, repo: Repo, novel_id: str
    ) -> None:
        chapter_id = started(repo, novel_id)
        repo.work.complete(chapter_id, ME, markdown="x", title="One", has_images=False)
        with db.read(repo.engine) as conn:
            newest = conn.execute(select(events).order_by(events.c.seq.desc())).first()
            ch = conn.execute(
                select(events).where(events.c.entity_id == chapter_id).order_by(events.c.seq.desc())
            ).first()
        assert ch is not None
        assert ch.payload["state"] == "downloaded"
        assert newest is not None
        view = repo.novels.get(novel_id)
        assert newest.entity_id == novel_id
        assert newest.payload["downloaded_pages"] == view.downloaded_pages == 1
        assert newest.payload["pending_pages"] == view.pending_pages == 0

    def test_complete__replaces_earlier_content_on_a_re_download(
        self, repo: Repo, novel_id: str
    ) -> None:
        chapter_id = started(repo, novel_id)
        repo.work.complete(chapter_id, ME, markdown="old", title="One", has_images=False)
        repo.chapters.patch(novel_id, ChapterState.PENDING, [1])
        (claim,) = repo.work.claim_batch(ME, 1, TTL)
        assert repo.work.start(claim, ME)
        repo.work.complete(chapter_id, ME, markdown="new", title="One", has_images=False)
        assert repo.contents.get(chapter_id) == "new"

    def test_complete__claim_expired_and_recovered_stores_nothing(
        self, repo: Repo, novel_id: str, clock: FakeClock
    ) -> None:
        chapter_id = started(repo, novel_id)
        clock.advance(seconds=TTL + 1)
        repo.work.recover()
        before = event_count(repo)
        assert not repo.work.complete(chapter_id, ME, markdown="x", title="t", has_images=False)
        assert row_of(repo, chapter_id).state == "pending"
        assert event_count(repo) == before
        with pytest.raises(NotFound):
            repo.contents.get(chapter_id)

    def test_complete__claim_expired_without_recovery_returns_false(
        self, repo: Repo, novel_id: str, clock: FakeClock
    ) -> None:
        chapter_id = started(repo, novel_id)
        clock.advance(seconds=TTL + 1)
        assert not repo.work.complete(chapter_id, ME, markdown="x", title="t", has_images=False)
        assert row_of(repo, chapter_id).state == "downloading"

    def test_complete__reclaimed_by_another_worker_stores_nothing(
        self, repo: Repo, novel_id: str, clock: FakeClock
    ) -> None:
        chapter_id = started(repo, novel_id)
        clock.advance(seconds=TTL + 1)
        repo.work.recover()
        repo.work.claim_batch(OTHER, 1, TTL)
        assert not repo.work.complete(chapter_id, ME, markdown="x", title="t", has_images=False)
        assert row_of(repo, chapter_id).claimed_by == OTHER

    def test_complete__deleted_novel_stores_nothing(self, repo: Repo, novel_id: str) -> None:
        chapter_id = started(repo, novel_id)
        repo.novels.delete(novel_id)
        assert not repo.work.complete(chapter_id, ME, markdown="x", title="t", has_images=False)
        with pytest.raises(NotFound):
            repo.contents.get(chapter_id)

    def test_complete__unknown_chapter_returns_false(self, repo: Repo) -> None:
        assert not repo.work.complete(
            "CH000000000099", ME, markdown="x", title="t", has_images=False
        )


class TestFail:
    def test_fail__sets_error_clears_queue_and_claim_and_returns_true(
        self, repo: Repo, novel_id: str
    ) -> None:
        chapter_id = started(repo, novel_id)
        assert repo.work.fail(chapter_id, ME, "ValueError: boom") is True
        row = row_of(repo, chapter_id)
        assert (row.state, row.queued_at, row.priority, row.claimed_by) == (
            "error",
            None,
            False,
            None,
        )

    def test_fail__ch_event_carries_error_and_error_at(
        self, repo: Repo, novel_id: str, clock: FakeClock
    ) -> None:
        chapter_id = started(repo, novel_id)
        repo.work.fail(chapter_id, ME, "ValueError: boom")
        payload = chapter_events(repo, chapter_id)[-1].payload
        assert payload["state"] == "error"
        assert payload["error"] == "ValueError: boom"
        assert payload["error_at"] is not None
        assert repo.chapters.get(chapter_id).error == "ValueError: boom"

    def test_fail__lost_claim_returns_false_and_changes_nothing(
        self, repo: Repo, novel_id: str, clock: FakeClock
    ) -> None:
        chapter_id = started(repo, novel_id)
        clock.advance(seconds=TTL + 1)
        repo.work.recover()
        before = event_count(repo)
        assert repo.work.fail(chapter_id, ME, "late") is False
        assert row_of(repo, chapter_id).state == "pending"
        assert event_count(repo) == before


class TestRequeue:
    def test_requeue__moves_downloading_chapter_back_to_pending_keeping_queued_at(
        self, repo: Repo, novel_id: str, clock: FakeClock
    ) -> None:
        chapter_id = add(repo, novel_id, 1, queued_at=clock.current - timedelta(hours=1))
        (claim,) = repo.work.claim_batch(ME, 1, TTL)
        repo.work.start(claim, ME)
        assert repo.work.requeue(chapter_id, ME) is True
        row = row_of(repo, chapter_id)
        assert (row.state, row.claimed_by, row.claimed_until) == ("pending", None, None)
        assert row.queued_at == clock.current - timedelta(hours=1)
        assert chapter_events(repo, chapter_id)[-1].payload["state"] == "pending"

    def test_requeue__of_a_removed_chapter_returns_it_to_not_requested(
        self, repo: Repo, novel_id: str, clock: FakeClock
    ) -> None:
        chapter_id = started(repo, novel_id)
        with db.write(repo.engine) as conn:
            conn.execute(update(chapters).values(position=None, removed_at=clock.current))
        assert repo.work.requeue(chapter_id, ME) is True
        row = row_of(repo, chapter_id)
        assert (row.state, row.queued_at, row.claimed_by) == ("not_requested", None, None)

    def test_requeue__not_downloading_or_not_mine_returns_false(
        self, repo: Repo, novel_id: str
    ) -> None:
        chapter_id = started(repo, novel_id)
        assert repo.work.requeue(chapter_id, OTHER) is False
        assert row_of(repo, chapter_id).state == "downloading"


class TestReleaseChapters:
    def test_release_chapters__frees_unstarted_claims_and_keeps_them_pending(
        self, repo: Repo, novel_id: str, clock: FakeClock
    ) -> None:
        for position in (1, 2):
            add(repo, novel_id, position)
        repo.work.claim_batch(ME, 2, TTL)
        assert repo.work.release_chapters([cid(1), cid(2)], ME) == 2
        for position in (1, 2):
            row = row_of(repo, cid(position))
            assert (row.state, row.claimed_by, row.queued_at) == ("pending", None, clock.current)

    def test_release_chapters__ignores_chapters_owned_by_others(
        self, repo: Repo, novel_id: str
    ) -> None:
        add(repo, novel_id, 1)
        repo.work.claim_batch(OTHER, 1, TTL)
        assert repo.work.release_chapters([cid(1)], ME) == 0
        assert row_of(repo, cid(1)).claimed_by == OTHER

    def test_release_chapters__leaves_a_started_chapter_alone(
        self, repo: Repo, novel_id: str
    ) -> None:
        chapter_id = started(repo, novel_id)
        assert repo.work.release_chapters([chapter_id], ME) == 0
        assert row_of(repo, chapter_id).state == "downloading"


class TestRenewChapters:
    def test_renew_chapters__pushes_claimed_until_forward_and_returns_ids(
        self, repo: Repo, novel_id: str, clock: FakeClock
    ) -> None:
        for position in (1, 2):
            add(repo, novel_id, position)
        repo.work.claim_batch(ME, 2, TTL)
        clock.advance(seconds=100)
        assert repo.work.renew_chapters([cid(1), cid(2)], ME, TTL) == [cid(1), cid(2)]
        assert row_of(repo, cid(1)).claimed_until == clock.current + timedelta(seconds=TTL)

    def test_renew_chapters__omits_dequeued_and_taken_over_chapters(
        self, repo: Repo, novel_id: str, clock: FakeClock
    ) -> None:
        for position in (1, 2, 3):
            add(repo, novel_id, position)
        repo.work.claim_batch(ME, 3, TTL)
        repo.chapters.patch(novel_id, ChapterState.NOT_REQUESTED, [1])
        with db.write(repo.engine) as conn:
            conn.execute(update(chapters).where(chapters.c.id == cid(2)).values(claimed_by=OTHER))
        assert repo.work.renew_chapters([cid(1), cid(2), cid(3)], ME, TTL) == [cid(3)]

    def test_renew_chapters__empty_input_returns_empty_list(self, repo: Repo) -> None:
        assert repo.work.renew_chapters([], ME, TTL) == []


class TestRecover:
    def test_recover__expired_downloading_chapters_go_back_to_pending_with_events(
        self, repo: Repo, novel_id: str, clock: FakeClock
    ) -> None:
        queued = clock.current
        chapter_id = started(repo, novel_id)
        clock.advance(seconds=TTL + 1)
        before = event_count(repo)
        report = repo.work.recover()
        row = row_of(repo, chapter_id)
        assert (row.state, row.claimed_by, row.claimed_until) == ("pending", None, None)
        assert row.queued_at == queued
        assert (report.chapters_requeued, report.chapters_freed) == (1, 0)
        assert event_count(repo) == before + 2
        assert chapter_events(repo, chapter_id)[-1].payload["state"] == "pending"

    def test_recover__expired_pending_claims_are_freed_without_events(
        self, repo: Repo, novel_id: str, clock: FakeClock
    ) -> None:
        add(repo, novel_id, 1)
        repo.work.claim_batch(ME, 1, TTL)
        clock.advance(seconds=TTL + 1)
        before = event_count(repo)
        report = repo.work.recover()
        row = row_of(repo, cid(1))
        assert (row.state, row.claimed_by, row.claimed_until) == ("pending", None, None)
        assert (report.chapters_requeued, report.chapters_freed) == (0, 1)
        assert event_count(repo) == before

    def test_recover__removed_downloading_chapter_returns_to_not_requested(
        self, repo: Repo, novel_id: str, clock: FakeClock
    ) -> None:
        chapter_id = started(repo, novel_id)
        with db.write(repo.engine) as conn:
            conn.execute(update(chapters).values(position=None, removed_at=clock.current))
        clock.advance(seconds=TTL + 1)
        report = repo.work.recover()
        row = row_of(repo, chapter_id)
        assert (row.state, row.queued_at, row.claimed_by, row.position) == (
            "not_requested",
            None,
            None,
            None,
        )
        assert (report.chapters_requeued, report.chapters_freed) == (0, 1)
        assert repo.work.claim_batch(ME, 5, TTL) == []

    def test_recover__recovers_chapters_of_several_novels(
        self, repo: Repo, novel_id: str, clock: FakeClock
    ) -> None:
        second = "NV000000000002"
        with db.write(repo.engine) as conn:
            from tests.integration.helpers import novel_row

            conn.execute(
                insert(novels).values(**novel_row(id=second, toc_url="https://example.com/other"))
            )
        one = started(repo, novel_id, 1)
        two = add(repo, second, 2, id="CH000000000222", url="https://example.com/other/2")
        (claim,) = repo.work.claim_batch(ME, 1, TTL)
        assert repo.work.start(claim, ME)
        clock.advance(seconds=TTL + 1)
        assert repo.work.recover().chapters_requeued == 2
        assert {row_of(repo, one).state, row_of(repo, two).state} == {"pending"}

    def test_recover__expired_sync_claim_frees_the_novel_and_appends_nv_event(
        self, repo: Repo, novel_id: str, clock: FakeClock
    ) -> None:
        with db.write(repo.engine) as conn:
            conn.execute(
                update(novels).values(
                    claimed_by=ME, claimed_until=clock.current + timedelta(seconds=TTL)
                )
            )
        assert repo.novels.get(novel_id).syncing is True
        clock.advance(seconds=TTL + 1)
        report = repo.work.recover()
        assert report.novels_freed == 1
        with db.read(repo.engine) as conn:
            row = conn.execute(select(novels)).one()
            newest = conn.execute(select(events).order_by(events.c.seq.desc())).first()
        assert (row.claimed_by, row.claimed_until) == (None, None)
        assert newest is not None
        assert newest.entity_id == novel_id
        assert newest.payload["syncing"] is False

    def test_recover__live_claims_are_left_alone(
        self, repo: Repo, novel_id: str, clock: FakeClock
    ) -> None:
        started(repo, novel_id, 1)
        add(repo, novel_id, 2)
        repo.work.claim_batch(ME, 1, TTL)
        with db.write(repo.engine) as conn:
            conn.execute(
                update(novels).values(
                    claimed_by=ME, claimed_until=clock.current + timedelta(seconds=TTL)
                )
            )
        before = event_count(repo)
        report = repo.work.recover()
        assert (
            report.chapters_requeued,
            report.chapters_freed,
            report.novels_freed,
            report.exports_reset,
            report.consumers_freed,
        ) == (0, 0, 0, 0, 0)
        assert event_count(repo) == before
        assert row_of(repo, cid(1)).state == "downloading"
        assert row_of(repo, cid(2)).claimed_by == ME
