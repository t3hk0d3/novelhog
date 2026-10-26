"""Task kinds through the runner: each kind's repo handler and worker processor (tasks spec 5).

`novel_refresh`: v1's refresh (spec section 6.2) as a task, with the derived `syncing` and
`next_refresh_at` (tasks spec sections 2 and 3) and the successor and manual-sync rules (5.2).
`export_build`: v1's build (spec section 6.5) as a task, and when builds are queued (5.1).
`chapter_download`: v1's download (spec section 6.4) as a task, queueing and dequeueing (tasks 4).
"""

import random
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select, update
from structlog.testing import capture_logs

from novelhog.chapter_store import ChapterStore
from novelhog.config import Settings
from novelhog.domain.chapters import ChapterState
from novelhog.domain.errors import NotFound
from novelhog.domain.exports import Format
from novelhog.domain.hosts import CHAPTER_MAX_RETRIES
from novelhog.domain.tasks import Done, Skip, Started, StartResult, TaskKind, TaskState
from novelhog.repo import Repo, db
from novelhog.repo import contents as contents_repo
from novelhog.repo import exports as export_repo
from novelhog.repo.novels import lock_novel
from novelhog.repo.schema import chapter_contents, chapters, exports, novels, tasks
from novelhog.repo.task_kinds.chapter_download import (
    ChapterClaim,
    ChapterDownloadHandler,
    Downloaded,
)
from novelhog.repo.task_kinds.export_build import ExportBuildHandler
from novelhog.repo.task_kinds.novel_refresh import NovelRefreshHandler
from novelhog.repo.tasks import Finished, NotStarted, TaskRow
from novelhog.scraper_adapter.adapter import TocResult
from novelhog.scraper_adapter.errors import InfrastructureError, SiteError, SiteUnreachable
from novelhog.storage import LocalStaticStorage
from novelhog.worker import runtime
from novelhog.worker.runner import Runner, RunnerBackoff
from novelhog.worker.tasks import default_processors
from novelhog.worker.tasks.base import HeartbeatFactory, TaskContext
from novelhog.worker.tasks.export_build import ExportBuildProcessor
from novelhog.worker.tasks.novel_refresh import NovelRefreshProcessor
from tests.conftest import FakeClock
from tests.integration.helpers import assert_no_events_after_delete, trash_keys
from tests.integration.task_fakes import claim_task, start_build, start_task, tasks_of
from tests.integration.worker.conftest import HOST, URL, FakeAdapter, FakeFetcher, RecordingStop

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
        self.chapter_titles: dict[str, str] = {}

    def sync_toc(self, novel_id: str, host: str, toc_url: str) -> TocResult:
        self.syncs += 1
        if self.during_toc is not None:
            self.during_toc()
        if self.toc_error is not None:
            raise self.toc_error
        return TocResult(
            "Lantern",
            "en",
            COVER_URL,
            list(self.urls),
            "<html></html>",
            chapter_titles=dict(self.chapter_titles),
        )


@pytest.fixture
def adapter() -> TocAdapter:
    return TocAdapter()


@pytest.fixture
def files(tmp_path: Path) -> LocalStaticStorage:
    return LocalStaticStorage(tmp_path / "static", "/static")


def make_runner(  # noqa: PLR0913
    repo: Repo,
    adapter: TocAdapter,
    fetcher: FakeFetcher,
    files: LocalStaticStorage,
    settings: Settings,
    heartbeat: HeartbeatFactory = runtime.Heartbeat,
) -> Runner:
    """The runner with the default processors, as the worker builds it; waits never sleep."""
    return Runner(
        repo,
        default_processors(),
        TaskContext(
            adapter=adapter,
            fetcher=fetcher,
            storage=files,
            chapter_store=ChapterStore(repo),
            settings=settings,
            stop=RecordingStop(),
            hosts=repo.hosts,
            rng=random.Random(3),  # noqa: S311 - refresh jitter, not security
            heartbeat=heartbeat,
            clock=repo.clock,
        ),
    )


@pytest.fixture
def runner(
    repo: Repo,
    adapter: TocAdapter,
    fetcher: FakeFetcher,
    files: LocalStaticStorage,
    settings: Settings,
) -> Runner:
    """The runner with the default processors, as the worker builds it."""
    return make_runner(repo, adapter, fetcher, files, settings)


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

    def test_finish__titles_the_chapters_by_the_toc(
        self, repo: Repo, novel_id: str, adapter: TocAdapter, runner: Runner
    ) -> None:
        adapter.chapter_titles = {f"{URL}/1": "Chapter 1: Start"}

        runner.step()

        with db.read(repo.engine) as conn:
            titles = conn.execute(
                select(chapters.c.position, chapters.c.title)
                .where(chapters.c.novel_id == novel_id)
                .order_by(chapters.c.position)
            ).all()
        assert [tuple(t) for t in titles] == [(1, "Chapter 1: Start"), (2, None)]

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


class _LostBeat:
    """A heartbeat whose renewal already found the claim gone."""

    lost = True

    def __enter__(self) -> _LostBeat:
        return self

    def __exit__(self, *_: object) -> None:
        return None


class _CountingFetcher(FakeFetcher):
    def __init__(self) -> None:
        super().__init__()
        self.fetches = 0

    def fetch(self, url: str) -> Any:
        self.fetches += 1
        return super().fetch(url)


class TestNovelRefresh__WhenTheHeartbeatLostTheClaimBeforeTheCover:
    @pytest.fixture
    def fetcher(self) -> _CountingFetcher:
        return _CountingFetcher()

    @pytest.fixture
    def lost_runner(  # noqa: PLR0913
        self,
        repo: Repo,
        adapter: TocAdapter,
        fetcher: _CountingFetcher,
        files: LocalStaticStorage,
        settings: Settings,
    ) -> Runner:
        return make_runner(repo, adapter, fetcher, files, settings, lambda renew, s: _LostBeat())

    def test_run__does_not_fetch_the_cover(
        self, novel_id: str, lost_runner: Runner, fetcher: _CountingFetcher
    ) -> None:
        assert lost_runner.step()

        assert fetcher.fetches == 0

    def test_run__stores_no_cover(
        self, repo: Repo, novel_id: str, lost_runner: Runner, tmp_path: Path
    ) -> None:
        assert lost_runner.step()

        assert not (tmp_path / "static" / "covers").exists()
        assert trash_keys(repo.engine) == []


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


class TestNovelRefresh__WhenRunRaises:
    @pytest.fixture
    def raising(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom(*_: object) -> None:
            message = "parser crashed"
            raise RuntimeError(message)

        monkeypatch.setattr(NovelRefreshProcessor, "run", boom)

    def test_finish__sets_import_error_on_the_initial_import(
        self, repo: Repo, novel_id: str, runner: Runner, raising: None
    ) -> None:
        runner.step()

        assert novel_row(repo, novel_id).import_error == "RuntimeError: parser crashed"

    def test_finish__backs_off_the_interval_and_schedules_the_successor_with_it(
        self, repo: Repo, novel_id: str, runner: Runner, clock: FakeClock, raising: None
    ) -> None:
        runner.step()

        assert novel_row(repo, novel_id).refresh_interval == MIN * 2
        [successor] = live(repo, novel_id, TaskState.QUEUED)
        wait = (successor.run_after - clock()).total_seconds()
        assert MIN * 2 * 0.9 <= wait <= MIN * 2 * 1.1

    def test_finish__leaves_import_error_alone_after_the_initial_import(
        self, repo: Repo, novel_id: str, runner: Runner, clock: FakeClock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:  # fmt: skip
        import_novel(runner, clock)

        def boom(*_: object) -> None:
            message = "parser crashed"
            raise RuntimeError(message)

        monkeypatch.setattr(NovelRefreshProcessor, "run", boom)

        runner.step()

        row = novel_row(repo, novel_id)
        assert (row.import_error, row.refresh_interval) == (None, MIN * 4)


class TestNovelRefresh__OnDoneWithoutItsResult:
    def test_complete__raises_a_programming_error(
        self, repo: Repo, novel_id: str, runner: Runner
    ) -> None:
        handler = NovelRefreshHandler(repo.context)
        [task] = refresh_tasks(repo, novel_id)

        with pytest.raises(TypeError), db.write(repo.engine) as conn:
            handler.complete(conn, task, Done("not a sync result"), repo.clock())


BUILD = TaskKind.EXPORT_BUILD


def set_chapter(repo: Repo, novel_id: str, position: int, state: str) -> str:
    """Set the resting state of the novel's chapter at `position`; return its id."""
    assert state in ("not_requested", "downloaded", "error")
    with db.write(repo.engine) as conn:
        chapter_id = conn.execute(
            update(chapters)
            .where(chapters.c.novel_id == novel_id, chapters.c.position == position)
            .values(state=state)
            .returning(chapters.c.id)
        ).scalar_one()
        if state == "downloaded":
            contents_repo.put(conn, chapter_id, f"Body of {position}", repo.clock())
    return str(chapter_id)


def set_states(repo: Repo, novel_id: str, states: str) -> list[str]:
    """Set chapter states by position from a string: d = downloaded, n = not_requested."""
    names = {"d": "downloaded", "n": "not_requested"}
    return [set_chapter(repo, novel_id, at, names[code]) for at, code in enumerate(states, start=1)]


def imported(runner: Runner, adapter: TocAdapter, clock: FakeClock, count: int) -> None:
    """Import the novel with `count` chapters (its next refresh is not due yet)."""
    adapter.urls = [f"{URL}/{n}" for n in range(1, count + 1)]
    assert runner.step() is True


def sync(repo: Repo, novel_id: str, runner: Runner, adapter: TocAdapter, *order: int) -> None:
    """Run a manual sync whose TOC lists the original chapters `order`."""
    adapter.urls = [f"{URL}/{n}" for n in order]
    repo.novels.request_sync(novel_id)
    claimed = repo.tasks.claim("worker-a", 300)
    assert claimed is not None
    assert claimed.kind is REFRESH
    runner.run_task(claimed)


def export(repo: Repo, novel_id: str, start: int, end: int, *, skip_missing: bool = False) -> str:
    view, _ = repo.exports.create(novel_id, start, end, Format.EPUB, skip_missing=skip_missing)
    return view.id


def builds_of(repo: Repo, export_id: str, state: TaskState | None = None) -> list[TaskRow]:
    return [t for t in tasks_of(repo, export_id) if state is None or t.state is state]


def export_state(repo: Repo, export_id: str) -> str:
    with db.read(repo.engine) as conn:
        return str(
            conn.execute(select(exports.c.state).where(exports.c.id == export_id)).scalar_one()
        )


def entities_since(repo: Repo, seq: int) -> list[str]:
    return [event.entity_id for event in repo.events.since(seq)]


@pytest.fixture
def imported3(
    repo: Repo, novel_id: str, runner: Runner, adapter: TocAdapter, clock: FakeClock
) -> list[str]:
    """The novel imported with chapters 1-3: 1 and 3 downloaded, 2 not requested."""
    imported(runner, adapter, clock, 3)
    return set_states(repo, novel_id, "dnd")


class TestExportBuild__Begin:
    def test_begin__starts_a_waiting_buildable_export_with_an_ex_event_building(
        self, repo: Repo, novel_id: str, imported3: list[str]
    ) -> None:
        export_id = export(repo, novel_id, 1, 1)
        [task] = builds_of(repo, export_id)
        before = repo.events.latest_seq()

        with db.write(repo.engine) as conn:
            assert ExportBuildHandler(repo.context).begin(conn, task, repo.clock()) == Started()

        assert export_state(repo, export_id) == "building"
        [event] = repo.events.since(before)
        assert (event.entity_id, event.payload["state"]) == (export_id, "building")

    def test_begin__skips_with_not_buildable_when_the_range_is_incomplete(
        self, repo: Repo, novel_id: str, imported3: list[str]
    ) -> None:
        export_id = export(repo, novel_id, 1, 3)
        [refresh] = live(repo, novel_id, TaskState.QUEUED)
        task = replace(refresh, kind=BUILD, entity_id=export_id)

        with db.write(repo.engine) as conn:
            begin = ExportBuildHandler(repo.context).begin(conn, task, repo.clock())

        assert begin == Skip("not buildable")
        assert export_state(repo, export_id) == "waiting"


class TestExportBuild__OnDoneOrFailed:
    def test_finish__done_ends_the_task_done_and_the_export_ready(
        self, repo: Repo, novel_id: str, runner: Runner, imported3: list[str]
    ) -> None:
        export_id = export(repo, novel_id, 1, 1)

        assert runner.step() is True

        [task] = builds_of(repo, export_id)
        assert (task.state, export_state(repo, export_id)) == (TaskState.DONE, "ready")

    def test_finish__failed_ends_the_task_and_the_export_failed_with_the_error(
        self, repo: Repo, novel_id: str, runner: Runner, imported3: list[str]
    ) -> None:
        export_id = export(repo, novel_id, 1, 1)
        with db.write(repo.engine) as conn:
            conn.execute(
                update(chapters)
                .where(chapters.c.id == imported3[0])
                .values(position=None, removed_at=repo.clock())
            )

        assert runner.step() is True

        [task] = builds_of(repo, export_id)
        assert (task.state, task.error) == (TaskState.FAILED, "Range changed upstream")
        assert export_state(repo, export_id) == "failed"


class TestExportBuild__WhenRunRaises:
    def test_finish__fails_the_export_without_a_retry(
        self, repo: Repo, novel_id: str, runner: Runner, imported3: list[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:  # fmt: skip
        export_id = export(repo, novel_id, 1, 1)

        def boom(*_: object) -> None:
            message = "exporter broke"
            raise RuntimeError(message)

        monkeypatch.setattr(ExportBuildProcessor, "run", boom)

        assert runner.step() is True

        assert [t.state for t in builds_of(repo, export_id)] == [TaskState.FAILED]
        with db.read(repo.engine) as conn:
            row = conn.execute(select(exports).where(exports.c.id == export_id)).one()
        assert (row.state, row.error) == ("failed", "RuntimeError: exporter broke")


class TestExportBuild__WhenTheNovelIsDeletedDuringTheBuild:
    @pytest.fixture
    def logs(
        self, repo: Repo, novel_id: str, runner: Runner, files: LocalStaticStorage,
        imported3: list[str], monkeypatch: pytest.MonkeyPatch,
    ) -> list[Any]:  # fmt: skip
        export(repo, novel_id, 1, 1)
        put_file = files.put_file

        def put_then_delete(*args: Any) -> None:
            put_file(*args)
            repo.novels.delete(novel_id)

        monkeypatch.setattr(files, "put_file", put_then_delete)
        with capture_logs() as logs:
            assert runner.step() is True
        return logs

    def test_delete__succeeds(self, repo: Repo, novel_id: str, logs: list[Any]) -> None:
        with pytest.raises(NotFound):
            repo.novels.get(novel_id)
        assert_no_events_after_delete(repo.engine, novel_id)

    def test_finish__is_task_lost(self, logs: list[Any]) -> None:
        assert [line["event"] for line in logs][-1] == "task_lost"

    def test_finish__trashes_the_stored_key(
        self, repo: Repo, novel_id: str, logs: list[Any]
    ) -> None:
        assert any(key.startswith(f"exports/{novel_id}/") for key in trash_keys(repo.engine))

    def test_delete__leaves_no_task_rows(self, repo: Repo, logs: list[Any]) -> None:
        with db.read(repo.engine) as conn:
            assert conn.execute(select(tasks)).all() == []


class TestExportBuild__Enqueueing:
    @pytest.mark.parametrize("skip_missing", [False, True])
    def test_create__queues_the_build_of_an_export_already_buildable(
        self, repo: Repo, novel_id: str, imported3: list[str], skip_missing: bool
    ) -> None:
        end = 3 if skip_missing else 1
        export_id = export(repo, novel_id, 1, end, skip_missing=skip_missing)

        [task] = builds_of(repo, export_id)
        view = repo.exports.get(novel_id, export_id)
        assert (task.state, task.priority, task.ordinal, task.emit_event) == (
            TaskState.QUEUED, 200, 0, True,
        )  # fmt: skip
        assert task.run_after == task.created_at == view.created_at

    def test_create__appends_the_tk_event_after_the_ex_event_in_the_same_transaction(
        self, repo: Repo, novel_id: str, imported3: list[str]
    ) -> None:
        before = repo.events.latest_seq()

        export_id = export(repo, novel_id, 1, 1)

        [task] = builds_of(repo, export_id)
        assert entities_since(repo, before) == [export_id, task.id]
        assert task.change_seq == before + 2

    def test_create__queues_no_build_for_an_export_that_is_not_buildable(
        self, repo: Repo, novel_id: str, imported3: list[str]
    ) -> None:
        export_id = export(repo, novel_id, 1, 3)

        assert builds_of(repo, export_id) == []

    def test_download__of_the_last_missing_chapter_queues_the_build_after_the_entity_events(
        self, repo: Repo, novel_id: str, imported3: list[str]
    ) -> None:
        export_id = export(repo, novel_id, 1, 3)  # queues chapter 2
        started = start_task(repo, imported3[1], "worker-a")
        before = repo.events.latest_seq()

        assert repo.tasks.finish(started.task, "worker-a", Done(Downloaded("x", "t", False)))

        [task] = builds_of(repo, export_id, TaskState.QUEUED)
        assert entities_since(repo, before) == [imported3[1], novel_id, export_id, task.id]

    def test_sync__moving_an_undownloaded_chapter_out_of_the_range_queues_the_build(
        self, repo: Repo, novel_id: str, runner: Runner, adapter: TocAdapter,
        imported3: list[str],
    ) -> None:  # fmt: skip
        export_id = export(repo, novel_id, 1, 3)  # waits for chapter 2
        assert builds_of(repo, export_id) == []

        sync(repo, novel_id, runner, adapter, 1, 3, 2)  # the range 1..3 now ends before 2

        assert len(builds_of(repo, export_id, TaskState.QUEUED)) == 1

    def test_sync__removing_an_end_chapter_queues_the_build_that_fails_range_changed(
        self, repo: Repo, novel_id: str, runner: Runner, adapter: TocAdapter,
        imported3: list[str],
    ) -> None:  # fmt: skip
        export_id = export(repo, novel_id, 1, 3)  # waits for chapter 2

        sync(repo, novel_id, runner, adapter, 1, 2)

        [task] = builds_of(repo, export_id, TaskState.QUEUED)
        assert runner.step() is True
        with db.read(repo.engine) as conn:
            row = conn.execute(select(exports).where(exports.c.id == export_id)).one()
        assert (row.state, row.error) == ("failed", "Range changed upstream")

    def test_sync__returning_a_removed_downloaded_chapter_completing_the_range_queues_the_build(
        self, repo: Repo, novel_id: str, runner: Runner, adapter: TocAdapter, clock: FakeClock
    ) -> None:
        imported(runner, adapter, clock, 4)
        set_states(repo, novel_id, "ddnd")
        sync(repo, novel_id, runner, adapter, 1, 3, 4)  # chapter 2 (downloaded) removed
        export_id = export(repo, novel_id, 1, 3)  # 1, 3, 4: waits for chapter 3
        assert builds_of(repo, export_id) == []

        sync(repo, novel_id, runner, adapter, 1, 2, 4)  # 3 removed, 2 back: 1, 2, 4 downloaded

        assert len(builds_of(repo, export_id, TaskState.QUEUED)) == 1

    def test_claim__runs_the_builds_of_two_exports_oldest_created_at_first(
        self, repo: Repo, novel_id: str, imported3: list[str], clock: FakeClock
    ) -> None:
        older = export(repo, novel_id, 1, 3)  # waits for chapter 2
        clock.advance(seconds=1)
        newer = export(repo, novel_id, 3, 3)  # buildable at once, queued first
        started = start_task(repo, imported3[1], "worker-a")
        assert repo.tasks.finish(started.task, "worker-a", Done(Downloaded("x", "t", False)))

        first = repo.tasks.claim("worker-a", 300)
        second = repo.tasks.claim("worker-a", 300)

        assert first is not None
        assert second is not None
        assert (first.entity_id, second.entity_id) == (older, newer)

    def test_patch__cancelling_a_needed_chapter_fails_the_export_and_cancels_its_build(
        self, repo: Repo, novel_id: str, runner: Runner, adapter: TocAdapter,
        imported3: list[str],
    ) -> None:  # fmt: skip
        set_chapter(repo, novel_id, 2, "downloaded")
        export_id = export(repo, novel_id, 1, 3)  # buildable: its build is queued
        sync(repo, novel_id, runner, adapter, 1, 9, 2, 3)  # a release inside the range, queued
        assert [t.state for t in builds_of(repo, export_id)] == [TaskState.QUEUED]

        repo.chapters.patch(novel_id, ChapterState.NOT_REQUESTED, [2])

        assert export_state(repo, export_id) == "failed"
        assert [t.state for t in builds_of(repo, export_id)] == [TaskState.CANCELLED]

    def test_delete__cancels_the_queued_build(
        self, repo: Repo, novel_id: str, imported3: list[str]
    ) -> None:
        export_id = export(repo, novel_id, 1, 1)

        repo.exports.delete(novel_id, export_id)

        assert [t.state for t in builds_of(repo, export_id)] == [TaskState.CANCELLED]

    def test_enqueue_buildable__a_repeated_enqueue_merges_into_one_queued_row(
        self, repo: Repo, novel_id: str, imported3: list[str]
    ) -> None:
        export_id = export(repo, novel_id, 1, 1)
        before = repo.events.latest_seq()

        with db.write(repo.engine) as conn:
            assert lock_novel(conn, novel_id)
            export_repo.enqueue_buildable(conn, novel_id, [export_id, export_id], repo.clock())
            export_repo.enqueue_buildable(conn, novel_id, [export_id], repo.clock())
            repo.tasks._queue.stamp(conn)  # noqa: SLF001

        assert len(builds_of(repo, export_id, TaskState.QUEUED)) == 1
        assert repo.events.latest_seq() == before


class TestExportBuild__Recovery:
    def test_recover__requeues_a_lapsed_running_build_and_sets_the_export_waiting(
        self, repo: Repo, novel_id: str, imported3: list[str], settings: Settings,
        clock: FakeClock,
    ) -> None:  # fmt: skip
        export_id = export(repo, novel_id, 1, 1)
        started = start_build(repo, export_id, "other", settings.claim_ttl)
        clock.advance(seconds=settings.claim_ttl + 1)
        before = repo.events.latest_seq()

        assert repo.tasks.recover().requeued == 1

        [task] = builds_of(repo, export_id)
        assert (task.id, task.state, task.claimed_by) == (started.task.id, TaskState.QUEUED, None)
        assert export_state(repo, export_id) == "waiting"
        ex = [e.payload for e in repo.events.since(before) if e.entity_id == export_id]
        assert [payload["state"] for payload in ex] == ["waiting"]


DOWNLOAD = TaskKind.CHAPTER_DOWNLOAD


def chapter_at(repo: Repo, novel_id: str, position: int) -> str:
    """The id of the novel's chapter at `position`."""
    with db.read(repo.engine) as conn:
        return str(
            conn.execute(
                select(chapters.c.id).where(
                    chapters.c.novel_id == novel_id, chapters.c.position == position
                )
            ).scalar_one()
        )


def chapter_record(repo: Repo, chapter_id: str) -> Any:
    with db.read(repo.engine) as conn:
        return conn.execute(select(chapters).where(chapters.c.id == chapter_id)).one()


def downloads_of(repo: Repo, chapter_id: str, state: TaskState | None = None) -> list[TaskRow]:
    return [t for t in tasks_of(repo, chapter_id) if state is None or t.state is state]


def ch_events(repo: Repo, chapter_id: str, after: int) -> list[dict[str, Any]]:
    return [e.payload for e in repo.events.since(after) if e.entity_id == chapter_id]


def stored(repo: Repo, chapter_id: str) -> str | None:
    with db.read(repo.engine) as conn:
        return conn.execute(
            select(chapter_contents.c.markdown).where(chapter_contents.c.chapter_id == chapter_id)
        ).scalar_one_or_none()


@pytest.fixture
def queued2(
    repo: Repo, novel_id: str, runner: Runner, adapter: TocAdapter, clock: FakeClock
) -> list[str]:
    """The novel imported with chapters 1-2, both queued by the user."""
    imported(runner, adapter, clock, 2)
    repo.chapters.patch(novel_id, ChapterState.PENDING, [1, 2])
    return [chapter_at(repo, novel_id, 1), chapter_at(repo, novel_id, 2)]


class TestChapterDownload__Begin:
    def test_start__turns_a_pending_positioned_chapter_downloading_with_a_ch_event(
        self, repo: Repo, novel_id: str, queued2: list[str]
    ) -> None:
        before = repo.events.latest_seq()

        started = start_task(repo, queued2[0], "worker-a")

        toc_url = repo.novels.get(novel_id).toc_url
        assert started.work == ChapterClaim(queued2[0], novel_id, HOST, toc_url, 1, f"{URL}/1")
        assert chapter_record(repo, queued2[0]).state == "downloading"
        assert [p["state"] for p in ch_events(repo, queued2[0], before)] == ["downloading"]

    @pytest.mark.parametrize(
        "values",
        [
            {"state": "downloaded"},
            {"position": None, "removed_at": datetime(2026, 1, 1, tzinfo=UTC)},
        ],
        ids=["not pending", "without a position"],
    )
    def test_begin__skips_with_not_pending_unless_pending_and_positioned(
        self, repo: Repo, queued2: list[str], values: dict[str, Any]
    ) -> None:
        handler = ChapterDownloadHandler(repo.context)
        [task] = downloads_of(repo, queued2[0])

        with db.write(repo.engine) as conn:
            conn.execute(update(chapters).where(chapters.c.id == queued2[0]).values(**values))
            assert handler.begin(conn, task, repo.clock()) == Skip("not pending")


class TestChapterDownload__WhenTheChapterWasDequeuedAfterTheClaim:
    def test_start__returns_cancelled(self, repo: Repo, novel_id: str, queued2: list[str]) -> None:
        claimed = claim_task(repo, queued2[0], "worker-a")
        repo.chapters.patch(novel_id, ChapterState.NOT_REQUESTED, [1])

        assert repo.tasks.start(claimed, "worker-a") == NotStarted(StartResult.CANCELLED)


class TestChapterDownload__WhenASyncRemovedTheChapterAfterTheClaim:
    def test_start__returns_cancelled(
        self, repo: Repo, novel_id: str, runner: Runner, adapter: TocAdapter, queued2: list[str]
    ) -> None:
        claimed = claim_task(repo, queued2[0], "worker-a")
        sync(repo, novel_id, runner, adapter, 2)  # chapter 1 removed and dequeued

        assert repo.tasks.start(claimed, "worker-a") == NotStarted(StartResult.CANCELLED)
        assert [t.state for t in downloads_of(repo, queued2[0])] == [TaskState.CANCELLED]


class TestChapterDownload__WhenASyncRemovedTheChapterMidDownload:
    @pytest.fixture
    def downloaded(
        self, repo: Repo, novel_id: str, runner: Runner, adapter: TocAdapter, queued2: list[str]
    ) -> str:
        adapter.hook = lambda url: sync(repo, novel_id, runner, adapter, 2)

        assert runner.step() is True
        return queued2[0]

    def test_finish__downloads_the_removed_chapter(self, repo: Repo, downloaded: str) -> None:
        row = chapter_record(repo, downloaded)
        assert (row.state, row.position) == ("downloaded", None)

    def test_finish__stores_its_content(self, repo: Repo, downloaded: str) -> None:
        assert stored(repo, downloaded) == "Hi"

    def test_finish__ends_the_task_done(self, repo: Repo, downloaded: str) -> None:
        assert [t.state for t in downloads_of(repo, downloaded)] == [TaskState.DONE]


class TestChapterDownload__OnDone:
    def test_finish__stores_the_chapter_downloaded_with_title_and_images(
        self, repo: Repo, runner: Runner, adapter: TocAdapter, queued2: list[str]
    ) -> None:
        adapter.titles[f"{URL}/1"] = "The Start"
        adapter.bodies[f"{URL}/1"] = '<p>Hi</p><p><img src="https://example.com/a.png"></p>'

        assert runner.step() is True

        row = chapter_record(repo, queued2[0])
        assert (row.state, row.title, row.has_images) == ("downloaded", "The Start", True)
        assert "data:image/png;base64," in (stored(repo, queued2[0]) or "")

    def test_finish__ends_the_task_done(
        self, repo: Repo, runner: Runner, queued2: list[str]
    ) -> None:
        assert runner.step() is True

        [task] = downloads_of(repo, queued2[0])
        assert (task.state, task.attempts, task.error) == (TaskState.DONE, 1, None)


class TestChapterDownload__OnFailed:
    @pytest.fixture
    def failed(self, repo: Repo, runner: Runner, adapter: TocAdapter, queued2: list[str]) -> str:
        adapter.errors[f"{URL}/1"] = SiteError("page gone")
        assert runner.step() is True
        return queued2[0]

    def test_finish__sets_the_chapter_error_with_the_message(self, repo: Repo, failed: str) -> None:
        assert chapter_record(repo, failed).state == "error"
        assert repo.chapters.get(failed).error == "SiteError: page gone"

    def test_finish__ends_the_task_failed_with_the_error(self, repo: Repo, failed: str) -> None:
        [task] = downloads_of(repo, failed)
        assert (task.state, task.error) == (TaskState.FAILED, "SiteError: page gone")


class TestChapterDownload__OnRetry:
    @pytest.fixture
    def before(self, repo: Repo, queued2: list[str]) -> TaskRow:
        [task] = downloads_of(repo, queued2[0])
        return task

    @pytest.fixture
    def retried(
        self, runner: Runner, adapter: TocAdapter, queued2: list[str], clock: FakeClock,
        before: TaskRow,
    ) -> str:  # fmt: skip
        adapter.errors[f"{URL}/1"] = InfrastructureError("dns")
        clock.advance(seconds=30)
        with pytest.raises(RunnerBackoff):
            runner.step()
        return queued2[0]

    def test_finish__puts_the_chapter_back_to_pending(self, repo: Repo, retried: str) -> None:
        assert chapter_record(repo, retried).state == "pending"

    def test_finish__requeues_the_same_task_keeping_run_after(
        self, repo: Repo, retried: str, before: TaskRow
    ) -> None:
        [task] = downloads_of(repo, retried)
        assert (task.id, task.state, task.run_after) == (
            before.id,
            TaskState.QUEUED,
            before.run_after,
        )
        assert (task.attempts, task.error) == (1, "InfrastructureError: dns")

    def test_get__queued_at_survives_the_retry(
        self, repo: Repo, retried: str, before: TaskRow
    ) -> None:
        assert repo.chapters.get(retried).queued_at == before.created_at


class TestChapterDownload__OnRetry__WhenTheChapterLostItsPositionMeanwhile:
    @pytest.fixture
    def retried(
        self, repo: Repo, novel_id: str, runner: Runner, adapter: TocAdapter, queued2: list[str]
    ) -> str:
        adapter.hook = lambda url: sync(repo, novel_id, runner, adapter, 2)
        adapter.errors[f"{URL}/1"] = InfrastructureError("dns")
        with pytest.raises(RunnerBackoff):
            runner.step()
        return queued2[0]

    def test_finish__sets_the_chapter_not_requested(self, repo: Repo, retried: str) -> None:
        assert chapter_record(repo, retried).state == "not_requested"

    def test_finish__cancels_the_task(self, repo: Repo, retried: str) -> None:
        assert [t.state for t in downloads_of(repo, retried)] == [TaskState.CANCELLED]


class TestChapterDownload__WhenTheSiteIsUnreachable:
    def test_run__retries_without_backing_the_loop_off(
        self, repo: Repo, runner: Runner, adapter: TocAdapter, queued2: list[str]
    ) -> None:
        adapter.errors[f"{URL}/1"] = SiteUnreachable("refused")

        assert runner.step() is True

        assert chapter_record(repo, queued2[0]).state == "pending"
        [task] = downloads_of(repo, queued2[0])
        assert (task.state, task.retries) == (TaskState.QUEUED, 1)

    def test_run__once_its_retries_are_used_fails_with_site_unreachable(
        self, repo: Repo, runner: Runner, adapter: TocAdapter, queued2: list[str]
    ) -> None:
        adapter.errors[f"{URL}/1"] = SiteUnreachable("refused")
        with db.write(repo.engine) as conn:
            conn.execute(
                update(tasks)
                .where(tasks.c.entity_id == queued2[0])
                .values(retries=CHAPTER_MAX_RETRIES)
            )

        assert runner.step() is True

        assert repo.chapters.get(queued2[0]).error == "Site unreachable: refused"
        [task] = downloads_of(repo, queued2[0])
        assert (task.state, task.error) == (TaskState.FAILED, "Site unreachable: refused")


class TestChapterDownload__WhenTheWorkerIsKilledMidDownload:
    """RF-2: the worker dies mid-download and restarts under another `worker_id`."""

    @pytest.fixture
    def restarted(
        self, repo: Repo, adapter: TocAdapter, fetcher: FakeFetcher, files: LocalStaticStorage,
        settings: Settings, clock: FakeClock, queued2: list[str],
    ) -> Runner:  # fmt: skip
        start_task(repo, queued2[0], "worker-killed", settings.claim_ttl)
        clock.advance(seconds=settings.claim_ttl + 1)
        other = replace(settings, worker_id="worker-b")
        return make_runner(repo, adapter, fetcher, files, other)

    def test_recover__puts_the_chapter_back_pending_with_queued_at_unchanged(
        self, repo: Repo, restarted: Runner, queued2: list[str]
    ) -> None:
        [task] = downloads_of(repo, queued2[0])

        restarted.recover()

        view = repo.chapters.get(queued2[0])
        assert (view.state, view.queued_at) == (ChapterState.PENDING, task.created_at)

    def test_step__downloads_it_once_and_the_task_is_done_with_attempts_2(
        self, repo: Repo, restarted: Runner, adapter: TocAdapter, queued2: list[str]
    ) -> None:
        restarted.recover()

        assert restarted.step() is True

        assert adapter.fetched == [f"{URL}/1"]
        assert chapter_record(repo, queued2[0]).state == "downloaded"
        [task] = downloads_of(repo, queued2[0])
        assert (task.state, task.attempts, task.claimed_by) == (TaskState.DONE, 2, None)

    def test_recover__never_leaves_two_live_tasks(
        self, repo: Repo, restarted: Runner, queued2: list[str]
    ) -> None:
        restarted.recover()
        assert len(downloads_of(repo, queued2[0])) == 1
        restarted.step()
        assert len(downloads_of(repo, queued2[0])) == 1


class TestChapterDownload__WhenTheNovelIsDeletedDuringTheDownload:
    """RF-3: `DELETE /novels` while one of its downloads runs."""

    @pytest.fixture
    def logs(
        self, repo: Repo, novel_id: str, runner: Runner, adapter: TocAdapter,
        queued2: list[str],
    ) -> list[Any]:  # fmt: skip
        adapter.hook = lambda url: repo.novels.delete(novel_id)
        with capture_logs() as logs:
            assert runner.step() is True
        return logs

    def test_delete__succeeds(self, repo: Repo, novel_id: str, logs: list[Any]) -> None:
        with pytest.raises(NotFound):
            repo.novels.get(novel_id)
        assert_no_events_after_delete(repo.engine, novel_id)

    def test_finish__is_task_lost(self, logs: list[Any]) -> None:
        assert [line["event"] for line in logs][-1] == "task_lost"

    def test_finish__stores_no_content(self, repo: Repo, logs: list[Any]) -> None:
        with db.read(repo.engine) as conn:
            assert conn.execute(select(chapter_contents)).all() == []

    def test_delete__leaves_no_task_rows(self, repo: Repo, logs: list[Any]) -> None:
        with db.read(repo.engine) as conn:
            assert conn.execute(select(tasks)).all() == []


class TestChapterDownload__Queueing:
    def test_patch__to_pending_enqueues_one_task_per_chapter_at_user_priority_by_position(
        self, repo: Repo, novel_id: str, runner: Runner, adapter: TocAdapter, clock: FakeClock
    ) -> None:
        imported(runner, adapter, clock, 3)

        repo.chapters.patch(novel_id, ChapterState.PENDING, [1, 3])

        for position in (1, 3):
            [task] = downloads_of(repo, chapter_at(repo, novel_id, position))
            assert (task.kind, task.state, task.priority, task.ordinal) == (
                DOWNLOAD, TaskState.QUEUED, 100, position,
            )  # fmt: skip
            assert (task.run_after, task.emit_event) == (clock(), False)
        assert downloads_of(repo, chapter_at(repo, novel_id, 2)) == []

    def test_patch__to_pending_appends_ch_events_showing_queued_at(
        self, repo: Repo, novel_id: str, runner: Runner, adapter: TocAdapter, clock: FakeClock
    ) -> None:
        imported(runner, adapter, clock, 2)
        before = repo.events.latest_seq()

        repo.chapters.patch(novel_id, ChapterState.PENDING, [1])

        [event] = ch_events(repo, chapter_at(repo, novel_id, 1), before)
        assert (event["state"], event["queued_at"]) == ("pending", stamp(clock()))
        assert [e.entity_id[:2] for e in repo.events.since(before)] == ["CH", "NV"]

    def test_sync__auto_queued_releases_enqueue_at_release_priority(
        self, repo: Repo, novel_id: str, runner: Runner, adapter: TocAdapter, clock: FakeClock
    ) -> None:
        imported(runner, adapter, clock, 2)

        sync(repo, novel_id, runner, adapter, 1, 2, 3)

        [task] = downloads_of(repo, chapter_at(repo, novel_id, 3))
        assert (task.state, task.priority, task.ordinal) == (TaskState.QUEUED, 110, 3)

    def test_patch__to_not_requested_cancels_the_queued_task(
        self, repo: Repo, novel_id: str, queued2: list[str]
    ) -> None:
        repo.chapters.patch(novel_id, ChapterState.NOT_REQUESTED, [1])

        assert [t.state for t in downloads_of(repo, queued2[0])] == [TaskState.CANCELLED]
        assert [t.state for t in downloads_of(repo, queued2[1])] == [TaskState.QUEUED]

    def test_patch__to_not_requested_leaves_a_running_download_to_finish(
        self, repo: Repo, novel_id: str, queued2: list[str]
    ) -> None:
        start_task(repo, queued2[0], "worker-a")

        result = repo.chapters.patch(novel_id, ChapterState.NOT_REQUESTED, [1])

        assert result.skipped == [(1, ChapterState.DOWNLOADING)]
        assert [t.state for t in downloads_of(repo, queued2[0])] == [TaskState.RUNNING]

    def test_sync__removing_a_pending_chapter_cancels_its_task(
        self, repo: Repo, novel_id: str, runner: Runner, adapter: TocAdapter, queued2: list[str]
    ) -> None:
        sync(repo, novel_id, runner, adapter, 2)

        assert chapter_record(repo, queued2[0]).state == "not_requested"
        assert [t.state for t in downloads_of(repo, queued2[0])] == [TaskState.CANCELLED]

    def test_get__queued_at_is_the_live_tasks_created_at(
        self, repo: Repo, novel_id: str, queued2: list[str], clock: FakeClock
    ) -> None:
        [task] = downloads_of(repo, queued2[0])
        clock.advance(minutes=5)
        start_task(repo, queued2[0], "worker-a")

        assert repo.chapters.get(queued2[0]).queued_at == task.created_at
        assert repo.chapters.get(queued2[1]).queued_at == task.created_at


class TestChapterDownload__Recovery:
    def test_recover__requeues_a_lapsed_download_with_run_after_kept_and_the_chapter_pending(
        self, repo: Repo, queued2: list[str], settings: Settings, clock: FakeClock
    ) -> None:
        started = start_task(repo, queued2[0], "other", settings.claim_ttl)
        clock.advance(seconds=settings.claim_ttl + 1)
        before = repo.events.latest_seq()

        assert repo.tasks.recover().requeued == 1

        [task] = downloads_of(repo, queued2[0])
        assert (task.id, task.state, task.run_after) == (
            started.task.id, TaskState.QUEUED, started.task.run_after,
        )  # fmt: skip
        assert [p["state"] for p in ch_events(repo, queued2[0], before)] == ["pending"]

    def test_recover__a_lapsed_download_without_a_position_cancels_it_and_dequeues_the_chapter(
        self, repo: Repo, novel_id: str, runner: Runner, adapter: TocAdapter,
        queued2: list[str], settings: Settings, clock: FakeClock,
    ) -> None:  # fmt: skip
        start_task(repo, queued2[0], "other", settings.claim_ttl)
        sync(repo, novel_id, runner, adapter, 2)  # removed mid-download: stays downloading
        clock.advance(seconds=settings.claim_ttl + 1)

        repo.tasks.recover()

        assert chapter_record(repo, queued2[0]).state == "not_requested"
        assert [t.state for t in downloads_of(repo, queued2[0])] == [TaskState.CANCELLED]
