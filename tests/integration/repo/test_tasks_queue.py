"""The task queue primitives and the change cursor (tasks spec sections 3 and 4)."""

import threading
from collections.abc import Callable, Iterator
from dataclasses import replace
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import Connection, delete, event, func, insert, select, update
from sqlalchemy.exc import IntegrityError

from novelhog.domain.tasks import Done, Failed, Retry, Skip, StartResult, TaskKind, TaskState
from novelhog.repo import Busy, Repo, db
from novelhog.repo import events as event_log
from novelhog.repo.novels import lock_novel
from novelhog.repo.schema import events, novels, tasks
from novelhog.repo.tasks import (
    Finished,
    NotStarted,
    Ready,
    Skipped,
    TaskQueue,
    TaskRecoveryReport,
    TaskRow,
    UnregisteredTaskKindError,
    cancel_many,
    enqueue_many,
)
from tests.conftest import FakeClock
from tests.integration.helpers import NOW, insert_host, novel_row
from tests.integration.task_fakes import RecordingHandler, force_claim

NOVEL = "NV000000000001"
OTHER_NOVEL = "NV000000000002"
CHAPTER = "CH000000000001"
TTL = 300
ME = "worker-a"
OTHER = "worker-b"
REFRESH = TaskKind.NOVEL_REFRESH
DOWNLOAD = TaskKind.CHAPTER_DOWNLOAD


@pytest.fixture
def queue(repo: Repo) -> TaskQueue:
    with db.write(repo.engine) as conn:
        insert_host(conn)
        conn.execute(insert(novels).values(**novel_row()))
        conn.execute(
            insert(novels).values(**novel_row(id=OTHER_NOVEL, toc_url="https://example.com/other"))
        )
    return TaskQueue(repo.context)


@pytest.fixture
def other_repo(migrated_database: str, clock: FakeClock) -> Iterator[Repo]:
    """A second `Repo` on the same database, for races between two processes."""
    other = Repo(migrated_database, clock=clock)
    yield other
    other.engine.dispose()


@pytest.fixture
def other_queue(other_repo: Repo) -> TaskQueue:
    """The task queue of the second `Repo`."""
    return TaskQueue(other_repo.context)


def chapter(n: int) -> str:
    return f"CH{n:012d}"


def _in_task_transaction(
    repo: Repo, queue: TaskQueue, novel_id: str, work: Callable[[Connection], Any]
) -> Any:
    """Run `work` in a write transaction that follows the lock order and ends with `stamp`."""
    with db.write(repo.engine) as conn:
        assert lock_novel(conn, novel_id)
        result = work(conn)
        queue.stamp(conn)
    return result


def enqueue(
    repo: Repo,
    queue: TaskQueue,
    kind: TaskKind = REFRESH,
    entity_id: str = NOVEL,
    novel_id: str = NOVEL,
    **options: Any,
) -> TaskRow:
    arguments: dict[str, Any] = {"priority": 300, "run_after": NOW} | options
    return _in_task_transaction(  # type: ignore[no-any-return]
        repo,
        queue,
        novel_id,
        lambda conn: queue.enqueue(conn, kind, entity_id, novel_id, **arguments),
    )


def start(repo: Repo, queue: TaskQueue, task: TaskRow, worker: str = ME) -> StartResult:
    return _in_task_transaction(  # type: ignore[no-any-return]
        repo, queue, task.novel_id, lambda conn: queue.start(conn, task, worker)
    )


def finish(
    repo: Repo,
    queue: TaskQueue,
    task: TaskRow,
    state: TaskState,
    error: str | None = None,
    worker: str = ME,
) -> bool:
    return _in_task_transaction(  # type: ignore[no-any-return]
        repo, queue, task.novel_id, lambda conn: queue.finish(conn, task, worker, state, error)
    )


def retry(
    repo: Repo, queue: TaskQueue, task: TaskRow, *, run_after: Any, error: str, worker: str = ME
) -> bool:
    return _in_task_transaction(  # type: ignore[no-any-return]
        repo,
        queue,
        task.novel_id,
        lambda conn: queue.retry(conn, task, worker, run_after=run_after, error=error),
    )


def cancel(repo: Repo, queue: TaskQueue, kind: TaskKind = REFRESH, entity_id: str = NOVEL) -> bool:
    return _in_task_transaction(  # type: ignore[no-any-return]
        repo, queue, NOVEL, lambda conn: queue.cancel(conn, kind, entity_id)
    )


def running(
    repo: Repo, queue: TaskQueue, kind: TaskKind = REFRESH, entity_id: str = NOVEL, **options: Any
) -> TaskRow:
    """Enqueue a task, claim it for ME and start it."""
    task = enqueue(repo, queue, kind, entity_id, **options)
    claimed = queue.claim(ME, TTL)
    assert claimed is not None and claimed.id == task.id
    assert start(repo, queue, claimed) is StartResult.OK
    return get(repo, task.id)


def get(repo: Repo, task_id: str) -> TaskRow:
    with db.read(repo.engine) as conn:
        return TaskRow.from_row(conn.execute(select(tasks).where(tasks.c.id == task_id)).one())


def find(repo: Repo, task_id: str) -> TaskRow | None:
    with db.read(repo.engine) as conn:
        row = conn.execute(select(tasks).where(tasks.c.id == task_id)).first()
    return None if row is None else TaskRow.from_row(row)


def all_tasks(repo: Repo) -> list[TaskRow]:
    with db.read(repo.engine) as conn:
        return [TaskRow.from_row(row) for row in conn.execute(select(tasks).order_by(tasks.c.id))]


def queued(repo: Repo, kind: TaskKind = REFRESH, entity_id: str = NOVEL) -> list[TaskRow]:
    return [
        task
        for task in all_tasks(repo)
        if (task.kind, task.entity_id, task.state) == (kind, entity_id, TaskState.QUEUED)
    ]


def clock_value(repo: Repo) -> int:
    return repo.events.latest_seq()


def event_count(repo: Repo) -> int:
    with db.read(repo.engine) as conn:
        return int(conn.execute(select(func.count()).select_from(events)).scalar_one())


def lapse(clock: FakeClock) -> None:
    clock.advance(seconds=TTL + 1)


def take_over(repo: Repo, task: TaskRow) -> TaskRow:
    """Give the running task a live claim of another worker and return the new row."""
    with db.write(repo.engine) as conn:
        conn.execute(
            update(tasks)
            .where(tasks.c.id == task.id)
            .values(claimed_by=OTHER, claimed_until=NOW + timedelta(seconds=TTL))
        )
    return get(repo, task.id)


class _Ordered:
    """Run `first` in a held write transaction, then `second` while it is held; release both.

    `second` must wait for `first` to commit (the novel lock, the clock, or SQLite's write lock).
    """

    def __init__(
        self,
        repo: Repo,
        other: Repo,
        first: Callable[[Connection], Any],
        second: Callable[[Connection], Any],
    ) -> None:
        self.engines = (repo.engine, other.engine)
        self.steps = (first, second)
        self.results: list[Any] = [None, None]
        self.errors: list[BaseException] = []
        self.first_done = threading.Event()
        self.second_done = threading.Event()
        self.release = threading.Event()

    def _run(self, index: int) -> None:
        try:
            with db.write(self.engines[index]) as conn:
                self.results[index] = self.steps[index](conn)
                if index == 0:
                    self.first_done.set()
                    self.release.wait(timeout=30)
            if index == 1:
                self.second_done.set()
        except BaseException as error:  # noqa: BLE001  (re-raised in the main thread)
            self.errors.append(error)
            self.first_done.set()

    def run(self) -> list[Any]:
        first = threading.Thread(target=self._run, args=(0,), daemon=True)
        second = threading.Thread(target=self._run, args=(1,), daemon=True)
        first.start()
        assert self.first_done.wait(timeout=30)
        second.start()
        blocked = not self.second_done.wait(timeout=0.5)
        self.release.set()
        first.join(timeout=30)
        second.join(timeout=30)
        if self.errors:
            raise self.errors[0]
        assert blocked, "the second transaction should wait for the first one"
        assert not first.is_alive() and not second.is_alive()
        return self.results


class TestTaskRow:
    def test_to_view__carries_the_task_object_fields(self, repo: Repo, queue: TaskQueue) -> None:
        task = get(repo, enqueue(repo, queue).id)

        view = task.to_view()

        assert (view.id, view.kind, view.entity_id, view.novel_id, view.state) == (
            task.id,
            REFRESH,
            NOVEL,
            NOVEL,
            TaskState.QUEUED,
        )
        assert (view.attempts, view.error, view.run_after, view.change_seq) == (
            0,
            None,
            NOW,
            task.change_seq,
        )
        assert (view.created_at, view.updated_at) == (NOW, NOW)
        assert (view.started_at, view.finished_at, view.duration_ms) == (None, None, None)


class TestEnqueue__WhenNoQueuedTaskExists:
    def test_enqueue__inserts_a_queued_task_with_the_given_fields(
        self, repo: Repo, queue: TaskQueue
    ) -> None:
        later = NOW + timedelta(minutes=5)

        task = enqueue(
            repo,
            queue,
            DOWNLOAD,
            CHAPTER,
            priority=110,
            ordinal=7,
            run_after=later,
            emit_event=True,
        )

        row = get(repo, task.id)
        assert row.id.startswith("TK")
        assert (row.kind, row.entity_id, row.novel_id, row.state) == (
            DOWNLOAD,
            CHAPTER,
            NOVEL,
            TaskState.QUEUED,
        )
        assert (row.priority, row.ordinal, row.run_after, row.emit_event) == (110, 7, later, True)
        assert row.payload == {}
        assert (row.claimed_by, row.claimed_until) == (None, None)

    def test_enqueue__sets_created_and_updated_at_to_now_and_attempts_to_0(
        self, repo: Repo, queue: TaskQueue, clock: FakeClock
    ) -> None:
        now = clock.advance(seconds=3)

        row = get(repo, enqueue(repo, queue).id)

        assert (row.created_at, row.updated_at, row.attempts) == (now, now, 0)


class TestEnqueue__WhenAQueuedTaskExists:
    def test_enqueue__merges_min_run_after_max_priority_and_or_emit_event(
        self, repo: Repo, queue: TaskQueue
    ) -> None:
        first = enqueue(repo, queue, priority=100, run_after=NOW + timedelta(hours=1))

        enqueue(repo, queue, priority=300, run_after=NOW, emit_event=True)
        enqueue(repo, queue, priority=200, run_after=NOW + timedelta(hours=2), emit_event=False)

        row = get(repo, first.id)
        assert (row.run_after, row.priority, row.emit_event) == (NOW, 300, True)

    def test_enqueue__keeps_created_at_and_the_claim(
        self, repo: Repo, queue: TaskQueue, clock: FakeClock
    ) -> None:
        first = enqueue(repo, queue)
        claimed = queue.claim(ME, TTL)
        assert claimed is not None
        clock.advance(seconds=10)

        enqueue(repo, queue, run_after=NOW - timedelta(hours=1))

        row = get(repo, first.id)
        assert row.created_at == NOW
        assert (row.claimed_by, row.claimed_until) == (ME, NOW + timedelta(seconds=TTL))

    def test_enqueue__leaves_exactly_one_queued_row_and_returns_it(
        self, repo: Repo, queue: TaskQueue
    ) -> None:
        first = enqueue(repo, queue, run_after=NOW + timedelta(hours=1))

        merged = enqueue(repo, queue, run_after=NOW)

        assert [task.id for task in queued(repo)] == [first.id]
        assert (merged.id, merged.run_after) == (first.id, NOW)

    def test_enqueue__a_merge_that_changes_no_task_field_leaves_updated_at_and_change_seq(
        self, repo: Repo, queue: TaskQueue, clock: FakeClock
    ) -> None:
        first = get(repo, enqueue(repo, queue, priority=100).id)
        clock.advance(seconds=10)

        enqueue(repo, queue, priority=300, run_after=NOW + timedelta(hours=1))

        row = get(repo, first.id)
        assert row.priority == 300
        assert (row.updated_at, row.change_seq) == (first.updated_at, first.change_seq)


class TestEnqueue__WhenOnlyARunningTaskExists:
    def test_enqueue__inserts_a_queued_successor_beside_it(
        self, repo: Repo, queue: TaskQueue
    ) -> None:
        current = running(repo, queue)

        successor = enqueue(repo, queue)

        assert successor.id != current.id
        assert get(repo, current.id).state is TaskState.RUNNING
        assert [task.id for task in queued(repo)] == [successor.id]


class TestEnqueue__WithAMismatchedEntityPrefix:
    def test_enqueue__is_rejected_by_the_database_check(self, repo: Repo, queue: TaskQueue) -> None:
        with pytest.raises(IntegrityError, match="ck_tasks_kind"):
            enqueue(repo, queue, REFRESH, CHAPTER)


class TestClaim:
    def test_claim__returns_by_priority_then_run_after_then_ordinal_then_id(
        self, repo: Repo, queue: TaskQueue
    ) -> None:
        early = NOW - timedelta(minutes=5)
        low = enqueue(repo, queue, DOWNLOAD, chapter(1), priority=100, run_after=early)
        late = enqueue(repo, queue, DOWNLOAD, chapter(2), priority=110, run_after=NOW, ordinal=1)
        high = enqueue(repo, queue, REFRESH, NOVEL, priority=300, run_after=NOW)
        second = enqueue(repo, queue, DOWNLOAD, chapter(3), priority=110, run_after=NOW, ordinal=2)
        tied = [
            enqueue(repo, queue, DOWNLOAD, chapter(n), priority=110, run_after=early, ordinal=9)
            for n in (4, 5, 6)
        ]
        first_ordinal = enqueue(
            repo, queue, DOWNLOAD, chapter(7), priority=110, run_after=early, ordinal=3
        )
        expected = (
            [high.id, first_ordinal.id]
            + sorted(task.id for task in tied)
            + [late.id, second.id, low.id]
        )

        claimed = [queue.claim(ME, TTL) for _ in expected]

        assert [task.id if task else None for task in claimed] == expected

    def test_claim__sets_the_claim_and_leaves_the_task_queued(
        self, repo: Repo, queue: TaskQueue
    ) -> None:
        task = enqueue(repo, queue)

        claimed = queue.claim(ME, TTL)

        assert claimed is not None and claimed.id == task.id
        row = get(repo, task.id)
        assert (row.claimed_by, row.claimed_until, row.state) == (
            ME,
            NOW + timedelta(seconds=TTL),
            TaskState.QUEUED,
        )
        assert claimed == row

    def test_claim__skips_tasks_due_in_the_future(self, repo: Repo, queue: TaskQueue) -> None:
        enqueue(repo, queue, run_after=NOW + timedelta(seconds=1))

        assert queue.claim(ME, TTL) is None

    def test_claim__skips_a_queued_task_whose_entity_has_a_running_task(
        self, repo: Repo, queue: TaskQueue
    ) -> None:
        running(repo, queue)
        enqueue(repo, queue)

        assert queue.claim(OTHER, TTL) is None

    def test_claim__takes_a_task_whose_claim_lapsed(
        self, repo: Repo, queue: TaskQueue, clock: FakeClock
    ) -> None:
        task = enqueue(repo, queue)
        assert queue.claim(ME, TTL) is not None
        lapse(clock)

        claimed = queue.claim(OTHER, TTL)

        assert claimed is not None and claimed.id == task.id
        assert get(repo, task.id).claimed_by == OTHER

    def test_claim__returns_none_when_nothing_is_eligible(self, queue: TaskQueue) -> None:
        assert queue.claim(ME, TTL) is None

    def test_claim__two_workers_racing_claim_different_tasks_or_one_gets_none(
        self, repo: Repo, queue: TaskQueue, other_queue: TaskQueue
    ) -> None:
        for n in (1, 2):
            enqueue(repo, queue, DOWNLOAD, chapter(n), priority=100)
        barrier = threading.Barrier(2)
        results: list[TaskRow | None] = []
        errors: list[BaseException] = []

        def worker(target: TaskQueue, name: str) -> None:
            try:
                barrier.wait(timeout=30)
                results.append(target.claim(name, TTL))
            except BaseException as error:  # noqa: BLE001  (re-raised in the main thread)
                errors.append(error)

        threads = [
            threading.Thread(target=worker, args=(queue, ME)),
            threading.Thread(target=worker, args=(other_queue, OTHER)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)

        if errors:
            raise errors[0]
        ids = [task.id for task in results if task is not None]
        assert len(ids) == len(set(ids))
        assert {task.claimed_by for task in all_tasks(repo) if task.claimed_by} == {
            task.claimed_by for task in results if task is not None
        }

    def test_claim__a_task_cancelled_while_the_claim_runs_is_not_claimed(
        self, repo: Repo, queue: TaskQueue, other_queue: TaskQueue
    ) -> None:
        """On Postgres the claim blocks on the cancel's row lock and re-checks the new version."""
        task = enqueue(repo, queue)
        outcome: list[TaskRow | None] = []
        failures: list[BaseException] = []

        def claim() -> None:
            try:
                outcome.append(other_queue.claim(OTHER, TTL))
            except BaseException as error:  # noqa: BLE001  (re-raised in the main thread)
                failures.append(error)

        with db.write(repo.engine) as conn:
            assert lock_novel(conn, NOVEL)
            assert queue.cancel(conn, REFRESH, NOVEL)
            queue.stamp(conn)
            thread = threading.Thread(target=claim)
            thread.start()
            thread.join(timeout=1)
            assert thread.is_alive(), "the claim should wait for the cancel to commit"
        thread.join(timeout=30)

        if failures:
            raise failures[0]
        assert outcome == [None]
        row = get(repo, task.id)
        assert (row.state, row.claimed_by) == (TaskState.CANCELLED, None)

    def test_claim__leaves_change_seq_and_updated_at_alone(
        self, repo: Repo, queue: TaskQueue, clock: FakeClock
    ) -> None:
        before = get(repo, enqueue(repo, queue).id)
        clock.advance(seconds=5)

        queue.claim(ME, TTL)

        row = get(repo, before.id)
        assert (row.change_seq, row.updated_at) == (before.change_seq, before.updated_at)
        assert clock_value(repo) == before.change_seq


class TestStart__WhenQueuedAndOwned:
    def test_start__sets_running_attempts_and_started_at_and_keeps_error(
        self, repo: Repo, queue: TaskQueue, clock: FakeClock
    ) -> None:
        first = running(repo, queue)
        assert retry(repo, queue, first, run_after=NOW, error="Timeout: slow")
        claimed = queue.claim(ME, TTL)
        assert claimed is not None
        now = clock.advance(seconds=4)

        result = start(repo, queue, claimed)

        assert result is StartResult.OK
        row = get(repo, first.id)
        assert (row.state, row.attempts, row.started_at, row.error) == (
            TaskState.RUNNING,
            2,
            now,
            "Timeout: slow",
        )


class TestStart__WhenTheTaskWasCancelledAfterTheClaim:
    def test_start__changes_nothing_and_returns_cancelled(
        self, repo: Repo, queue: TaskQueue
    ) -> None:
        enqueue(repo, queue)
        claimed = queue.claim(ME, TTL)
        assert claimed is not None
        assert cancel(repo, queue)
        before = get(repo, claimed.id)

        result = start(repo, queue, claimed)

        assert result is StartResult.CANCELLED
        assert get(repo, claimed.id) == before


class TestStart__WhenASiblingIsRunning:
    @pytest.fixture
    def deferred(self, repo: Repo, queue: TaskQueue) -> tuple[TaskRow, StartResult]:
        running(repo, queue)
        successor = enqueue(repo, queue)
        # Claimed before the sibling started (a race the claim's NOT EXISTS cannot rule out).
        with db.write(repo.engine) as conn:
            conn.execute(
                update(tasks)
                .where(tasks.c.id == successor.id)
                .values(claimed_by=OTHER, claimed_until=NOW + timedelta(seconds=TTL))
            )
        return successor, start(repo, queue, get(repo, successor.id), OTHER)

    def test_start__returns_deferred(self, deferred: tuple[TaskRow, StartResult]) -> None:
        assert deferred[1] is StartResult.DEFERRED

    def test_start__releases_the_claim_and_keeps_the_task_queued(
        self, repo: Repo, deferred: tuple[TaskRow, StartResult]
    ) -> None:
        row = get(repo, deferred[0].id)
        assert (row.state, row.claimed_by, row.claimed_until, row.attempts) == (
            TaskState.QUEUED,
            None,
            None,
            0,
        )


class TestStart__WhenTheClaimWasLost:
    @pytest.mark.parametrize("situation", ["lapsed", "taken over", "deleted"])
    def test_start__changes_nothing_and_returns_lost(
        self, repo: Repo, queue: TaskQueue, clock: FakeClock, situation: str
    ) -> None:
        enqueue(repo, queue)
        claimed = queue.claim(ME, TTL)
        assert claimed is not None
        lapse(clock)
        if situation == "taken over":
            assert queue.claim(OTHER, TTL) is not None
        elif situation == "deleted":
            _in_task_transaction(
                repo, queue, NOVEL, lambda conn: queue.delete_for_novel(conn, NOVEL)
            )
        before = find(repo, claimed.id)

        result = start(repo, queue, claimed)

        assert result is StartResult.LOST
        assert find(repo, claimed.id) == before


class TestFinish:
    @pytest.fixture
    def task(self, repo: Repo, queue: TaskQueue, clock: FakeClock) -> TaskRow:
        """A running task that carries an error from an earlier attempt, started at NOW + 1 s."""
        first = running(repo, queue)
        assert retry(repo, queue, first, run_after=NOW, error="Timeout: slow")
        claimed = queue.claim(ME, TTL)
        assert claimed is not None
        clock.advance(seconds=1)
        assert start(repo, queue, claimed) is StartResult.OK
        clock.advance(seconds=2.5)
        return claimed

    @pytest.mark.parametrize(
        ("state", "error", "stored"),
        [
            (TaskState.DONE, None, None),
            (TaskState.FAILED, "Http: 404", "Http: 404"),
            (TaskState.SKIPPED, None, "Timeout: slow"),
        ],
    )
    def test_finish__sets_the_state_and_error(
        self,
        repo: Repo,
        queue: TaskQueue,
        task: TaskRow,
        state: TaskState,
        error: str | None,
        stored: str | None,
    ) -> None:
        finish(repo, queue, task, state, error)

        row = get(repo, task.id)
        assert (row.state, row.error) == (state, stored)

    @pytest.mark.parametrize("state", [TaskState.DONE, TaskState.FAILED, TaskState.SKIPPED])
    def test_finish__sets_finished_at_and_duration_and_clears_the_claim(
        self, repo: Repo, queue: TaskQueue, clock: FakeClock, task: TaskRow, state: TaskState
    ) -> None:
        finish(repo, queue, task, state, "Http: 404")

        row = get(repo, task.id)
        assert (row.finished_at, row.duration_ms) == (clock.current, 2500)
        assert (row.claimed_by, row.claimed_until) == (None, None)

    def test_finish__returns_true(self, repo: Repo, queue: TaskQueue, task: TaskRow) -> None:
        assert finish(repo, queue, task, TaskState.DONE) is True

    @pytest.mark.parametrize("state", [TaskState.QUEUED, TaskState.RUNNING, TaskState.CANCELLED])
    def test_finish__rejects_a_state_that_is_not_an_outcome(
        self, repo: Repo, queue: TaskQueue, task: TaskRow, state: TaskState
    ) -> None:
        with pytest.raises(ValueError, match=state.value):
            finish(repo, queue, task, state)

    def test_finish__rejects_failed_without_an_error(
        self, repo: Repo, queue: TaskQueue, task: TaskRow
    ) -> None:
        with pytest.raises(ValueError, match="error"):
            finish(repo, queue, task, TaskState.FAILED)


class TestFinish__WhenTheClaimWasLost:
    def test_finish__changes_nothing_and_returns_false(
        self, repo: Repo, queue: TaskQueue, clock: FakeClock
    ) -> None:
        task = running(repo, queue)
        lapse(clock)

        assert finish(repo, queue, task, TaskState.DONE) is False
        assert get(repo, task.id) == task


class TestFinish__WhenAnotherWorkerHoldsALiveClaim:
    def test_finish__changes_nothing_and_returns_false(self, repo: Repo, queue: TaskQueue) -> None:
        task = take_over(repo, running(repo, queue))

        assert finish(repo, queue, task, TaskState.DONE) is False
        assert get(repo, task.id) == task


class TestRetry:
    def test_retry__returns_the_row_to_queued_with_run_after_error_and_duration(
        self, repo: Repo, queue: TaskQueue, clock: FakeClock
    ) -> None:
        task = running(repo, queue)
        clock.advance(seconds=1.5)
        later = NOW + timedelta(minutes=10)

        retry(repo, queue, task, run_after=later, error="Timeout: slow")

        row = get(repo, task.id)
        assert (row.state, row.run_after, row.error, row.duration_ms, row.finished_at) == (
            TaskState.QUEUED,
            later,
            "Timeout: slow",
            1500,
            None,
        )
        assert (row.claimed_by, row.claimed_until) == (None, None)

    def test_retry__returns_true(self, repo: Repo, queue: TaskQueue) -> None:
        task = running(repo, queue)

        assert retry(repo, queue, task, run_after=NOW, error="Timeout: slow") is True


class TestRetry__WhenAnotherQueuedTaskAppearedMeanwhile:
    def test_retry__merges_into_it_by_the_enqueue_rules(self, repo: Repo, queue: TaskQueue) -> None:
        task = running(repo, queue, priority=300, emit_event=True)
        successor = enqueue(repo, queue, priority=100, run_after=NOW + timedelta(hours=1))

        retry(repo, queue, task, run_after=NOW + timedelta(minutes=1), error="Timeout: slow")

        row = get(repo, successor.id)
        assert (row.state, row.run_after, row.priority, row.emit_event) == (
            TaskState.QUEUED,
            NOW + timedelta(minutes=1),
            300,
            True,
        )
        assert [t.id for t in queued(repo)] == [successor.id]

    def test_retry__cancels_the_retried_row(
        self, repo: Repo, queue: TaskQueue, clock: FakeClock
    ) -> None:
        task = running(repo, queue)
        enqueue(repo, queue)
        clock.advance(seconds=2)

        retry(repo, queue, task, run_after=NOW, error="Timeout: slow")

        row = get(repo, task.id)
        assert (row.state, row.finished_at, row.claimed_by, row.duration_ms) == (
            TaskState.CANCELLED,
            clock.current,
            None,
            2000,
        )
        assert row.started_at == task.started_at

    def test_retry__copies_the_error_onto_the_successor(
        self, repo: Repo, queue: TaskQueue, clock: FakeClock
    ) -> None:
        task = running(repo, queue)
        successor = enqueue(repo, queue)
        clock.advance(seconds=2)

        retry(repo, queue, task, run_after=NOW + timedelta(hours=1), error="Timeout: slow")

        row = get(repo, successor.id)
        assert (row.error, row.updated_at) == ("Timeout: slow", clock.current)
        assert row.run_after == successor.run_after

    def test_retry__stamps_the_successor_whose_error_changed(
        self, repo: Repo, queue: TaskQueue
    ) -> None:
        task = running(repo, queue)
        successor = get(repo, enqueue(repo, queue).id)

        retry(repo, queue, task, run_after=NOW + timedelta(hours=1), error="Timeout: slow")

        assert get(repo, successor.id).change_seq > successor.change_seq


class TestRetry__WhenTheClaimWasLost:
    def test_retry__returns_false(self, repo: Repo, queue: TaskQueue, clock: FakeClock) -> None:
        task = running(repo, queue)
        lapse(clock)

        assert retry(repo, queue, task, run_after=NOW, error="Timeout: slow") is False
        assert get(repo, task.id) == task


class TestRetry__WhenAnotherWorkerHoldsALiveClaim:
    def test_retry__changes_nothing_and_returns_false(self, repo: Repo, queue: TaskQueue) -> None:
        task = take_over(repo, running(repo, queue))

        assert retry(repo, queue, task, run_after=NOW, error="Timeout: slow") is False
        assert get(repo, task.id) == task

    def test_retry__with_a_queued_successor_changes_nothing_and_returns_false(
        self, repo: Repo, queue: TaskQueue
    ) -> None:
        task = take_over(repo, running(repo, queue))
        successor = get(repo, enqueue(repo, queue, run_after=NOW + timedelta(hours=1)).id)

        assert retry(repo, queue, task, run_after=NOW, error="Timeout: slow") is False
        assert (get(repo, task.id), get(repo, successor.id)) == (task, successor)


class TestCancel:
    def test_cancel__turns_a_queued_task_into_cancelled_and_returns_true(
        self, repo: Repo, queue: TaskQueue, clock: FakeClock
    ) -> None:
        task = enqueue(repo, queue)
        assert queue.claim(ME, TTL) is not None
        now = clock.advance(seconds=1)

        assert cancel(repo, queue) is True

        row = get(repo, task.id)
        assert (row.state, row.finished_at, row.claimed_by, row.claimed_until) == (
            TaskState.CANCELLED,
            now,
            None,
            None,
        )

    def test_cancel__leaves_a_running_task_alone_and_returns_false(
        self, repo: Repo, queue: TaskQueue
    ) -> None:
        task = running(repo, queue)

        assert cancel(repo, queue) is False
        assert get(repo, task.id) == task

    def test_cancel__returns_false_when_no_queued_task_exists(
        self, queue: TaskQueue, repo: Repo
    ) -> None:
        assert cancel(repo, queue) is False


class TestDeleteForNovel:
    def test_delete_for_novel__removes_every_task_of_the_novel_and_leaves_others(
        self, repo: Repo, queue: TaskQueue
    ) -> None:
        running(repo, queue)
        enqueue(repo, queue)
        done = enqueue(repo, queue, DOWNLOAD, chapter(1), priority=100)
        cancel(repo, queue, DOWNLOAD, done.entity_id)
        kept = enqueue(repo, queue, REFRESH, OTHER_NOVEL, OTHER_NOVEL)
        statements: list[str] = []

        def capture(_c: object, _cur: object, statement: str, *_rest: object) -> None:
            statements.append(statement)

        event.listen(repo.engine, "before_cursor_execute", capture)
        try:
            _in_task_transaction(
                repo, queue, NOVEL, lambda conn: queue.delete_for_novel(conn, NOVEL)
            )
        finally:
            event.remove(repo.engine, "before_cursor_execute", capture)

        assert [task.id for task in all_tasks(repo)] == [kept.id]
        assert not any(s.lstrip().upper().startswith("INSERT INTO EVENTS") for s in statements)


class TestRelease:
    def test_release__clears_this_workers_claim_on_a_queued_task(
        self, repo: Repo, queue: TaskQueue
    ) -> None:
        enqueue(repo, queue)
        claimed = queue.claim(ME, TTL)
        assert claimed is not None

        assert queue.release(claimed, ME) is True

        row = get(repo, claimed.id)
        assert (row.claimed_by, row.claimed_until, row.state) == (None, None, TaskState.QUEUED)

    def test_release__returns_false_for_another_owner(self, repo: Repo, queue: TaskQueue) -> None:
        enqueue(repo, queue)
        claimed = queue.claim(ME, TTL)
        assert claimed is not None

        assert queue.release(claimed, OTHER) is False
        assert get(repo, claimed.id).claimed_by == ME

    def test_release__leaves_change_seq_and_updated_at_alone(
        self, repo: Repo, queue: TaskQueue, clock: FakeClock
    ) -> None:
        before = get(repo, enqueue(repo, queue).id)
        claimed = queue.claim(ME, TTL)
        assert claimed is not None
        clock.advance(seconds=5)

        queue.release(claimed, ME)

        row = get(repo, before.id)
        assert (row.change_seq, row.updated_at) == (before.change_seq, before.updated_at)


class TestRenew:
    def test_renew__pushes_claimed_until_for_this_workers_task(
        self, repo: Repo, queue: TaskQueue, clock: FakeClock
    ) -> None:
        task = running(repo, queue)
        now = clock.advance(seconds=100)

        assert queue.renew(task, ME, TTL) is True
        assert get(repo, task.id).claimed_until == now + timedelta(seconds=TTL)

    def test_renew__returns_false_once_the_claim_lapsed(
        self, repo: Repo, queue: TaskQueue, clock: FakeClock
    ) -> None:
        task = running(repo, queue)
        lapse(clock)

        assert queue.renew(task, ME, TTL) is False
        assert get(repo, task.id).claimed_until == task.claimed_until


class TestLapsed:
    def test_lapsed__returns_queued_and_running_tasks_whose_claim_lapsed_and_nothing_else(
        self, repo: Repo, queue: TaskQueue, clock: FakeClock
    ) -> None:
        stuck = running(repo, queue)
        claimed_only = enqueue(repo, queue, DOWNLOAD, chapter(1), priority=100)
        assert queue.claim(ME, TTL) is not None
        enqueue(repo, queue, DOWNLOAD, chapter(2), priority=100, run_after=NOW + timedelta(days=1))
        finished = running(repo, queue, DOWNLOAD, chapter(3), priority=500)
        assert finish(repo, queue, finished, TaskState.DONE)
        lapse(clock)
        live = enqueue(repo, queue, DOWNLOAD, chapter(4), priority=500)
        assert queue.claim(OTHER, TTL) is not None

        lapsed = queue.lapsed(clock.current)

        assert sorted(task.id for task in lapsed) == sorted([stuck.id, claimed_only.id])
        assert live.id not in {task.id for task in lapsed}


def _finished_rows(count: int, *, finished_at: Any, start: int = 0) -> list[dict[str, Any]]:
    return [
        {
            "id": f"TK{start + n:012d}",
            "kind": DOWNLOAD.value,
            "entity_id": chapter(start + n),
            "novel_id": NOVEL,
            "state": TaskState.DONE.value,
            "priority": 100,
            "run_after": NOW,
            "change_seq": 1,
            "created_at": NOW,
            "updated_at": NOW,
            "finished_at": finished_at,
        }
        for n in range(count)
    ]


class TestPrune:
    def test_prune__deletes_finished_tasks_finished_before_the_cutoff_in_batches_of_500(
        self, repo: Repo, queue: TaskQueue
    ) -> None:
        with db.write(repo.engine) as conn:
            conn.execute(insert(tasks), _finished_rows(1001, finished_at=NOW))
        deletes: list[str] = []

        def capture(_c: object, _cur: object, statement: str, *_rest: object) -> None:
            if statement.lstrip().upper().startswith("DELETE FROM TASKS"):
                deletes.append(statement)

        event.listen(repo.engine, "before_cursor_execute", capture)
        try:
            deleted = queue.prune(NOW + timedelta(seconds=1))
        finally:
            event.remove(repo.engine, "before_cursor_execute", capture)

        assert deleted == 1001
        assert all_tasks(repo) == []
        assert len(deletes) == 3

    def test_prune__keeps_live_and_newer_tasks(
        self, repo: Repo, queue: TaskQueue, clock: FakeClock
    ) -> None:
        with db.write(repo.engine) as conn:
            conn.execute(insert(tasks), _finished_rows(1, finished_at=NOW, start=1))
            conn.execute(
                insert(tasks), _finished_rows(1, finished_at=NOW + timedelta(days=1), start=2)
            )
        live = running(repo, queue)
        waiting = enqueue(repo, queue)

        assert queue.prune(NOW + timedelta(hours=1)) == 1

        assert sorted(task.id for task in all_tasks(repo)) == sorted(
            [f"TK{2:012d}", live.id, waiting.id]
        )


def _operations() -> dict[str, Callable[[Repo, TaskQueue], TaskRow]]:
    """Each operation runs one stamped task write and returns the task it changed."""

    def insert_(repo: Repo, queue: TaskQueue) -> TaskRow:
        repo_clock_tick(repo)
        return enqueue(repo, queue)

    def merge(repo: Repo, queue: TaskQueue) -> TaskRow:
        enqueue(repo, queue, run_after=NOW + timedelta(hours=1))
        repo_clock_tick(repo)
        return enqueue(repo, queue, run_after=NOW)

    def start_(repo: Repo, queue: TaskQueue) -> TaskRow:
        enqueue(repo, queue)
        claimed = queue.claim(ME, TTL)
        assert claimed is not None
        repo_clock_tick(repo)
        assert start(repo, queue, claimed) is StartResult.OK
        return claimed

    def finish_(repo: Repo, queue: TaskQueue) -> TaskRow:
        task = running(repo, queue)
        repo_clock_tick(repo)
        assert finish(repo, queue, task, TaskState.DONE)
        return task

    def retry_(repo: Repo, queue: TaskQueue) -> TaskRow:
        task = running(repo, queue)
        repo_clock_tick(repo)
        assert retry(repo, queue, task, run_after=NOW, error="Timeout: slow")
        return task

    def cancel_(repo: Repo, queue: TaskQueue) -> TaskRow:
        task = enqueue(repo, queue)
        repo_clock_tick(repo)
        assert cancel(repo, queue)
        return task

    return {
        "insert": insert_,
        "merge": merge,
        "start": start_,
        "finish": finish_,
        "retry": retry_,
        "cancel": cancel_,
    }


def repo_clock_tick(repo: Repo) -> None:
    """Move the fake clock on, so a write's `updated_at` differs from the setup's."""
    clock = repo.clock
    assert isinstance(clock, FakeClock)
    clock.advance(seconds=7)


OPERATIONS = _operations()


class TestChangeCursor:
    @pytest.mark.parametrize("operation", OPERATIONS)
    def test_change_cursor__every_task_write_sets_change_seq_when_stamp_runs(
        self, repo: Repo, queue: TaskQueue, operation: str
    ) -> None:
        task = OPERATIONS[operation](repo, queue)

        row = get(repo, task.id)
        assert row.change_seq == clock_value(repo)
        assert row.change_seq > 0

    @pytest.mark.parametrize("operation", ["start", "finish", "retry", "cancel"])
    def test_change_cursor__a_write_is_not_stamped_before_stamp_runs(
        self, repo: Repo, queue: TaskQueue, operation: str
    ) -> None:
        task = running(repo, queue) if operation in ("finish", "retry") else enqueue(repo, queue)
        if operation == "start":
            claimed = queue.claim(ME, TTL)
            assert claimed is not None
            task = claimed
        before = get(repo, task.id).change_seq
        writes: dict[str, Callable[[Connection], object]] = {
            "start": lambda conn: queue.start(conn, task, ME),
            "finish": lambda conn: queue.finish(conn, task, ME, TaskState.DONE),
            "retry": lambda conn: queue.retry(conn, task, ME, run_after=NOW, error="E: e"),
            "cancel": lambda conn: queue.cancel(conn, REFRESH, NOVEL),
        }

        with db.write(repo.engine) as conn:
            assert lock_novel(conn, NOVEL)
            writes[operation](conn)
            unstamped = conn.execute(
                select(tasks.c.change_seq).where(tasks.c.id == task.id)
            ).scalar_one()
            queue.stamp(conn)

        assert unstamped == before
        assert get(repo, task.id).change_seq == before + 1

    def test_change_cursor__is_the_transactions_last_event_seq_when_it_appended_events(
        self, repo: Repo, queue: TaskQueue
    ) -> None:
        with db.write(repo.engine) as conn:
            assert lock_novel(conn, NOVEL)
            task = queue.enqueue(conn, REFRESH, NOVEL, NOVEL, priority=300, run_after=NOW)
            event_log.append(conn, NOVEL, NOVEL, {"a": 1}, now=NOW)
            last = event_log.append(conn, NOVEL, NOVEL, {"a": 2}, now=NOW)
            queue.stamp(conn)

        assert get(repo, task.id).change_seq == last
        assert clock_value(repo) == last

    def test_change_cursor__a_transaction_without_events_takes_one_clock_value_of_its_own(
        self, repo: Repo, queue: TaskQueue
    ) -> None:
        before = clock_value(repo)

        with db.write(repo.engine) as conn:
            assert lock_novel(conn, NOVEL)
            first = queue.enqueue(conn, REFRESH, NOVEL, NOVEL, priority=300, run_after=NOW)
            second = queue.enqueue(conn, DOWNLOAD, CHAPTER, NOVEL, priority=100, run_after=NOW)
            queue.stamp(conn)

        assert clock_value(repo) == before + 1
        assert get(repo, first.id).change_seq == get(repo, second.id).change_seq == before + 1
        assert event_count(repo) == 0

    def test_change_cursor__does_not_reuse_an_earlier_transactions_event_seq(
        self, repo: Repo, queue: TaskQueue
    ) -> None:
        with db.write(repo.engine) as conn:
            event_log.append(conn, NOVEL, NOVEL, {}, now=NOW)

        task = enqueue(repo, queue)

        assert get(repo, task.id).change_seq == 2

    def test_change_cursor__a_transaction_without_task_writes_takes_no_clock_value(
        self, repo: Repo, queue: TaskQueue
    ) -> None:
        with db.write(repo.engine) as conn:
            queue.stamp(conn)

        assert clock_value(repo) == 0

    def test_change_cursor__an_inserted_row_never_shows_change_seq_0_to_another_transaction(
        self, repo: Repo, queue: TaskQueue
    ) -> None:
        with db.write(repo.engine) as conn:
            assert lock_novel(conn, NOVEL)
            task = queue.enqueue(conn, REFRESH, NOVEL, NOVEL, priority=300, run_after=NOW)
            seen_during = all_tasks(repo)
            queue.stamp(conn)

        assert seen_during == []
        assert get(repo, task.id).change_seq != 0

    def test_change_cursor__change_seq_order_equals_commit_order(
        self, repo: Repo, queue: TaskQueue, other_repo: Repo, other_queue: TaskQueue
    ) -> None:
        """The second writer waits for the first one's clock lock, so it commits second."""

        def write(target: TaskQueue, novel_id: str) -> Callable[[Connection], TaskRow]:
            def step(conn: Connection) -> TaskRow:
                assert lock_novel(conn, novel_id)
                task = target.enqueue(
                    conn, REFRESH, novel_id, novel_id, priority=300, run_after=NOW
                )
                target.stamp(conn)
                return task

            return step

        first, second = _Ordered(
            repo, other_repo, write(queue, NOVEL), write(other_queue, OTHER_NOVEL)
        ).run()

        assert get(repo, first.id).change_seq < get(repo, second.id).change_seq

    @pytest.mark.parametrize("operation", ["claim", "release", "renew"])
    def test_change_cursor__claim_release_and_renew_do_not_change_it(
        self, repo: Repo, queue: TaskQueue, operation: str
    ) -> None:
        task = enqueue(repo, queue) if operation != "renew" else running(repo, queue)
        if operation == "release":
            claimed = queue.claim(ME, TTL)
            assert claimed is not None
        before = get(repo, task.id)

        if operation == "claim":
            assert queue.claim(ME, TTL) is not None
        elif operation == "release":
            assert queue.release(task, ME)
        else:
            assert queue.renew(task, ME, TTL)

        assert get(repo, task.id).change_seq == before.change_seq
        assert clock_value(repo) == before.change_seq


class TestWrite__WhenATaskWriteWasNotStamped:
    """`db.write` refuses to commit a task write that `stamp` did not follow (change cursor)."""

    @pytest.mark.parametrize("operation", ["enqueue", "start", "finish", "retry", "cancel"])
    def test_write__raises_unstamped_task_write(
        self, repo: Repo, queue: TaskQueue, operation: str
    ) -> None:
        task = running(repo, queue) if operation in ("finish", "retry") else enqueue(repo, queue)
        if operation == "start":
            claimed = queue.claim(ME, TTL)
            assert claimed is not None
            task = claimed
        writes: dict[str, Callable[[Connection], object]] = {
            "enqueue": lambda conn: queue.enqueue(
                conn, DOWNLOAD, CHAPTER, NOVEL, priority=100, run_after=NOW
            ),
            "start": lambda conn: queue.start(conn, task, ME),
            "finish": lambda conn: queue.finish(conn, task, ME, TaskState.DONE),
            "retry": lambda conn: queue.retry(conn, task, ME, run_after=NOW, error="E: e"),
            "cancel": lambda conn: queue.cancel(conn, REFRESH, NOVEL),
        }
        before = all_tasks(repo)

        with pytest.raises(db.UnstampedTaskWriteError), db.write(repo.engine) as conn:
            assert lock_novel(conn, NOVEL)
            writes[operation](conn)

        assert all_tasks(repo) == before

    def test_write__raises_when_a_task_write_follows_stamp(
        self, repo: Repo, queue: TaskQueue
    ) -> None:
        with pytest.raises(db.UnstampedTaskWriteError), db.write(repo.engine) as conn:
            assert lock_novel(conn, NOVEL)
            queue.enqueue(conn, REFRESH, NOVEL, NOVEL, priority=300, run_after=NOW)
            queue.stamp(conn)
            queue.enqueue(conn, DOWNLOAD, CHAPTER, NOVEL, priority=100, run_after=NOW)

        assert all_tasks(repo) == []

    def test_write__commits_a_transaction_that_touched_no_task_without_stamp(
        self, repo: Repo, queue: TaskQueue
    ) -> None:
        task = enqueue(repo, queue)

        with db.write(repo.engine) as conn:
            assert lock_novel(conn, NOVEL)
            assert queue.queued(conn, REFRESH, NOVEL) is not None

        assert all_tasks(repo) == [get(repo, task.id)]


class TestUpdatedAt:
    @pytest.mark.parametrize("operation", OPERATIONS)
    def test_updated_at__is_set_by_the_same_writes_from_the_writers_clock(
        self, repo: Repo, queue: TaskQueue, clock: FakeClock, operation: str
    ) -> None:
        task = OPERATIONS[operation](repo, queue)

        assert get(repo, task.id).updated_at == clock.current
        assert clock.current > NOW


class TestLockOrder:
    """The second transaction waits on `lock_novel` (Postgres) or the write lock (SQLite).

    `DELETE /novels` is simulated by `lock_novel` + `delete_for_novel`; `Novels.delete` adopts it
    in a later task.
    """

    @pytest.fixture
    def claimed(self, repo: Repo, queue: TaskQueue) -> TaskRow:
        enqueue(repo, queue)
        task = queue.claim(ME, TTL)
        assert task is not None
        return task

    @staticmethod
    def _start(queue: TaskQueue, task: TaskRow) -> Callable[[Connection], StartResult]:
        def step(conn: Connection) -> StartResult:
            assert lock_novel(conn, task.novel_id)
            result = queue.start(conn, task, ME)
            queue.stamp(conn)
            return result

        return step

    @staticmethod
    def _delete(queue: TaskQueue) -> Callable[[Connection], None]:
        def step(conn: Connection) -> None:
            assert lock_novel(conn, NOVEL)
            queue.delete_for_novel(conn, NOVEL)

        return step

    def test_lock_order__start_then_novel_delete_both_commit_without_deadlock(
        self,
        repo: Repo,
        queue: TaskQueue,
        other_repo: Repo,
        other_queue: TaskQueue,
        claimed: TaskRow,
    ) -> None:
        started, _ = _Ordered(
            repo, other_repo, self._start(queue, claimed), self._delete(other_queue)
        ).run()

        assert started is StartResult.OK
        assert all_tasks(repo) == []

    def test_lock_order__novel_delete_then_start_returns_lost(
        self,
        repo: Repo,
        queue: TaskQueue,
        other_repo: Repo,
        other_queue: TaskQueue,
        claimed: TaskRow,
    ) -> None:
        _, started = _Ordered(
            repo, other_repo, self._delete(queue), self._start(other_queue, claimed)
        ).run()

        assert started is StartResult.LOST
        assert all_tasks(repo) == []

    @pytest.mark.parametrize("retry_first", [True, False])
    def test_lock_order__retry_against_a_concurrent_enqueue_merge_leaves_one_queued_row(
        self,
        repo: Repo,
        queue: TaskQueue,
        other_repo: Repo,
        other_queue: TaskQueue,
        retry_first: bool,
    ) -> None:
        task = running(repo, queue)

        def retry_step(conn: Connection) -> bool:
            assert lock_novel(conn, NOVEL)
            result = queue.retry(conn, task, ME, run_after=NOW, error="Timeout: slow")
            queue.stamp(conn)
            return result

        def enqueue_step(conn: Connection) -> TaskRow:
            assert lock_novel(conn, NOVEL)
            row = other_queue.enqueue(conn, REFRESH, NOVEL, NOVEL, priority=300, run_after=NOW)
            other_queue.stamp(conn)
            return row

        steps = (retry_step, enqueue_step) if retry_first else (enqueue_step, retry_step)
        retried, _ = _Ordered(repo, other_repo, *steps).run()[:: 1 if retry_first else -1]

        assert retried is True
        assert len(queued(repo)) == 1


class TestListPollingWithoutGaps:
    """`Tasks.list(since=...)` against a writer that commits late (tasks spec section 7)."""

    def test_list__a_task_finished_by_a_transaction_that_commits_late_is_in_the_next_poll(
        self, repo: Repo, queue: TaskQueue, other_repo: Repo
    ) -> None:
        if repo.engine.dialect.name != "postgresql":
            pytest.skip("needs concurrent readers during an open write transaction (Postgres)")
        task = running(repo, queue)
        with db.write(repo.engine) as conn:
            assert lock_novel(conn, NOVEL)
            assert queue.finish(conn, task, ME, TaskState.DONE)
            queue.stamp(conn)
            before = other_repo.tasks.list(since=None)  # the writer has not committed yet
        # Once done, the task is no longer live: only `change_seq > since` can bring it back.
        after = other_repo.tasks.list(since=before.next_since)

        assert [(item.id, item.state) for item in before.items] == [(task.id, TaskState.RUNNING)]
        assert [(item.id, item.state) for item in after.items] == [(task.id, TaskState.DONE)]
        assert after.items[0].change_seq > before.next_since


# --- The public start, finish and recovery path (tasks spec sections 5 and 8) ---------------


@pytest.fixture
def handler() -> RecordingHandler:
    return RecordingHandler()


@pytest.fixture
def handled(
    repo: Repo,
    queue: TaskQueue,
    migrated_database: str,
    clock: FakeClock,
    handler: RecordingHandler,
) -> Iterator[Repo]:
    """A `Repo` with the recording handler registered for `REFRESH`, on the emptied database."""
    handled_repo = Repo(migrated_database, clock=clock, handlers={REFRESH: handler})
    yield handled_repo
    handled_repo.engine.dispose()


def claimed_task(repo: Repo, queue: TaskQueue, worker: str = ME, **options: Any) -> TaskRow:
    """Enqueue a refresh task for NOVEL and claim it."""
    task = enqueue(repo, queue, **options)
    claimed = queue.claim(worker, TTL)
    assert claimed is not None and claimed.id == task.id
    return claimed


def started_task(handled: Repo, queue: TaskQueue) -> TaskRow:
    """Enqueue, claim and start a refresh task for NOVEL through the public path."""
    outcome = handled.tasks.start(claimed_task(handled, queue), ME)
    assert isinstance(outcome, Ready)
    return outcome.task


def novel_snapshot(repo: Repo) -> Any:
    with db.read(repo.engine) as conn:
        return conn.execute(select(novels).where(novels.c.id == NOVEL)).one()


class TestTasksClaim:
    def test_claim__claims_the_first_eligible_task_for_the_worker(
        self, handled: Repo, queue: TaskQueue
    ) -> None:
        task = enqueue(handled, queue)

        claimed = handled.tasks.claim(ME, TTL)

        assert claimed is not None and claimed.id == task.id
        assert claimed.claimed_by == ME

    def test_claim__returns_none_when_nothing_is_claimable(self, handled: Repo) -> None:
        assert handled.tasks.claim(ME, TTL) is None


class TestTasksReleaseAndRenew:
    def test_release__clears_this_workers_claim(self, handled: Repo, queue: TaskQueue) -> None:
        task = claimed_task(handled, queue)

        assert handled.tasks.release(task, ME) is True
        assert get(handled, task.id).claimed_by is None

    def test_renew__pushes_claimed_until(
        self, handled: Repo, queue: TaskQueue, clock: FakeClock
    ) -> None:
        task = started_task(handled, queue)
        now = clock.advance(seconds=100)

        assert handled.tasks.renew(task, ME, TTL) is True
        assert get(handled, task.id).claimed_until == now + timedelta(seconds=TTL)


class TestTasksStart__WhenTheQueueStartIsOkAndBeginSaysStarted:
    def test_start__sets_the_task_running(self, handled: Repo, queue: TaskQueue) -> None:
        task = claimed_task(handled, queue)

        handled.tasks.start(task, ME)

        row = get(handled, task.id)
        assert (row.state, row.attempts, row.started_at) == (TaskState.RUNNING, 1, NOW)

    def test_start__returns_ready_with_the_handlers_work_item_and_the_started_task(
        self, handled: Repo, queue: TaskQueue
    ) -> None:
        task = claimed_task(handled, queue)

        outcome = handled.tasks.start(task, ME)

        assert outcome == Ready(work="https://example.com/novel", task=get(handled, task.id))

    def test_start__runs_begin_and_work_in_starts_transaction(
        self, handled: Repo, queue: TaskQueue, handler: RecordingHandler
    ) -> None:
        task = claimed_task(handled, queue)

        handled.tasks.start(task, ME)

        assert handler.calls == [
            ("begin", task.id, TaskState.RUNNING),
            ("work", task.id, TaskState.RUNNING),
        ]

    def test_start__rolls_back_with_the_handler(
        self, handled: Repo, queue: TaskQueue, handler: RecordingHandler
    ) -> None:
        task = claimed_task(handled, queue)
        handler.fail_in = "begin"

        with pytest.raises(Busy):
            handled.tasks.start(task, ME)

        row = get(handled, task.id)
        assert (row.state, row.attempts, row.claimed_by) == (TaskState.QUEUED, 0, ME)


class TestTasksStart__WhenBeginSaysSkip:
    def test_start__skips_the_task_with_finished_at(
        self, handled: Repo, queue: TaskQueue, handler: RecordingHandler
    ) -> None:
        handler.begin_result = Skip("novel gone")
        task = claimed_task(handled, queue)

        handled.tasks.start(task, ME)

        row = get(handled, task.id)
        assert (row.state, row.finished_at, row.claimed_by) == (TaskState.SKIPPED, NOW, None)

    def test_start__returns_skipped_with_the_reason(
        self, handled: Repo, queue: TaskQueue, handler: RecordingHandler
    ) -> None:
        handler.begin_result = Skip("novel gone")

        assert handled.tasks.start(claimed_task(handled, queue), ME) == Skipped("novel gone")

    def test_start__leaves_the_entity_unchanged_and_reads_no_work(
        self, handled: Repo, queue: TaskQueue, handler: RecordingHandler
    ) -> None:
        handler.begin_result = Skip("novel gone")
        task = claimed_task(handled, queue)
        before = novel_snapshot(handled)

        handled.tasks.start(task, ME)

        assert novel_snapshot(handled) == before
        assert handler.hooks == ["begin"]


class TestTasksStart__WhenTheQueueStartIsNotOk:
    @pytest.fixture(params=["cancelled", "deferred", "lost"])
    def case(
        self, request: pytest.FixtureRequest, handled: Repo, queue: TaskQueue, clock: FakeClock
    ) -> tuple[TaskRow, StartResult]:
        if request.param == "deferred":
            # A sibling runs while this row stays queued and claimed: start the sibling first.
            sibling = claimed_task(handled, queue)
            assert start(handled, queue, sibling) is StartResult.OK
            task = enqueue(handled, queue)
            return force_claim(
                handled, task.id, ME, NOW + timedelta(seconds=TTL)
            ), StartResult.DEFERRED
        task = claimed_task(handled, queue)
        if request.param == "cancelled":
            assert cancel(handled, queue)
            return task, StartResult.CANCELLED
        lapse(clock)
        return task, StartResult.LOST

    def test_start__returns_not_started_with_that_result(
        self, handled: Repo, case: tuple[TaskRow, StartResult]
    ) -> None:
        task, result = case

        assert handled.tasks.start(task, ME) == NotStarted(result)

    def test_start__does_not_call_begin(
        self, handled: Repo, handler: RecordingHandler, case: tuple[TaskRow, StartResult]
    ) -> None:
        task, _ = case

        handled.tasks.start(task, ME)

        assert handler.calls == []


class TestTasksStart__WhenTheNovelIsGone:
    def test_start__returns_not_started_lost(
        self, handled: Repo, queue: TaskQueue, handler: RecordingHandler
    ) -> None:
        task = claimed_task(handled, queue)
        with db.write(handled.engine) as conn:
            assert lock_novel(conn, NOVEL)
            queue.delete_for_novel(conn, NOVEL)
            conn.execute(delete(novels).where(novels.c.id == NOVEL))

        assert handled.tasks.start(task, ME) == NotStarted(StartResult.LOST)
        assert handler.calls == []


class TestTasksStart__WithoutARegisteredHandler:
    def test_start__raises_and_leaves_the_task_alone(
        self, repo: Repo, queue: TaskQueue, migrated_database: str, clock: FakeClock
    ) -> None:
        task = claimed_task(repo, queue)
        unhandled = Repo(migrated_database, clock=clock, handlers={})

        try:
            with pytest.raises(UnregisteredTaskKindError):
                unhandled.tasks.start(task, ME)
        finally:
            unhandled.engine.dispose()

        assert get(repo, task.id) == task


class TestTasksFinish__WhenFenced:
    def test_finish__done_ends_the_task_done(self, handled: Repo, queue: TaskQueue) -> None:
        task = started_task(handled, queue)

        finished = handled.tasks.finish(task, ME, Done("ok"))

        row = get(handled, task.id)
        assert (row.state, row.error, row.finished_at) == (TaskState.DONE, None, NOW)
        assert finished == Finished(row)

    def test_finish__failed_ends_the_task_failed_with_its_error(
        self, handled: Repo, queue: TaskQueue
    ) -> None:
        task = started_task(handled, queue)

        finished = handled.tasks.finish(task, ME, Failed("SiteError: 404"))

        row = get(handled, task.id)
        assert (row.state, row.error) == (TaskState.FAILED, "SiteError: 404")
        assert finished == Finished(row)

    def test_finish__retry_requeues_the_task_at_run_after(
        self, handled: Repo, queue: TaskQueue
    ) -> None:
        task = started_task(handled, queue)
        later = NOW + timedelta(hours=6)

        finished = handled.tasks.finish(task, ME, Retry(later, "InfrastructureError: dns"))

        row = get(handled, task.id)
        assert finished == Finished(row)
        assert (row.state, row.run_after, row.error, row.claimed_by) == (
            TaskState.QUEUED,
            later,
            "InfrastructureError: dns",
            None,
        )

    @pytest.mark.parametrize(
        ("outcome", "state"),
        [
            (Done("ok"), TaskState.DONE),
            (Failed("SiteError: 404"), TaskState.FAILED),
            (Retry(NOW, "InfrastructureError: dns"), TaskState.QUEUED),
        ],
    )
    def test_finish__calls_complete_with_the_outcome_in_the_same_transaction(
        self,
        handled: Repo,
        queue: TaskQueue,
        handler: RecordingHandler,
        outcome: Any,
        state: TaskState,
    ) -> None:
        task = started_task(handled, queue)

        handled.tasks.finish(task, ME, outcome)

        assert handler.calls[-1] == ("complete", task.id, state)
        assert handler.outcomes == [outcome]

    def test_finish__rolls_back_with_complete(
        self, handled: Repo, queue: TaskQueue, handler: RecordingHandler
    ) -> None:
        task = started_task(handled, queue)
        handler.fail_in = "complete"

        with pytest.raises(Busy):
            handled.tasks.finish(task, ME, Done("ok"))

        assert get(handled, task.id) == task


class TestTasksFinish__WhenTheClaimWasLost:
    @pytest.mark.parametrize(
        "outcome", [Done("ok"), Failed("SiteError: 404"), Retry(NOW, "InfrastructureError: dns")]
    )
    def test_finish__returns_none_changes_nothing_and_skips_complete(
        self,
        handled: Repo,
        queue: TaskQueue,
        handler: RecordingHandler,
        clock: FakeClock,
        outcome: Any,
    ) -> None:
        task = started_task(handled, queue)
        lapse(clock)
        handler.calls.clear()

        assert handled.tasks.finish(task, ME, outcome) is None
        assert get(handled, task.id) == task
        assert handler.calls == []


class TestTasksFinish__WhenTheNovelIsGone:
    def test_finish__returns_none(self, handled: Repo, queue: TaskQueue) -> None:
        task = started_task(handled, queue)
        with db.write(handled.engine) as conn:
            assert lock_novel(conn, NOVEL)
            queue.delete_for_novel(conn, NOVEL)
            conn.execute(delete(novels).where(novels.c.id == NOVEL))

        assert handled.tasks.finish(task, ME, Done("ok")) is None


class TestTasksRecover:
    def test_recover__returns_a_lapsed_running_task_to_queued_with_run_after_kept(
        self, handled: Repo, queue: TaskQueue, clock: FakeClock
    ) -> None:
        task = started_task(handled, queue)
        lapse(clock)

        report = handled.tasks.recover()

        row = get(handled, task.id)
        assert (row.state, row.run_after, row.claimed_by, row.finished_at) == (
            TaskState.QUEUED,
            task.run_after,
            None,
            None,
        )
        assert report == TaskRecoveryReport(
            claims_cleared=0, requeued=1, merged=0, recovered=(task,)
        )

    def test_recover__calls_handler_recover_after_the_task_is_queued(
        self, handled: Repo, queue: TaskQueue, handler: RecordingHandler, clock: FakeClock
    ) -> None:
        task = started_task(handled, queue)
        lapse(clock)

        handled.tasks.recover()

        assert handler.calls[-1] == ("recover", task.id, TaskState.QUEUED)

    def test_recover__clears_only_the_claim_of_a_lapsed_claimed_queued_task(
        self, handled: Repo, queue: TaskQueue, handler: RecordingHandler, clock: FakeClock
    ) -> None:
        task = claimed_task(handled, queue)
        lapse(clock)

        report = handled.tasks.recover()

        assert get(handled, task.id) == replace(task, claimed_by=None, claimed_until=None)
        assert report == TaskRecoveryReport(claims_cleared=1, requeued=0, merged=0)
        assert handler.calls == []

    def test_recover__merges_a_recovered_task_into_its_queued_sibling_and_cancels_it(
        self, handled: Repo, queue: TaskQueue, handler: RecordingHandler, clock: FakeClock
    ) -> None:
        task = started_task(handled, queue)
        sibling = enqueue(handled, queue, run_after=NOW + timedelta(hours=1))
        lapse(clock)

        report = handled.tasks.recover()

        row = get(handled, task.id)
        assert (row.state, row.finished_at, row.claimed_by) == (
            TaskState.CANCELLED,
            clock.current,
            None,
        )
        assert [t.id for t in queued(handled)] == [sibling.id]
        assert get(handled, sibling.id).run_after == task.run_after
        assert report == TaskRecoveryReport(
            claims_cleared=0, requeued=0, merged=1, recovered=(task,)
        )
        assert handler.calls[-1] == ("recover", task.id, TaskState.CANCELLED)

    def test_recover__leaves_live_claims_alone(
        self, handled: Repo, queue: TaskQueue, handler: RecordingHandler, clock: FakeClock
    ) -> None:
        task = started_task(handled, queue)
        other = enqueue(handled, queue, DOWNLOAD, chapter(1), priority=100)
        assert queue.claim(ME, TTL) is not None
        other = get(handled, other.id)
        clock.advance(seconds=TTL - 1)
        handler.calls.clear()

        report = handled.tasks.recover()

        assert (get(handled, task.id), get(handled, other.id)) == (task, other)
        assert report == TaskRecoveryReport(claims_cleared=0, requeued=0, merged=0)
        assert handler.calls == []

    def test_recover__decides_on_the_row_read_in_its_transaction_not_the_lapsed_snapshot(
        self, handled: Repo, queue: TaskQueue, handler: RecordingHandler, clock: FakeClock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:  # fmt: skip
        claimed = claimed_task(handled, queue)
        lapse(clock)
        stale = handled.tasks._queue.lapsed(clock())  # noqa: SLF001  (queued, claim lapsed)
        clock.advance(seconds=-(TTL + 1))
        assert isinstance(handled.tasks.start(claimed, ME), Ready)
        task = get(handled, claimed.id)
        lapse(clock)
        monkeypatch.setattr(handled.tasks._queue, "lapsed", lambda now: stale)  # noqa: SLF001

        report = handled.tasks.recover()

        assert [t.state for t in stale] == [TaskState.QUEUED]
        assert handler.calls[-1] == ("recover", task.id, TaskState.QUEUED)
        assert report == TaskRecoveryReport(
            claims_cleared=0, requeued=1, merged=0, recovered=(task,)
        )

    def test_recover__stamps_the_recovered_task(
        self, handled: Repo, queue: TaskQueue, clock: FakeClock
    ) -> None:
        task = started_task(handled, queue)
        lapse(clock)
        before = clock_value(handled)

        handled.tasks.recover()

        assert get(handled, task.id).change_seq > before
        assert get(handled, task.id).updated_at == clock.current


class TestEnqueueMany:
    def test_enqueue_many__inserts_one_queued_task_per_entity_with_its_ordinal(
        self, repo: Repo, queue: TaskQueue, clock: FakeClock
    ) -> None:
        _in_task_transaction(
            repo,
            queue,
            NOVEL,
            lambda conn: enqueue_many(
                conn,
                DOWNLOAD,
                NOVEL,
                [(chapter(1), 1), (chapter(2), 2)],
                priority=100,
                run_after=NOW,
                now=clock(),
            ),  # fmt: skip
        )

        rows = sorted(all_tasks(repo), key=lambda row: row.ordinal)
        assert [(r.entity_id, r.ordinal, r.priority, r.state) for r in rows] == [
            (chapter(1), 1, 100, TaskState.QUEUED),
            (chapter(2), 2, 100, TaskState.QUEUED),
        ]
        assert {(r.run_after, r.created_at) for r in rows} == {(NOW, clock())}

    def test_enqueue_many__merges_into_an_existing_queued_task(
        self, repo: Repo, queue: TaskQueue, clock: FakeClock
    ) -> None:
        existing = enqueue(repo, queue, DOWNLOAD, chapter(1), priority=100, run_after=NOW)

        _in_task_transaction(
            repo,
            queue,
            NOVEL,
            lambda conn: enqueue_many(
                conn,
                DOWNLOAD,
                NOVEL,
                [(chapter(1), 1)],
                priority=110,
                run_after=NOW - timedelta(minutes=1),
                now=clock(),
            ),  # fmt: skip
        )

        [row] = all_tasks(repo)
        assert (row.id, row.priority, row.run_after) == (
            existing.id, 110, NOW - timedelta(minutes=1),
        )  # fmt: skip

    def test_enqueue_many__stamps_every_inserted_row(
        self, repo: Repo, queue: TaskQueue, clock: FakeClock
    ) -> None:
        _in_task_transaction(
            repo,
            queue,
            NOVEL,
            lambda conn: enqueue_many(
                conn,
                DOWNLOAD,
                NOVEL,
                [(chapter(1), 1), (chapter(2), 2)],
                priority=100,
                run_after=NOW,
                now=clock(),
            ),  # fmt: skip
        )

        assert {row.change_seq for row in all_tasks(repo)} == {repo.events.latest_seq()}
        assert repo.events.latest_seq() > 0


class TestCancelMany:
    def test_cancel_many__cancels_the_queued_tasks_and_leaves_a_running_one(
        self, repo: Repo, queue: TaskQueue, clock: FakeClock
    ) -> None:
        queued = enqueue(repo, queue, DOWNLOAD, chapter(1), priority=100, run_after=NOW)
        active = running(repo, queue, DOWNLOAD, chapter(2))
        now = clock.advance(seconds=1)

        _in_task_transaction(
            repo,
            queue,
            NOVEL,
            lambda conn: cancel_many(conn, DOWNLOAD, [chapter(1), chapter(2)], now=now),
        )

        assert (get(repo, queued.id).state, get(repo, queued.id).finished_at) == (
            TaskState.CANCELLED, now,
        )  # fmt: skip
        assert get(repo, active.id).state is TaskState.RUNNING
