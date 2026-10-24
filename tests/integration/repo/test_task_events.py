"""Task events: `TaskQueue.stamp` appends `TK` events (tasks spec sections 4 and 7)."""

from collections.abc import Callable
from datetime import timedelta

import pytest
from sqlalchemy import insert, select

from novelhog.domain.tasks import StartResult, TaskState
from novelhog.repo import Repo, db
from novelhog.repo import events as event_log
from novelhog.repo.novels import lock_novel
from novelhog.repo.schema import novels, tasks
from novelhog.repo.tasks import TaskQueue, TaskRow
from tests.integration.helpers import NOW, insert_host, novel_row
from tests.integration.repo.test_tasks_queue import (
    CHAPTER,
    DOWNLOAD,
    ME,
    NOVEL,
    REFRESH,
    TTL,
    cancel,
    clock_value,
    enqueue,
    event_count,
    finish,
    get,
    retry,
    running,
    start,
)


@pytest.fixture
def queue(repo: Repo) -> TaskQueue:
    with db.write(repo.engine) as conn:
        insert_host(conn)
        conn.execute(insert(novels).values(**novel_row()))
    return TaskQueue(repo.context)


def log(repo: Repo) -> list[event_log.EventRow]:
    return repo.events.since(0)


def task_events(repo: Repo) -> list[event_log.EventRow]:
    return [row for row in log(repo) if row.entity_id.startswith("TK")]


Operation = Callable[[Repo, TaskQueue], TaskRow]


def _do_enqueue(repo: Repo, queue: TaskQueue) -> TaskRow:
    return enqueue(repo, queue, emit_event=True)


def _do_merge(repo: Repo, queue: TaskQueue) -> TaskRow:
    enqueue(repo, queue, emit_event=True, run_after=NOW + timedelta(hours=1))
    return enqueue(repo, queue, emit_event=True, run_after=NOW)


def _do_start(repo: Repo, queue: TaskQueue) -> TaskRow:
    task = enqueue(repo, queue, emit_event=True)
    claimed = queue.claim(ME, TTL)
    assert claimed is not None
    assert start(repo, queue, claimed) is StartResult.OK
    return task


def _do_finish(repo: Repo, queue: TaskQueue) -> TaskRow:
    task = running(repo, queue, emit_event=True)
    assert finish(repo, queue, task, TaskState.DONE)
    return task


def _do_skip(repo: Repo, queue: TaskQueue) -> TaskRow:
    task = running(repo, queue, emit_event=True)
    assert finish(repo, queue, task, TaskState.SKIPPED)
    return task


def _do_retry(repo: Repo, queue: TaskQueue) -> TaskRow:
    task = running(repo, queue, emit_event=True)
    assert retry(repo, queue, task, run_after=NOW + timedelta(minutes=5), error="Timeout: slow")
    return task


def _do_cancel(repo: Repo, queue: TaskQueue) -> TaskRow:
    task = enqueue(repo, queue, emit_event=True)
    assert cancel(repo, queue)
    return task


OPERATIONS: dict[str, Operation] = {
    "enqueue": _do_enqueue,
    "merge": _do_merge,
    "start": _do_start,
    "finish": _do_finish,
    "retry": _do_retry,
    "skip": _do_skip,
    "cancel": _do_cancel,
}


def _last_event(repo: Repo) -> event_log.EventRow:
    return log(repo)[-1]


class TestWhenEmitEventIsTrue:
    @pytest.mark.parametrize("operation", ["enqueue", "start", "finish", "retry", "skip", "cancel"])
    def test_stamp__the_operations_own_transaction_adds_exactly_one_event(
        self, repo: Repo, queue: TaskQueue, operation: str
    ) -> None:
        # setup runs its own stamped transactions; count only the last one
        setups: dict[str, Callable[[], object]] = {
            "enqueue": lambda: None,
            "start": lambda: enqueue(repo, queue, emit_event=True),
            "finish": lambda: running(repo, queue, emit_event=True),
            "retry": lambda: running(repo, queue, emit_event=True),
            "skip": lambda: running(repo, queue, emit_event=True),
            "cancel": lambda: enqueue(repo, queue, emit_event=True),
        }
        setups[operation]()
        before = event_count(repo)
        task = _operate(repo, queue, operation)
        assert event_count(repo) == before + 1
        last = _last_event(repo)
        assert (last.entity_id, last.kind, last.novel_id) == (task.id, "task", NOVEL)

    def test_stamp__a_merge_appends_one_event(self, repo: Repo, queue: TaskQueue) -> None:
        enqueue(repo, queue, emit_event=True, run_after=NOW + timedelta(hours=1))
        before = event_count(repo)
        enqueue(repo, queue, emit_event=True, run_after=NOW)
        assert event_count(repo) == before + 1

    def test_stamp__a_task_event_follows_the_transactions_entity_events(
        self, repo: Repo, queue: TaskQueue
    ) -> None:
        with db.write(repo.engine) as conn:
            assert lock_novel(conn, NOVEL)
            task = queue.enqueue(
                conn, REFRESH, NOVEL, NOVEL, priority=300, run_after=NOW, emit_event=True
            )
            first = event_log.append(conn, NOVEL, NOVEL, {"a": 1}, now=NOW)
            second = event_log.append(conn, NOVEL, CHAPTER, {"a": 2}, now=NOW)
            queue.stamp(conn)

        rows = log(repo)
        assert [row.seq for row in rows] == [first, second, second + 1]
        assert rows[-1].entity_id == task.id

    def test_stamp__a_retry_that_merges_appends_one_event_per_changed_row(
        self, repo: Repo, queue: TaskQueue
    ) -> None:
        task = running(repo, queue, emit_event=True, run_after=NOW)
        successor = enqueue(
            repo, queue, emit_event=True, run_after=NOW + timedelta(hours=2), priority=300
        )
        before = event_count(repo)

        assert retry(repo, queue, task, run_after=NOW + timedelta(hours=1), error="E: e")

        added = log(repo)[before:]
        assert [row.entity_id for row in added] == [task.id, successor.id]
        assert [row.payload["state"] for row in added] == ["cancelled", "queued"]

    @pytest.mark.parametrize("operation", OPERATIONS)
    def test_stamp__the_payload_equals_the_task_object_read_after_commit(
        self, repo: Repo, queue: TaskQueue, operation: str
    ) -> None:
        task = OPERATIONS[operation](repo, queue)

        event = _last_event(repo)

        assert event.payload == get(repo, task.id).to_view().to_json()
        assert event.payload["change_seq"] == event.seq

    @pytest.mark.parametrize("operation", OPERATIONS)
    def test_stamp__the_rows_change_seq_equals_its_event_seq(
        self, repo: Repo, queue: TaskQueue, operation: str
    ) -> None:
        task = OPERATIONS[operation](repo, queue)

        assert get(repo, task.id).change_seq == _last_event(repo).seq


def _operate(repo: Repo, queue: TaskQueue, operation: str) -> TaskRow:
    """Run only the operation under test, on the task its setup left."""
    if operation == "enqueue":
        return enqueue(repo, queue, emit_event=True)
    if operation == "start":
        claimed = queue.claim(ME, TTL)
        assert claimed is not None
        assert start(repo, queue, claimed) is StartResult.OK
        return claimed
    task = _the_task(repo)
    if operation == "cancel":
        assert cancel(repo, queue)
    elif operation == "retry":
        assert retry(repo, queue, task, run_after=NOW + timedelta(minutes=5), error="E: e")
    else:
        state = TaskState.DONE if operation == "finish" else TaskState.SKIPPED
        assert finish(repo, queue, task, state)
    return task


def _the_task(repo: Repo) -> TaskRow:
    with db.read(repo.engine) as conn:
        return TaskRow.from_row(conn.execute(select(tasks)).one())


class TestWhenEmitEventIsFalse:
    def test_stamp__appends_no_task_event_and_takes_one_clock_value(
        self, repo: Repo, queue: TaskQueue
    ) -> None:
        task = enqueue(repo, queue)

        assert event_count(repo) == 0
        assert get(repo, task.id).change_seq == clock_value(repo) == 1

    def test_stamp__the_change_seq_is_the_transactions_last_event_seq(
        self, repo: Repo, queue: TaskQueue
    ) -> None:
        with db.write(repo.engine) as conn:
            assert lock_novel(conn, NOVEL)
            task = queue.enqueue(conn, REFRESH, NOVEL, NOVEL, priority=300, run_after=NOW)
            event_log.append(conn, NOVEL, NOVEL, {"a": 1}, now=NOW)
            last = event_log.append(conn, NOVEL, NOVEL, {"a": 2}, now=NOW)
            queue.stamp(conn)

        assert get(repo, task.id).change_seq == last
        assert task_events(repo) == []


class TestMixedRows:
    def test_stamp__quiet_rows_take_the_last_seq_including_task_events(
        self, repo: Repo, queue: TaskQueue
    ) -> None:
        with db.write(repo.engine) as conn:
            assert lock_novel(conn, NOVEL)
            loud = queue.enqueue(
                conn, REFRESH, NOVEL, NOVEL, priority=300, run_after=NOW, emit_event=True
            )
            quiet = queue.enqueue(conn, DOWNLOAD, CHAPTER, NOVEL, priority=100, run_after=NOW)
            queue.stamp(conn)

        (event,) = task_events(repo)
        assert event.entity_id == loud.id
        assert get(repo, loud.id).change_seq == event.seq
        assert get(repo, quiet.id).change_seq == event.seq == clock_value(repo)

    def test_stamp__task_events_come_in_touch_order(self, repo: Repo, queue: TaskQueue) -> None:
        with db.write(repo.engine) as conn:
            assert lock_novel(conn, NOVEL)
            second = queue.enqueue(
                conn, DOWNLOAD, CHAPTER, NOVEL, priority=100, run_after=NOW, emit_event=True
            )
            first = queue.enqueue(
                conn, REFRESH, NOVEL, NOVEL, priority=300, run_after=NOW, emit_event=True
            )
            queue.stamp(conn)

        assert [row.entity_id for row in task_events(repo)] == [second.id, first.id]


class TestClaimReleaseRenew:
    def test_claim_release_and_renew__append_nothing(self, repo: Repo, queue: TaskQueue) -> None:
        task = enqueue(repo, queue, emit_event=True)
        before = event_count(repo)
        clock = clock_value(repo)

        claimed = queue.claim(ME, TTL)
        assert claimed is not None
        assert queue.renew(claimed, ME, TTL)
        assert queue.release(claimed, ME)

        assert event_count(repo) == before
        assert clock_value(repo) == clock
        assert get(repo, task.id).change_seq == clock
