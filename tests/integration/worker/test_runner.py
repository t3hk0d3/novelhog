"""The task runner (tasks spec section 6) with a recording handler and processor."""

import random
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from datetime import timedelta
from pathlib import Path
from types import TracebackType
from typing import Any, Self

import pytest
from structlog.testing import capture_logs

from novelhog.chapter_store import ChapterStore
from novelhog.config import Settings
from novelhog.domain.tasks import Failed, Retry, Skip, TaskState
from novelhog.repo import Repo, db
from novelhog.repo.novels import lock_novel
from novelhog.repo.tasks import TaskRow, UnregisteredTaskKindError
from novelhog.storage import LocalStaticStorage
from novelhog.worker.loop import Worker
from novelhog.worker.reachability import Reachability
from novelhog.worker.runner import Runner, RunnerBackoff
from novelhog.worker.tasks.base import TaskContext
from tests.conftest import FakeClock
from tests.integration.helpers import trash_keys
from tests.integration.task_fakes import (
    KIND,
    RecordingHandler,
    RecordingProcessor,
    enqueue_task,
    force_claim,
    task_row,
)
from tests.integration.worker.conftest import ME, FakeAdapter, FakeFetcher, RecordingStop

TTL = 300


class FakeHeartbeat:
    """Records the renew callback and interval instead of starting a thread."""

    def __init__(self, renew: Callable[[], bool], interval_s: float) -> None:
        self.renew = renew
        self.interval_s = interval_s
        self.lost = False

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None


@pytest.fixture
def beats() -> list[FakeHeartbeat]:
    return []


@pytest.fixture
def handler() -> RecordingHandler:
    return RecordingHandler()


@pytest.fixture
def processor() -> RecordingProcessor:
    return RecordingProcessor()


@pytest.fixture
def handled(
    repo: Repo, migrated_database: str, clock: FakeClock, handler: RecordingHandler
) -> Iterator[Repo]:
    """A `Repo` with the recording handler registered, on the database `repo` emptied."""
    handled_repo = Repo(migrated_database, clock=clock, handlers={KIND: handler})
    yield handled_repo
    handled_repo.engine.dispose()


@pytest.fixture
def stop() -> threading.Event:
    return threading.Event()


@pytest.fixture
def ctx(
    handled: Repo,
    adapter: FakeAdapter,
    fetcher: FakeFetcher,
    settings: Settings,
    tmp_path: Path,
    stop: threading.Event,
    beats: list[FakeHeartbeat],
) -> TaskContext:
    def heartbeat(renew: Callable[[], bool], interval_s: float) -> FakeHeartbeat:
        beat = FakeHeartbeat(renew, interval_s)
        beats.append(beat)
        return beat

    return TaskContext(
        adapter=adapter,
        fetcher=fetcher,
        storage=LocalStaticStorage(tmp_path / "static", "/static"),
        chapter_store=ChapterStore(handled),
        settings=settings,
        stop=stop,
        reachability=Reachability(),
        rng=random.Random(0),  # noqa: S311 - refresh jitter, not security
        heartbeat=heartbeat,
        clock=handled.clock,
    )


@pytest.fixture
def runner(handled: Repo, processor: RecordingProcessor, ctx: TaskContext) -> Runner:
    return Runner(handled, {KIND: processor}, ctx)


@pytest.fixture
def task(handled: Repo, novel_id: str) -> TaskRow:
    """A queued task of the recording kind for the novel, due now."""
    return enqueue_task(handled, novel_id)


def events_of(logs: Sequence[Mapping[str, Any]]) -> list[str]:
    return [entry["event"] for entry in logs if str(entry["event"]).startswith("task_")]


def entry(logs: Sequence[Mapping[str, Any]], event: str) -> Mapping[str, Any]:
    return next(line for line in logs if line["event"] == event)


def claim(repo: Repo) -> TaskRow:
    claimed = repo.tasks.claim(ME, TTL)
    assert claimed is not None
    return claimed


class TestRunnerStep__WhenNothingIsClaimable:
    def test_step__returns_false(self, handled: Repo, runner: Runner, novel_id: str) -> None:
        assert handled.tasks.claim("other", TTL) is not None  # the novel's first refresh

        assert runner.step() is False


class TestRunnerStep__WhenStartReturnsCancelledDeferredOrLost:
    def test_run_task__logs_task_cancelled_and_does_not_run(
        self, handled: Repo, runner: Runner, processor: RecordingProcessor, task: TaskRow
    ) -> None:
        claimed = claim(handled)
        queue = handled.tasks._queue  # noqa: SLF001
        with db.write(handled.engine) as conn:
            assert lock_novel(conn, task.novel_id)
            assert queue.cancel(conn, KIND, task.entity_id)
            queue.stamp(conn)

        with capture_logs() as logs:
            runner.run_task(claimed)

        assert events_of(logs) == ["task_cancelled"]
        assert processor.runs == []

    def test_run_task__logs_task_deferred_and_does_not_run(
        self, handled: Repo, runner: Runner, processor: RecordingProcessor, task: TaskRow,
        clock: FakeClock,
    ) -> None:  # fmt: skip
        sibling = claim(handled)
        handled.tasks.start(sibling, ME)
        successor = enqueue_task(handled, task.novel_id)
        claimed = force_claim(handled, successor.id, ME, clock() + timedelta(seconds=TTL))

        with capture_logs() as logs:
            runner.run_task(claimed)

        assert events_of(logs) == ["task_deferred"]
        assert processor.runs == []

    def test_run_task__logs_task_lost_and_does_not_run(
        self, handled: Repo, runner: Runner, processor: RecordingProcessor, task: TaskRow,
        clock: FakeClock,
    ) -> None:  # fmt: skip
        claimed = claim(handled)
        clock.advance(seconds=TTL + 1)

        with capture_logs() as logs:
            runner.run_task(claimed)

        assert events_of(logs) == ["task_lost"]
        assert processor.runs == []

    def test_step__returns_true(
        self, handled: Repo, runner: Runner, task: TaskRow, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        queue = handled.tasks._queue  # noqa: SLF001

        def cancel_first(claimed: TaskRow) -> bool:
            with db.write(handled.engine) as conn:
                assert lock_novel(conn, claimed.novel_id)
                queue.cancel(conn, KIND, claimed.entity_id)
                queue.stamp(conn)
            return True

        monkeypatch.setattr(runner, "_wait_turn", cancel_first)

        assert runner.step() is True
        assert task_row(handled, task.id).state is TaskState.CANCELLED

    def test_run_task__logs_the_task_fields(
        self, handled: Repo, runner: Runner, task: TaskRow, clock: FakeClock
    ) -> None:
        claimed = claim(handled)
        clock.advance(seconds=TTL + 1)

        with capture_logs() as logs:
            runner.run_task(claimed)

        line = entry(logs, "task_lost")
        assert (line["task_id"], line["kind"], line["entity_id"], line["attempts"]) == (
            task.id,
            KIND.value,
            task.entity_id,
            0,
        )


class TestRunnerStep__WhenBeginSaysSkip:
    def test_step__logs_task_skipped_with_the_reason(
        self, runner: Runner, handler: RecordingHandler, task: TaskRow
    ) -> None:
        handler.begin_result = Skip("novel gone")

        with capture_logs() as logs:
            runner.step()

        assert events_of(logs) == ["task_skipped"]
        assert entry(logs, "task_skipped")["reason"] == "novel gone"

    def test_step__does_not_run(
        self, runner: Runner, handler: RecordingHandler, processor: RecordingProcessor,
        task: TaskRow,
    ) -> None:  # fmt: skip
        handler.begin_result = Skip("novel gone")

        runner.step()

        assert processor.runs == []


class TestRunnerStep__WhenRunReturnsDone:
    def test_step__ends_the_task_done(
        self, handled: Repo, runner: Runner, processor: RecordingProcessor, task: TaskRow
    ) -> None:
        assert runner.step() is True

        assert task_row(handled, task.id).state is TaskState.DONE
        assert processor.runs == [(task.id, "https://example.com/novel")]

    def test_step__logs_task_started_then_task_finished_with_outcome_and_duration(
        self, runner: Runner, processor: RecordingProcessor, task: TaskRow, clock: FakeClock
    ) -> None:
        processor.during = lambda: clock.advance(seconds=2)

        with capture_logs() as logs:
            runner.step()

        assert events_of(logs) == ["task_started", "task_finished"]
        finished = entry(logs, "task_finished")
        assert (finished["outcome"], finished["duration_ms"], finished["error"]) == (
            "done",
            2000,
            None,
        )
        assert entry(logs, "task_started")["attempts"] == 1

    def test_step__logs_the_duration_stored_on_the_task(
        self, handled: Repo, runner: Runner, processor: RecordingProcessor, task: TaskRow,
        clock: FakeClock, monkeypatch: pytest.MonkeyPatch,
    ) -> None:  # fmt: skip
        processor.during = lambda: clock.advance(seconds=2)
        finish = handled.tasks.finish

        def finish_then_tick(*args: Any) -> Any:
            finished = finish(*args)
            clock.advance(seconds=1)
            return finished

        monkeypatch.setattr(handled.tasks, "finish", finish_then_tick)

        with capture_logs() as logs:
            runner.step()

        assert entry(logs, "task_finished")["duration_ms"] == 2000
        assert task_row(handled, task.id).duration_ms == 2000


class TestRunnerStep__WhenRunRaises:
    def test_step__fails_the_task_with_type_and_summary(
        self, handled: Repo, runner: Runner, processor: RecordingProcessor, task: TaskRow
    ) -> None:
        processor.error = ValueError("bad\n  page")

        runner.step()

        row = task_row(handled, task.id)
        assert (row.state, row.error) == (TaskState.FAILED, "ValueError: bad page")

    def test_step__logs_the_traceback(
        self, runner: Runner, processor: RecordingProcessor, task: TaskRow
    ) -> None:
        processor.error = ValueError("bad page")

        with capture_logs() as logs:
            runner.step()

        finished = entry(logs, "task_finished")
        assert (finished["outcome"], finished["error"]) == ("failed", "ValueError: bad page")
        assert finished["exc_info"] is processor.error


class TestRunnerStep__WhenRunReturnsFailed:
    def test_step__fails_the_task_with_the_error(
        self, handled: Repo, runner: Runner, processor: RecordingProcessor, task: TaskRow
    ) -> None:
        processor.outcome = Failed("SiteError: 404")

        runner.step()

        assert task_row(handled, task.id).error == "SiteError: 404"


class TestRunnerStep__WhenRunReturnsRetry:
    @pytest.fixture
    def later(self, processor: RecordingProcessor, clock: FakeClock) -> Any:
        run_after = clock() + timedelta(hours=6)
        processor.outcome = Retry(run_after, "InfrastructureError: dns")
        return run_after

    def test_step__requeues_the_task_at_run_after(
        self, handled: Repo, runner: Runner, task: TaskRow, later: Any
    ) -> None:
        with pytest.raises(RunnerBackoff):
            runner.step()

        row = task_row(handled, task.id)
        assert (row.state, row.run_after) == (TaskState.QUEUED, later)

    def test_step__logs_task_retry(self, runner: Runner, task: TaskRow, later: Any) -> None:
        with capture_logs() as logs, pytest.raises(RunnerBackoff):
            runner.step()

        assert events_of(logs) == ["task_started", "task_retry"]
        retry = entry(logs, "task_retry")
        assert (retry["run_after"], retry["error"]) == (later, "InfrastructureError: dns")

    def test_step__raises_runner_backoff(self, runner: Runner, task: TaskRow, later: Any) -> None:
        with pytest.raises(RunnerBackoff):
            runner.step()


class TestRunnerStep__WhenTheClaimIsLostDuringRun:
    @pytest.fixture
    def lost(
        self, handled: Repo, processor: RecordingProcessor, task: TaskRow, clock: FakeClock
    ) -> None:
        processor.keys = ["covers/NV.png"]
        processor.during = lambda: clock.advance(seconds=TTL + 1)

    def test_step__discards_the_result(
        self, handled: Repo, runner: Runner, handler: RecordingHandler, task: TaskRow, lost: None
    ) -> None:
        runner.step()

        assert task_row(handled, task.id).state is TaskState.RUNNING
        assert "complete" not in handler.hooks

    def test_step__logs_task_lost(self, runner: Runner, task: TaskRow, lost: None) -> None:
        with capture_logs() as logs:
            runner.step()

        assert events_of(logs) == ["task_started", "task_lost"]

    def test_step__trashes_the_stored_keys(
        self, handled: Repo, runner: Runner, task: TaskRow, lost: None
    ) -> None:
        runner.step()

        assert trash_keys(handled.engine) == ["covers/NV.png"]


class TestRunnerStep__WhenTheStartTransactionFails:
    def test_step__logs_task_error_and_raises_runner_backoff(
        self, runner: Runner, handler: RecordingHandler, task: TaskRow
    ) -> None:
        handler.fail_in = "begin"

        with capture_logs() as logs, pytest.raises(RunnerBackoff):
            runner.step()

        assert events_of(logs) == ["task_error"]

    def test_step__task_keeps_its_claim_and_is_recovered_after_the_ttl(
        self, handled: Repo, runner: Runner, handler: RecordingHandler, task: TaskRow,
        clock: FakeClock,
    ) -> None:  # fmt: skip
        handler.fail_in = "begin"
        with pytest.raises(RunnerBackoff):
            runner.step()
        row = task_row(handled, task.id)
        assert (row.state, row.claimed_by) == (TaskState.QUEUED, ME)

        clock.advance(seconds=TTL + 1)
        handled.tasks.recover()

        assert task_row(handled, task.id).claimed_by is None


class TestRunnerStep__WhenTheFinishTransactionFails:
    def test_step__logs_task_error_and_raises_runner_backoff(
        self, runner: Runner, handler: RecordingHandler, task: TaskRow
    ) -> None:
        handler.fail_in = "complete"

        with capture_logs() as logs, pytest.raises(RunnerBackoff):
            runner.step()

        assert events_of(logs) == ["task_started", "task_error"]

    def test_step__task_keeps_its_claim_and_is_recovered_after_the_ttl(
        self, handled: Repo, runner: Runner, handler: RecordingHandler, task: TaskRow,
        clock: FakeClock,
    ) -> None:  # fmt: skip
        handler.fail_in = "complete"
        with pytest.raises(RunnerBackoff):
            runner.step()
        row = task_row(handled, task.id)
        assert (row.state, row.claimed_by) == (TaskState.RUNNING, ME)

        clock.advance(seconds=TTL + 1)
        report = handled.tasks.recover()

        assert task_row(handled, task.id).state is TaskState.QUEUED
        assert report.requeued == 1


class TestRunnerStep__WhileRunIsWorking:
    def test_step__renews_the_claim_every_third_of_the_ttl(
        self, handled: Repo, runner: Runner, processor: RecordingProcessor, task: TaskRow,
        beats: list[FakeHeartbeat], clock: FakeClock,
    ) -> None:  # fmt: skip
        renewed: list[bool] = []

        def renew_midway() -> None:
            now = clock.advance(seconds=200)
            renewed.append(beats[0].renew())
            assert task_row(handled, task.id).claimed_until == now + timedelta(seconds=TTL)

        processor.during = renew_midway

        runner.step()

        assert [beat.interval_s for beat in beats] == [TTL / 3]
        assert renewed == [True]
        assert task_row(handled, task.id).state is TaskState.DONE


class TestRunnerStep__OnStop:
    def test_step__releases_a_claimed_unstarted_task(
        self, handled: Repo, runner: Runner, processor: RecordingProcessor, task: TaskRow,
        stop: threading.Event,
    ) -> None:  # fmt: skip
        stop.set()

        assert runner.step() is True

        row = task_row(handled, task.id)
        assert (row.state, row.claimed_by, row.attempts) == (TaskState.QUEUED, None, 0)
        assert processor.runs == []

    def test_step__lets_a_running_task_finish(
        self, handled: Repo, runner: Runner, processor: RecordingProcessor, task: TaskRow,
        stop: threading.Event,
    ) -> None:  # fmt: skip
        processor.during = stop.set

        runner.step()

        assert task_row(handled, task.id).state is TaskState.DONE


class TestRunnerStep__WithoutARegisteredProcessor:
    def test_step__releases_the_claim_and_raises(
        self, handled: Repo, ctx: TaskContext, task: TaskRow
    ) -> None:
        with pytest.raises(UnregisteredTaskKindError):
            Runner(handled, {}, ctx).step()

        row = task_row(handled, task.id)
        assert (row.state, row.claimed_by) == (TaskState.QUEUED, None)


class TestRunnerRecover:
    def test_recover__logs_task_recovered_for_each_running_task_put_back(
        self, handled: Repo, runner: Runner, task: TaskRow, clock: FakeClock
    ) -> None:
        handled.tasks.start(claim(handled), ME)
        clock.advance(seconds=TTL + 1)

        with capture_logs() as logs:
            report = runner.recover()

        assert report.requeued == 1
        line = entry(logs, "task_recovered")
        assert (line["task_id"], line["kind"], line["attempts"]) == (task.id, KIND.value, 1)


def make_worker(
    repo: Repo,
    adapter: FakeAdapter,
    fetcher: FakeFetcher,
    settings: Settings,
    tmp_path: Path,
    processor: RecordingProcessor,
    stop: threading.Event,
) -> Worker:
    storage = LocalStaticStorage(tmp_path / "static", "/static")
    return Worker(
        repo, adapter, fetcher, storage, {}, settings, stop=stop, processors={KIND: processor}
    )


class TestWorkerStep__WithTasks:
    def test_step__runs_the_runner_before_the_v1_stages_and_reports_task(
        self, handled: Repo, adapter: FakeAdapter, fetcher: FakeFetcher, settings: Settings,
        tmp_path: Path, processor: RecordingProcessor, task: TaskRow,
    ) -> None:  # fmt: skip
        worker = make_worker(
            handled, adapter, fetcher, settings, tmp_path, processor, RecordingStop()
        )

        assert worker.step() == "task"
        assert task_row(handled, task.id).state is TaskState.DONE
        assert worker.step() == "idle"  # the task was the novel's refresh

    def test_step__recovers_lapsed_tasks(
        self, handled: Repo, adapter: FakeAdapter, fetcher: FakeFetcher, settings: Settings,
        tmp_path: Path, processor: RecordingProcessor, task: TaskRow, clock: FakeClock,
    ) -> None:  # fmt: skip
        handled.tasks.start(claim(handled), ME)
        clock.advance(seconds=TTL + 1)
        worker = make_worker(
            handled, adapter, fetcher, settings, tmp_path, processor, RecordingStop()
        )

        assert worker.step() == "task"
        assert task_row(handled, task.id).attempts == 2

    def test_run__backs_off_on_runner_backoff_without_worker_step_failed(
        self, handled: Repo, adapter: FakeAdapter, fetcher: FakeFetcher, settings: Settings,
        tmp_path: Path, processor: RecordingProcessor, task: TaskRow, clock: FakeClock,
    ) -> None:  # fmt: skip
        processor.outcome = Retry(clock() + timedelta(hours=1), "InfrastructureError: dns")
        stop = RecordingStop(stop_after_waits=1)
        worker = make_worker(handled, adapter, fetcher, settings, tmp_path, processor, stop)

        with capture_logs() as logs:
            worker.run()

        assert stop.waits == [5]
        names = [line["event"] for line in logs]
        assert "task_retry" in names
        assert "worker_step_failed" not in names
