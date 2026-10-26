"""Starting and finishing a `novel_refresh` task: v1's sync claim and `finish_sync` (spec 6.2).

The refresh runs as a task (tasks spec section 5): `repo.tasks.start` reads the `NovelClaim`, and
`repo.tasks.finish` applies the sync result through the kind's handler.
"""

import random
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select, update

from novelhog.domain.tasks import Done, Failed, Outcome, TaskKind, TaskState
from novelhog.repo import Repo, db
from novelhog.repo.chapters import TocDiff
from novelhog.repo.schema import chapters, novels, tasks
from novelhog.repo.task_kinds.novel_refresh import NovelClaim, SyncFailed, SyncSucceeded
from novelhog.repo.tasks import Finished, Ready, TaskRow
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


def refresh_tasks(repo: Repo, novel_id: str) -> list[TaskRow]:
    with db.read(repo.engine) as conn:
        statement = select(tasks).where(
            tasks.c.kind == TaskKind.NOVEL_REFRESH.value, tasks.c.entity_id == novel_id
        )
        return [TaskRow.from_row(row) for row in conn.execute(statement.order_by(tasks.c.id))]


def queued_refresh(repo: Repo, novel_id: str) -> TaskRow:
    [task] = [t for t in refresh_tasks(repo, novel_id) if t.state is TaskState.QUEUED]
    return task


def queued_or_running(repo: Repo, novel_id: str) -> TaskRow:
    """The novel's only refresh task."""
    [task] = refresh_tasks(repo, novel_id)
    return task


def success(urls: list[str], **overrides: Any) -> Done[SyncSucceeded]:
    values: dict[str, Any] = {
        "urls": urls,
        "title": "Lantern",
        "language": "en",
        "cover_key": None,
        "min_s": MIN,
        "max_s": MAX,
        "rng": random.Random(1),  # noqa: S311
    }
    return Done(SyncSucceeded(**(values | overrides)))


def failure(import_error: str | None, interval: int, next_at: Any) -> Failed[SyncFailed]:
    return Failed(import_error or "SiteError: boom", SyncFailed(import_error, interval, next_at))


class Sync:
    """One started refresh: its running task and the work item the start read."""

    def __init__(self, repo: Repo, worker: str = ME) -> None:
        claimed = repo.tasks.claim(worker, TTL)
        assert claimed is not None
        started = repo.tasks.start(claimed, worker)
        assert isinstance(started, Ready)
        self.repo, self.worker = repo, worker
        self.task: TaskRow = started.task
        self.claim: NovelClaim = started.work

    def finish(self, outcome: Outcome[Any]) -> Finished | None:
        return self.repo.tasks.finish(self.task, self.worker, outcome)


def novel_events(repo: Repo, novel_id: str) -> list[Any]:
    return [e for e in repo.events.since(0, novel_id=novel_id) if e.entity_id == novel_id]


class TestNovelRefreshStart__OnDueNovels:
    def test_start__starts_the_oldest_due_refresh_and_appends_a_syncing_event(
        self, repo: Repo, clock: FakeClock
    ) -> None:
        repo.hosts.publish(["example.com"])
        first = repo.novels.create(URL, followed=True).id
        clock.advance(seconds=10)
        repo.novels.create(f"{URL}-2", followed=False)
        clock.advance(seconds=10)

        sync = Sync(repo)

        assert sync.claim == NovelClaim(
            first, "example.com", URL, None, None, True, True, MIN, True, 0
        )
        last = novel_events(repo, first)[-1]
        assert last.payload["syncing"] is True
        assert (sync.task.entity_id, sync.task.claimed_by) == (first, ME)

    def test_start__reports_not_initial_after_a_refresh(
        self, repo: Repo, novel_id: str, clock: FakeClock
    ) -> None:
        first = Sync(repo)
        first.finish(success([u(1)]))
        clock.advance(days=40)

        assert Sync(repo).claim.initial is False
        assert first.claim.initial is True

    def test_start__reads_the_known_pages_and_whether_the_host_is_supported(
        self, repo: Repo, novel_id: str, clock: FakeClock
    ) -> None:
        Sync(repo).finish(success([u(1), u(2)]))
        clock.advance(days=40)
        repo.hosts.publish(["other.com"])

        claim = Sync(repo).claim

        assert (claim.known_pages, claim.host_supported) == (2, False)


class TestNovelRefreshStart__OnNothingAvailable:
    def test_claim__returns_none_when_nothing_is_due(self, repo: Repo, novel_id: str) -> None:
        Sync(repo).finish(success([u(1)]))

        assert repo.tasks.claim(ME, TTL) is None

    def test_claim__skips_a_running_refresh(self, repo: Repo, novel_id: str) -> None:
        Sync(repo)

        assert repo.tasks.claim(OTHER, TTL) is None

    def test_claim__takes_a_lapsed_refresh_once_recovery_put_it_back(
        self, repo: Repo, novel_id: str, clock: FakeClock
    ) -> None:
        Sync(repo)
        clock.advance(seconds=TTL + 1)
        repo.tasks.recover()

        sync = Sync(repo, OTHER)

        assert (sync.claim.novel_id, sync.task.claimed_by) == (novel_id, OTHER)


class TestNovelRefreshRenew:
    def test_renew__extends_a_live_claim(self, repo: Repo, novel_id: str, clock: FakeClock) -> None:
        sync = Sync(repo)
        clock.advance(seconds=TTL - 1)

        assert repo.tasks.renew(sync.task, ME, TTL) is True
        assert queued_or_running(repo, novel_id).claimed_until == clock() + timedelta(seconds=TTL)

    def test_renew__returns_false_once_lost(
        self, repo: Repo, novel_id: str, clock: FakeClock
    ) -> None:
        sync = Sync(repo)
        clock.advance(seconds=TTL + 1)

        assert repo.tasks.renew(sync.task, ME, TTL) is False
        assert repo.tasks.renew(sync.task, OTHER, TTL) is False


class TestNovelRefreshFinish__Succeeded:
    def test_finish__applies_toc_and_sets_novel_fields(
        self, repo: Repo, novel_id: str, clock: FakeClock
    ) -> None:
        sync = Sync(repo)
        clock.advance(seconds=5)
        with db.write(repo.engine) as conn:
            conn.execute(update(novels).values(import_error="old", language="xx"))

        finished = sync.finish(success([u(1), u(2)], cover_key="covers/x/a.png"))

        assert finished is not None
        assert isinstance(finished.report, TocDiff)
        assert len(finished.report.inserted) == 2
        row = novel_of(repo, novel_id)
        assert (row.title, row.language, row.cover_key) == ("Lantern", "en", "covers/x/a.png")
        assert row.import_error is None
        assert row.last_refreshed_at == clock()
        assert [c.position for c in chapter_rows(repo, novel_id)] == [1, 2]

    def test_finish__keeps_an_existing_title_and_cover(self, repo: Repo, novel_id: str) -> None:
        with db.write(repo.engine) as conn:
            conn.execute(update(novels).values(title="Mine", cover_key="covers/x/old.png"))
        sync = Sync(repo)

        sync.finish(success([u(1)], cover_key="covers/x/new.png"))

        row = novel_of(repo, novel_id)
        assert (row.title, row.cover_key) == ("Mine", "covers/x/new.png")

    def test_finish__keeps_title_and_language_when_none_found(
        self, repo: Repo, novel_id: str
    ) -> None:
        Sync(repo).finish(success([u(1)], title=None, language=None))

        row = novel_of(repo, novel_id)
        assert (row.title, row.language) == (None, None)

    def test_finish__interval_resets_to_min_on_new_chapters_after_initial(
        self, repo: Repo, novel_id: str, clock: FakeClock
    ) -> None:
        Sync(repo).finish(success([u(1)]))
        clock.advance(days=40)
        sync = Sync(repo)
        with db.write(repo.engine) as conn:
            conn.execute(update(novels).values(refresh_interval=MIN * 8))

        sync.finish(success([u(1), u(2)]))

        assert novel_of(repo, novel_id).refresh_interval == MIN

    def test_finish__interval_doubles_to_max_without_new_chapters(
        self, repo: Repo, novel_id: str, clock: FakeClock
    ) -> None:
        Sync(repo).finish(success([u(1)]))
        assert novel_of(repo, novel_id).refresh_interval == MIN * 2
        clock.advance(days=40)
        sync = Sync(repo)
        with db.write(repo.engine) as conn:
            conn.execute(update(novels).values(refresh_interval=MAX - 1))

        sync.finish(success([u(1)]))

        assert novel_of(repo, novel_id).refresh_interval == MAX

    def test_finish__schedules_a_jittered_next_refresh(
        self, repo: Repo, novel_id: str, clock: FakeClock
    ) -> None:
        Sync(repo).finish(success([u(1)]))

        interval = novel_of(repo, novel_id).refresh_interval
        wait = (repo.novels.get(novel_id).next_refresh_at - clock()).total_seconds()
        assert interval * 0.9 <= wait <= interval * 1.1
        assert queued_refresh(repo, novel_id).run_after == repo.novels.get(novel_id).next_refresh_at

    def test_finish__is_due_at_the_request_when_a_sync_was_requested_meanwhile(
        self, repo: Repo, novel_id: str, clock: FakeClock
    ) -> None:
        sync = Sync(repo)
        clock.advance(seconds=30)
        repo.novels.request_sync(novel_id)
        requested = clock()
        clock.advance(seconds=30)

        sync.finish(success([u(1)]))

        assert repo.novels.get(novel_id).next_refresh_at == requested
        assert queued_refresh(repo, novel_id).run_after == requested

    def test_finish__ends_the_task_done_with_its_claim_cleared(
        self, repo: Repo, novel_id: str
    ) -> None:
        sync = Sync(repo)

        sync.finish(success([u(1)]))

        [done] = [t for t in refresh_tasks(repo, novel_id) if t.id == sync.task.id]
        assert (done.state, done.claimed_by, done.claimed_until) == (TaskState.DONE, None, None)

    def test_finish__event_has_syncing_false_and_empty_released_on_initial(
        self, repo: Repo, novel_id: str
    ) -> None:
        Sync(repo).finish(success([u(1), u(2)]))

        payload = novel_events(repo, novel_id)[-1].payload
        assert payload["syncing"] is False
        assert payload["released"] == []

    def test_finish__event_lists_positions_of_new_chapters(
        self, repo: Repo, novel_id: str, clock: FakeClock
    ) -> None:
        Sync(repo).finish(success([u(1), u(2)]))
        clock.advance(days=40)

        Sync(repo).finish(success([u(1), u(2), u(3), u(4)]))

        payload = novel_events(repo, novel_id)[-1].payload
        assert payload["released"] == [3, 4]
        assert payload["followed"] is True

    def test_finish__emits_chapter_events_before_the_novel_event(
        self, repo: Repo, novel_id: str
    ) -> None:
        sync = Sync(repo)
        before = repo.events.latest_seq()

        sync.finish(success([u(1), u(2)]))

        kinds = [e.kind for e in repo.events.since(before, novel_id=novel_id)]
        assert kinds == ["chapter", "chapter", "novel", "task"]


class TestNovelRefreshFinish__Failed:
    def test_finish__sets_error_interval_and_next_time(
        self, repo: Repo, novel_id: str, clock: FakeClock
    ) -> None:
        sync = Sync(repo)
        later = clock() + timedelta(hours=12)

        finished = sync.finish(failure("SiteError: boom", MIN * 2, later))

        assert finished is not None
        assert isinstance(finished.report, TocDiff)
        assert finished.report.changed_ids == []
        row = novel_of(repo, novel_id)
        assert row.import_error == "SiteError: boom"
        assert (row.refresh_interval, repo.novels.get(novel_id).next_refresh_at) == (
            MIN * 2,
            later,
        )
        assert row.last_refreshed_at is None
        [failed] = [t for t in refresh_tasks(repo, novel_id) if t.id == sync.task.id]
        assert failed.state is TaskState.FAILED

    def test_finish__keeps_import_error_when_none_given(
        self, repo: Repo, novel_id: str, clock: FakeClock
    ) -> None:
        Sync(repo).finish(failure("SiteError: first", MIN, clock()))
        clock.advance(seconds=1)

        Sync(repo).finish(failure(None, MIN, clock()))

        assert novel_of(repo, novel_id).import_error == "SiteError: first"

    def test_finish__event_has_empty_released_and_syncing_false(
        self, repo: Repo, novel_id: str, clock: FakeClock
    ) -> None:
        Sync(repo).finish(failure(None, MIN, clock()))

        payload = novel_events(repo, novel_id)[-1].payload
        assert payload["released"] == []
        assert payload["syncing"] is False

    def test_finish__is_due_at_the_request_when_a_sync_was_requested_meanwhile(
        self, repo: Repo, novel_id: str, clock: FakeClock
    ) -> None:
        sync = Sync(repo)
        repo.novels.request_sync(novel_id)

        sync.finish(failure(None, MIN, clock() + timedelta(days=1)))

        assert repo.novels.get(novel_id).next_refresh_at == clock()


class TestNovelRefreshFinish__OnLostClaim:
    def test_finish__changes_nothing_when_the_claim_expired(
        self, repo: Repo, novel_id: str, clock: FakeClock
    ) -> None:
        sync = Sync(repo)
        clock.advance(seconds=TTL + 1)
        seq = repo.events.latest_seq()
        before = novel_of(repo, novel_id)

        assert sync.finish(success([u(1)])) is None

        assert novel_of(repo, novel_id) == before
        assert chapter_rows(repo, novel_id) == []
        assert repo.events.latest_seq() == seq

    def test_finish__changes_nothing_when_another_worker_took_over(
        self, repo: Repo, novel_id: str, clock: FakeClock
    ) -> None:
        sync = Sync(repo)
        clock.advance(seconds=TTL + 1)
        repo.tasks.recover()
        Sync(repo, OTHER)

        assert sync.finish(success([u(1)])) is None
        assert queued_or_running(repo, novel_id).claimed_by == OTHER

    def test_finish__returns_none_for_a_deleted_novel(self, repo: Repo, novel_id: str) -> None:
        sync = Sync(repo)
        repo.novels.delete(novel_id)

        assert sync.finish(success([u(1)])) is None
