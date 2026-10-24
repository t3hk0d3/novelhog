"""Task kinds through the runner: each kind's repo handler and worker processor (tasks spec 5).

`novel_refresh`: v1's refresh (spec section 6.2) as a task, with the derived `syncing` and
`next_refresh_at` (tasks spec sections 2 and 3) and the successor and manual-sync rules (5.2).
"""

import random
import threading
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from structlog.testing import capture_logs

from novelhog.chapter_store import ChapterStore
from novelhog.config import Settings
from novelhog.domain.tasks import Skip, Started, TaskKind, TaskState
from novelhog.repo import Repo, db
from novelhog.repo.schema import chapters, novels, tasks
from novelhog.repo.task_kinds.novel_refresh import NovelRefreshHandler
from novelhog.repo.tasks import Finished, TaskRow
from novelhog.scraper_adapter.adapter import TocResult
from novelhog.scraper_adapter.errors import InfrastructureError, SiteError
from novelhog.storage import LocalStaticStorage
from novelhog.worker import runtime
from novelhog.worker.reachability import Reachability
from novelhog.worker.runner import Runner, RunnerBackoff
from novelhog.worker.tasks import default_processors
from novelhog.worker.tasks.base import TaskContext
from tests.conftest import FakeClock
from tests.integration.helpers import assert_no_events_after_delete, trash_keys
from tests.integration.worker.conftest import URL, FakeAdapter, FakeFetcher

MIN = 21600
COVER_URL = "https://example.com/cover.png"
REFRESH = TaskKind.NOVEL_REFRESH


class TocAdapter(FakeAdapter):
    """Serves a TOC with a cover; `during_toc` runs inside `sync_toc` (the sync is running)."""

    def __init__(self) -> None:
        super().__init__()
        self.urls = [f"{URL}/1", f"{URL}/2"]
        self.syncs = 0
        self.during_toc: Any = None

    def sync_toc(self, novel_id: str, host: str, toc_url: str) -> TocResult:
        self.syncs += 1
        if self.during_toc is not None:
            self.during_toc()
        if self.toc_error is not None:
            raise self.toc_error
        return TocResult("Lantern", "en", COVER_URL, list(self.urls), "<html></html>")


@pytest.fixture
def adapter() -> TocAdapter:
    return TocAdapter()


@pytest.fixture
def files(tmp_path: Path) -> LocalStaticStorage:
    return LocalStaticStorage(tmp_path / "static", "/static")


@pytest.fixture
def runner(
    repo: Repo,
    adapter: TocAdapter,
    fetcher: FakeFetcher,
    files: LocalStaticStorage,
    settings: Settings,
) -> Runner:
    """The runner with the default processors, as the worker builds it."""
    return Runner(
        repo,
        default_processors(),
        TaskContext(
            adapter=adapter,
            fetcher=fetcher,
            storage=files,
            chapter_store=ChapterStore(repo),
            settings=settings,
            stop=threading.Event(),
            reachability=Reachability(),
            rng=random.Random(3),  # noqa: S311 - refresh jitter, not security
            heartbeat=runtime.Heartbeat,
            clock=repo.clock,
        ),
    )


def refresh_tasks(repo: Repo, novel_id: str) -> list[TaskRow]:
    with db.read(repo.engine) as conn:
        statement = select(tasks).where(
            tasks.c.kind == REFRESH.value, tasks.c.entity_id == novel_id
        )
        return [TaskRow.from_row(row) for row in conn.execute(statement.order_by(tasks.c.id))]


def live(repo: Repo, novel_id: str, state: TaskState) -> list[TaskRow]:
    return [task for task in refresh_tasks(repo, novel_id) if task.state is state]


def nv_events(repo: Repo, novel_id: str, after: int = 0) -> list[dict[str, Any]]:
    return [
        e.payload for e in repo.events.since(after, novel_id=novel_id) if e.entity_id == novel_id
    ]


def novel_row(repo: Repo, novel_id: str) -> Any:
    with db.read(repo.engine) as conn:
        return conn.execute(select(novels).where(novels.c.id == novel_id)).one()


def stamp(value: datetime) -> str:
    """A timestamp as the wire shows it."""
    return value.strftime("%Y-%m-%dT%H:%M:%S.") + f"{value.microsecond // 1000:03d}Z"


def import_novel(runner: Runner, clock: FakeClock) -> None:
    """Run the first refresh, then move past the successor's due time."""
    assert runner.step() is True
    clock.advance(days=40)


def run_until_idle(runner: Runner) -> int:
    """Run tasks until none is claimable; return how many ran."""
    ran = 0
    while runner.step():
        ran += 1
    return ran


class TestNovelRefresh__OnStart:
    def test_start__appends_an_nv_event_with_syncing_true(
        self, repo: Repo, novel_id: str, runner: Runner, adapter: TocAdapter
    ) -> None:
        seen: list[dict[str, Any]] = []
        adapter.during_toc = lambda: seen.append(nv_events(repo, novel_id)[-1])

        runner.step()

        assert seen[0]["syncing"] is True

    def test_begin__starts_while_the_novel_exists(self, repo: Repo, novel_id: str) -> None:
        handler = NovelRefreshHandler(repo.context)
        [task] = refresh_tasks(repo, novel_id)

        with db.write(repo.engine) as conn:
            assert handler.begin(conn, task, repo.clock()) == Started()

    def test_begin__skips_with_novel_gone_when_the_novel_row_is_missing(
        self, repo: Repo, novel_id: str
    ) -> None:
        handler = NovelRefreshHandler(repo.context)
        [task] = refresh_tasks(repo, novel_id)
        orphan = replace(task, entity_id="NV000000000099")

        with db.write(repo.engine) as conn:
            assert handler.begin(conn, orphan, repo.clock()) == Skip("novel gone")


class TestNovelRefresh__OnDoneOrFailed:
    def test_finish__applies_the_toc_diff_title_cover_and_interval(
        self, repo: Repo, novel_id: str, runner: Runner, clock: FakeClock
    ) -> None:
        runner.step()

        row = novel_row(repo, novel_id)
        assert (row.title, row.language, row.import_error) == ("Lantern", "en", None)
        assert row.cover_key.startswith(f"covers/{novel_id}/")
        assert (row.refresh_interval, row.last_refreshed_at) == (MIN * 2, clock())
        with db.read(repo.engine) as conn:
            positions = conn.execute(
                select(chapters.c.position).where(chapters.c.novel_id == novel_id)
            ).scalars()
            assert sorted(positions) == [1, 2]

    def test_finish__applies_import_error_and_backed_off_interval_on_a_site_error(
        self, repo: Repo, novel_id: str, runner: Runner, adapter: TocAdapter
    ) -> None:
        adapter.toc_error = SiteError("HTTP 404")

        runner.step()

        row = novel_row(repo, novel_id)
        assert (row.import_error, row.refresh_interval) == ("SiteError: HTTP 404", MIN * 2)
        states = sorted(task.state for task in refresh_tasks(repo, novel_id))
        assert states == [TaskState.FAILED, TaskState.QUEUED]

    @pytest.mark.parametrize("error", [None, SiteError("HTTP 404")])
    def test_finish__enqueues_the_successor_at_the_next_refresh_time(
        self, repo: Repo, novel_id: str, runner: Runner, adapter: TocAdapter, clock: FakeClock,
        error: Exception | None,
    ) -> None:  # fmt: skip
        adapter.toc_error = error

        runner.step()

        [successor] = live(repo, novel_id, TaskState.QUEUED)
        interval = novel_row(repo, novel_id).refresh_interval
        wait = (successor.run_after - clock()).total_seconds()
        assert interval * 0.9 <= wait <= interval * 1.1
        assert (successor.priority, successor.emit_event) == (300, False)

    @pytest.mark.parametrize("error", [None, SiteError("HTTP 404")])
    def test_finish__nv_event_shows_the_successor_due_time_and_syncing_false(
        self, repo: Repo, novel_id: str, runner: Runner, adapter: TocAdapter,
        error: Exception | None,
    ) -> None:  # fmt: skip
        adapter.toc_error = error

        runner.step()

        [successor] = live(repo, novel_id, TaskState.QUEUED)
        event = nv_events(repo, novel_id)[-1]
        assert (event["next_refresh_at"], event["syncing"]) == (stamp(successor.run_after), False)


class TestNovelRefresh__WhenAManualSyncWasQueuedDuringTheRun:
    def test_finish__leaves_the_queued_task_due_now_without_a_second_successor(
        self, repo: Repo, novel_id: str, runner: Runner, adapter: TocAdapter, clock: FakeClock
    ) -> None:
        adapter.during_toc = lambda: repo.novels.request_sync(novel_id)

        runner.step()

        [queued] = live(repo, novel_id, TaskState.QUEUED)
        assert (queued.run_after, queued.emit_event) == (clock(), True)
        assert len(refresh_tasks(repo, novel_id)) == 2
        assert nv_events(repo, novel_id)[-1]["next_refresh_at"] == stamp(clock())


class TestNovelRefresh__OnRetry:
    @pytest.fixture
    def retried(
        self, repo: Repo, novel_id: str, runner: Runner, adapter: TocAdapter, clock: FakeClock
    ) -> TaskRow:
        import_novel(runner, clock)
        [task] = live(repo, novel_id, TaskState.QUEUED)
        adapter.toc_error = InfrastructureError("FlareSolverr down")
        with pytest.raises(RunnerBackoff):
            runner.step()
        return task

    def test_finish__requeues_the_same_task_at_now_plus_the_min_interval(
        self, repo: Repo, novel_id: str, retried: TaskRow, clock: FakeClock
    ) -> None:
        [queued] = live(repo, novel_id, TaskState.QUEUED)
        assert (queued.id, queued.run_after) == (retried.id, clock() + timedelta(seconds=MIN))
        assert queued.error == "InfrastructureError: FlareSolverr down"

    def test_finish__keeps_the_interval(self, repo: Repo, novel_id: str, retried: TaskRow) -> None:
        assert novel_row(repo, novel_id).refresh_interval == MIN * 2

    def test_finish__appends_an_nv_event_with_syncing_false(
        self, repo: Repo, novel_id: str, retried: TaskRow, clock: FakeClock
    ) -> None:
        event = nv_events(repo, novel_id)[-1]
        assert (event["syncing"], event["next_refresh_at"]) == (
            False,
            stamp(clock() + timedelta(seconds=MIN)),
        )


class TestNovelRefresh__WhenTheFenceFailsAfterACoverWasStored:
    def test_finish__trashes_the_cover_key(
        self, repo: Repo, novel_id: str, runner: Runner, adapter: TocAdapter,
        settings: Settings, clock: FakeClock,
    ) -> None:  # fmt: skip
        adapter.during_toc = lambda: clock.advance(seconds=settings.claim_ttl + 1)

        with capture_logs() as logs:
            runner.step()

        [key] = trash_keys(repo.engine)
        assert key.startswith(f"covers/{novel_id}/")
        assert novel_row(repo, novel_id).cover_key is None
        assert "task_lost" in [line["event"] for line in logs]


class TestNovelRefresh__WhenTheNovelWasDeletedDuringTheRun:
    @pytest.fixture
    def finishes(self, repo: Repo, monkeypatch: pytest.MonkeyPatch) -> list[Finished | None]:
        results: list[Finished | None] = []
        finish = repo.tasks.finish

        def spy(*args: Any) -> Finished | None:
            results.append(finish(*args))
            return results[-1]

        monkeypatch.setattr(repo.tasks, "finish", spy)
        return results

    @pytest.fixture
    def logs(
        self, repo: Repo, novel_id: str, runner: Runner, adapter: TocAdapter,
        finishes: list[Finished | None],
    ) -> list[Any]:  # fmt: skip
        adapter.during_toc = lambda: repo.novels.delete(novel_id)
        with capture_logs() as logs:
            runner.step()
        return logs

    def test_finish__returns_none_and_logs_task_lost(
        self, finishes: list[Finished | None], logs: list[Any]
    ) -> None:
        assert finishes == [None]
        assert [line["event"] for line in logs][-1] == "task_lost"

    def test_finish__writes_nothing(self, repo: Repo, novel_id: str, logs: list[Any]) -> None:
        assert_no_events_after_delete(repo.engine, novel_id)
        with db.read(repo.engine) as conn:
            assert conn.execute(select(tasks)).all() == []
            assert conn.execute(select(chapters)).all() == []

    def test_finish__trashes_the_cover(self, repo: Repo, novel_id: str, logs: list[Any]) -> None:
        [key] = trash_keys(repo.engine)
        assert key.startswith(f"covers/{novel_id}/")


class TestNovelRefresh__OnPostSyncOnAnIdleNovel:
    def test_request_sync__syncs_once(
        self, repo: Repo, novel_id: str, runner: Runner, adapter: TocAdapter, clock: FakeClock
    ) -> None:
        assert runner.step() is True
        assert adapter.syncs == 1

        repo.novels.request_sync(novel_id)

        assert run_until_idle(runner) == 1
        assert adapter.syncs == 2
        [queued] = live(repo, novel_id, TaskState.QUEUED)
        assert queued.run_after > clock()


class TestNovelRefresh__OnSyncNowThreeTimesDuringASyncAndTwiceWhenIdle:
    @pytest.fixture
    def taps(
        self, repo: Repo, novel_id: str, runner: Runner, adapter: TocAdapter, clock: FakeClock
    ) -> list[tuple[int, int, str]]:
        """Each tap's (running, queued, next_refresh_at): two idle taps, then three mid-sync.

        The novel is idle and its next refresh is not due yet when the first tap comes.
        """
        assert runner.step() is True
        seen: list[tuple[int, int, str]] = []

        def tap() -> None:
            view = repo.novels.request_sync(novel_id)
            running = len(live(repo, novel_id, TaskState.RUNNING))
            queued = len(live(repo, novel_id, TaskState.QUEUED))
            seen.append((running, queued, stamp(view.next_refresh_at)))

        tap()
        tap()

        def tap_three_times() -> None:
            for _ in range(3):
                tap()

        adapter.during_toc = tap_three_times
        assert runner.step() is True
        adapter.during_toc = None
        return seen

    def test_request_sync__keeps_one_running_and_at_most_one_queued_due_now(
        self, taps: list[tuple[int, int, str]], clock: FakeClock
    ) -> None:
        now = stamp(clock())
        assert taps == [(0, 1, now), (0, 1, now), (1, 1, now), (1, 1, now), (1, 1, now)]

    def test_request_sync__runs_exactly_one_more_sync_after_the_current_one(
        self, repo: Repo, novel_id: str, runner: Runner, adapter: TocAdapter,
        taps: list[tuple[int, int, str]],
    ) -> None:  # fmt: skip
        syncs = adapter.syncs

        assert run_until_idle(runner) == 1
        assert adapter.syncs == syncs + 1
        assert len(live(repo, novel_id, TaskState.QUEUED)) == 1


class TestNovelRefresh__DerivedFields:
    def test_snapshot__during_a_sync_next_refresh_at_is_the_running_tasks_run_after(
        self, repo: Repo, novel_id: str, runner: Runner, adapter: TocAdapter, clock: FakeClock
    ) -> None:
        import_novel(runner, clock)
        [task] = live(repo, novel_id, TaskState.QUEUED)
        before = repo.events.latest_seq()
        seen: list[dict[str, Any]] = []
        adapter.during_toc = lambda: seen.append(repo.novels.get(novel_id).to_json())

        runner.step()

        started = nv_events(repo, novel_id, before)[0]
        assert started["syncing"] is True
        assert started["next_refresh_at"] == stamp(task.run_after)
        assert seen[0]["next_refresh_at"] == stamp(task.run_after)

    def test_request_sync__during_a_sync_appends_an_nv_event_due_now(
        self, repo: Repo, novel_id: str, runner: Runner, adapter: TocAdapter, clock: FakeClock
    ) -> None:
        import_novel(runner, clock)
        seen: list[dict[str, Any]] = []

        def tap() -> None:
            clock.advance(seconds=30)
            repo.novels.request_sync(novel_id)
            seen.append(nv_events(repo, novel_id)[-1])

        adapter.during_toc = tap

        runner.step()

        assert (seen[0]["next_refresh_at"], seen[0]["syncing"]) == (stamp(clock()), True)

    def test_get__syncing_is_false_once_the_running_claim_lapsed_before_recovery(
        self, repo: Repo, novel_id: str, runner: Runner, adapter: TocAdapter,
        settings: Settings, clock: FakeClock,
    ) -> None:  # fmt: skip
        seen: list[tuple[bool, int]] = []

        def lapse() -> None:
            clock.advance(seconds=settings.claim_ttl + 1)
            running = len(live(repo, novel_id, TaskState.RUNNING))
            seen.append((repo.novels.get(novel_id).syncing, running))

        adapter.during_toc = lapse

        runner.step()

        assert seen == [(False, 1)]


class TestNovelRefresh__Recovery:
    def test_recover__requeues_a_lapsed_running_refresh_and_appends_an_nv_event(
        self, repo: Repo, novel_id: str, settings: Settings, clock: FakeClock
    ) -> None:
        claimed = repo.tasks.claim("other", settings.claim_ttl)
        assert claimed is not None
        repo.tasks.start(claimed, "other")
        clock.advance(seconds=settings.claim_ttl + 1)
        before = repo.events.latest_seq()

        report = repo.tasks.recover()

        assert report.requeued == 1
        [task] = refresh_tasks(repo, novel_id)
        assert (task.id, task.state, task.claimed_by) == (claimed.id, TaskState.QUEUED, None)
        [event] = nv_events(repo, novel_id, before)
        assert (event["syncing"], event["next_refresh_at"]) == (False, stamp(task.run_after))
