"""The task queue: one `tasks` table for all background work (tasks spec sections 3 and 4).

`TaskQueue` is internal to `repo`. Its methods that take a `Connection` write inside the caller's
transaction, which follows the lock order (tasks spec section 4, "Lock order"): `lock_novel`
first, then every entity and task row write, then the events, then `TaskQueue.stamp` last. The
module functions `enqueue` and `cancel` are the same writes with the writer's `now` given, for
entity helpers that take a `Connection` and a time but hold no `TaskQueue` (`exports`).
`claim`, `release`, `renew`, `lapsed` and `prune` are their own transactions and are exempt.
`queued`, `has_task` and `queued_unneeded` are the reads and conditions the handlers' repair checks
share.

Change cursor: every write that changes a field of the Task object records the row as touched in
the transaction (see `db.transaction_info`); `stamp` then appends a `TK` event for each touched row
with `emit_event` and sets `change_seq` on the touched rows: to the row's own `TK` seq, else to the
transaction's last event seq, or to a clock value of its own if it appended no events.
Inserted rows carry `change_seq = 0` until then, which no other transaction can see.
`db.write` rolls back a transaction that touched a row after its last `stamp`
(`db.UnstampedTaskWriteError`), so a writer that forgets `stamp` cannot commit a stale cursor.

`Tasks` is the public side (`repo.tasks`). Its `start`, `finish` and `recover` each compose one
transaction per task in that order: `lock_novel`, the queue primitive, the kind's `TaskHandler`
hook (tasks spec section 5), then `stamp`. `repair` scans with each handler's `problems` in a plain
read, then runs each fix in its own transaction: `lock_novel`, the handler's `fix` (which re-checks
the problem), then `stamp`. A task whose kind has no registered handler is a
programming error: those methods raise `UnregisteredTaskKindError` and write nothing.
"""

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    ColumnElement,
    Connection,
    Row,
    Table,
    and_,
    delete,
    exists,
    func,
    insert,
    or_,
    select,
    update,
)

from novelhog import ids
from novelhog.domain.errors import NotFound
from novelhog.domain.payloads import TaskView
from novelhog.domain.tasks import (
    LIVE_STATES,
    Failed,
    Outcome,
    Retry,
    Skip,
    StartResult,
    TaskKind,
    TaskState,
)
from novelhog.ids import new_id
from novelhog.repo import claims, db, events, novels
from novelhog.repo.context import RepoContext
from novelhog.repo.schema import event_clock, tasks
from novelhog.repo.schema import novels as novels_table
from novelhog.repo.task_kinds import HandlerRegistry, TaskHandler, default_handlers

PRUNE_BATCH = 500
_IN_BATCH = 500
DEFAULT_LIMIT = 50
MAX_LIMIT = 500
_OUTCOMES = frozenset({TaskState.DONE, TaskState.FAILED, TaskState.SKIPPED})


class Recovered(StrEnum):
    """What `TaskQueue.recover` did to a task whose claim lapsed."""

    CLAIM_CLEARED = "claim_cleared"
    REQUEUED = "requeued"
    MERGED = "merged"


@dataclass(frozen=True)
class TaskRow:
    """One `tasks` row with every column."""

    id: str
    kind: TaskKind
    entity_id: str
    novel_id: str
    state: TaskState
    priority: int
    ordinal: int
    run_after: datetime
    payload: dict[str, object]
    emit_event: bool
    attempts: int
    error: str | None
    change_seq: int
    claimed_by: str | None
    claimed_until: datetime | None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    duration_ms: int | None

    @classmethod
    def from_row(cls, row: Row[Any]) -> TaskRow:
        """Build a `TaskRow` from a selected or returned `tasks` row."""
        values = dict(row._mapping)
        return cls(
            **values
            | {
                "kind": TaskKind(values["kind"]),
                "state": TaskState(values["state"]),
                "payload": dict(values["payload"]),
                "emit_event": bool(values["emit_event"]),
            }
        )

    def to_view(self) -> TaskView:
        """Return the API `Task` object (tasks spec section 7)."""
        return TaskView(
            id=self.id,
            kind=self.kind,
            entity_id=self.entity_id,
            novel_id=self.novel_id,
            state=self.state,
            attempts=self.attempts,
            error=self.error,
            run_after=self.run_after,
            change_seq=self.change_seq,
            created_at=self.created_at,
            updated_at=self.updated_at,
            started_at=self.started_at,
            finished_at=self.finished_at,
            duration_ms=self.duration_ms,
        )


def _touch(conn: Connection, task_id: str) -> None:
    """Record that this transaction changed a field of the task's Task object."""
    touched: dict[str, None] = db.transaction_info(conn).setdefault(db.TASKS_TOUCHED, {})
    touched[task_id] = None


def _find(conn: Connection, kind: TaskKind, entity_id: str, state: TaskState) -> TaskRow | None:
    row = conn.execute(
        select(tasks).where(
            tasks.c.kind == kind.value, tasks.c.entity_id == entity_id, tasks.c.state == state.value
        )
    ).first()
    return None if row is None else TaskRow.from_row(row)


def _get(conn: Connection, task_id: str) -> TaskRow | None:
    row = conn.execute(select(tasks).where(tasks.c.id == task_id)).first()
    return None if row is None else TaskRow.from_row(row)


def _require(conn: Connection, task_id: str) -> TaskRow:
    """The task this transaction just wrote; it cannot have vanished."""
    task = _get(conn, task_id)
    if task is None:
        message = f"task {task_id} vanished inside its own transaction"
        raise RuntimeError(message)
    return task


def _running_sibling() -> ColumnElement[bool]:
    """A running task of the same `(kind, entity_id)` exists."""
    sibling = tasks.alias("sibling")
    return exists().where(
        sibling.c.kind == tasks.c.kind,
        sibling.c.entity_id == tasks.c.entity_id,
        sibling.c.state == TaskState.RUNNING.value,
    )


def _duration_ms(task: TaskRow, now: datetime) -> int | None:
    if task.started_at is None:
        return None
    return (now - task.started_at) // timedelta(milliseconds=1)


def _released(now: datetime) -> dict[str, object]:
    return {"claimed_by": None, "claimed_until": None, "updated_at": now}


@dataclass(frozen=True)
class Recovery:
    """What `TaskQueue.recover` did, and the task as its transaction read it before the reset."""

    result: Recovered
    task: TaskRow


def enqueue(  # noqa: PLR0913  (the enqueue parameters of tasks spec section 4)
    conn: Connection,
    kind: TaskKind,
    entity_id: str,
    novel_id: str,
    *,
    priority: int,
    ordinal: int = 0,
    run_after: datetime,
    emit_event: bool = False,
    payload: Mapping[str, object] = {},  # noqa: B006  (read-only default)
    now: datetime,
) -> TaskRow:
    """Insert a queued task, or merge into the queued task of `(kind, entity_id)`.

    The caller's transaction holds `lock_novel` and calls `TaskQueue.stamp` last. A merge sets
    `run_after = min`, `priority = max` and `emit_event = OR`, and keeps the claim, `created_at`
    and `payload`. The returned row's `change_seq` is the value before `stamp` (0 for an insert).
    `now` is the writer's clock (`created_at`, `updated_at`).
    """
    current = _find(conn, kind, entity_id, TaskState.QUEUED)
    if current is not None:
        return _merge(
            conn,
            current,
            priority=priority,
            run_after=run_after,
            emit_event=emit_event,
            now=now,
        )
    row = conn.execute(
        insert(tasks)
        .values(
            id=new_id("TK"),
            kind=kind.value,
            entity_id=entity_id,
            novel_id=novel_id,
            state=TaskState.QUEUED.value,
            priority=priority,
            ordinal=ordinal,
            run_after=run_after,
            payload=dict(payload),
            emit_event=emit_event,
            attempts=0,
            change_seq=0,
            created_at=now,
            updated_at=now,
        )
        .returning(*tasks.c)
    ).one()
    task = TaskRow.from_row(row)
    _touch(conn, task.id)
    return task


def enqueue_many(  # noqa: PLR0913  (the enqueue parameters of tasks spec section 4)
    conn: Connection,
    kind: TaskKind,
    novel_id: str,
    entities: Sequence[tuple[str, int]],
    *,
    priority: int,
    run_after: datetime,
    emit_event: bool = False,
    now: datetime,
) -> None:
    """`enqueue` for many `(entity_id, ordinal)` of one novel, with batched reads and inserts.

    Same rules as `enqueue`: an entity with a queued task merges into it, the others get a new
    queued task each. The caller holds `lock_novel` and calls `TaskQueue.stamp` last.
    """
    for start in range(0, len(entities), _IN_BATCH):
        batch = entities[start : start + _IN_BATCH]
        queued = {
            task.entity_id: task
            for task in (
                TaskRow.from_row(row)
                for row in conn.execute(
                    select(tasks).where(
                        tasks.c.kind == kind.value,
                        tasks.c.entity_id.in_([entity_id for entity_id, _ in batch]),
                        tasks.c.state == TaskState.QUEUED.value,
                    )
                )
            )
        }
        rows: list[dict[str, object]] = []
        for entity_id, ordinal in batch:
            current = queued.get(entity_id)
            if current is not None:
                _merge(
                    conn,
                    current,
                    priority=priority,
                    run_after=run_after,
                    emit_event=emit_event,
                    now=now,
                )
                continue
            rows.append(
                {
                    "id": new_id("TK"),
                    "kind": kind.value,
                    "entity_id": entity_id,
                    "novel_id": novel_id,
                    "state": TaskState.QUEUED.value,
                    "priority": priority,
                    "ordinal": ordinal,
                    "run_after": run_after,
                    "payload": {},
                    "emit_event": emit_event,
                    "attempts": 0,
                    "change_seq": 0,
                    "created_at": now,
                    "updated_at": now,
                }
            )
        if rows:
            conn.execute(insert(tasks), rows)
            for row in rows:
                _touch(conn, str(row["id"]))


def _merge(
    conn: Connection,
    current: TaskRow,
    *,
    priority: int,
    run_after: datetime,
    emit_event: bool,
    now: datetime,
) -> TaskRow:
    """Merge an enqueue into the queued task `current`; only a new `run_after` is visible."""
    values: dict[str, object] = {
        "priority": max(current.priority, priority),
        "emit_event": current.emit_event or emit_event,
    }
    moved = run_after < current.run_after
    if moved:
        values |= {"run_after": run_after, "updated_at": now}
    row = conn.execute(
        update(tasks).where(tasks.c.id == current.id).values(values).returning(*tasks.c)
    ).one()
    if moved:
        _touch(conn, current.id)
    return TaskRow.from_row(row)


def cancel(conn: Connection, kind: TaskKind, entity_id: str, *, now: datetime) -> bool:
    """Turn the queued task of `(kind, entity_id)` into `cancelled`; a running one stays.

    The caller's transaction holds `lock_novel` and calls `TaskQueue.stamp` last.
    """
    task_id = conn.execute(
        update(tasks)
        .where(
            tasks.c.kind == kind.value,
            tasks.c.entity_id == entity_id,
            tasks.c.state == TaskState.QUEUED.value,
        )
        .values(state=TaskState.CANCELLED.value, finished_at=now, **_released(now))
        .returning(tasks.c.id)
    ).scalar_one_or_none()
    if task_id is None:
        return False
    _touch(conn, task_id)
    return True


def cancel_many(
    conn: Connection, kind: TaskKind, entity_ids: Sequence[str], *, now: datetime
) -> None:
    """`cancel` for many entities: each queued task of `kind` becomes `cancelled`."""
    for start in range(0, len(entity_ids), _IN_BATCH):
        cancelled = conn.execute(
            update(tasks)
            .where(
                tasks.c.kind == kind.value,
                tasks.c.entity_id.in_(entity_ids[start : start + _IN_BATCH]),
                tasks.c.state == TaskState.QUEUED.value,
            )
            .values(state=TaskState.CANCELLED.value, finished_at=now, **_released(now))
            .returning(tasks.c.id)
        ).scalars()
        for task_id in cancelled:
            _touch(conn, task_id)


def queued(conn: Connection, kind: TaskKind, entity_id: str) -> TaskRow | None:
    """The queued task of `(kind, entity_id)`, if there is one."""
    return _find(conn, kind, entity_id, TaskState.QUEUED)


def has_task(
    kind: TaskKind, entity_id: ColumnElement[Any], states: Iterable[TaskState]
) -> ColumnElement[bool]:
    """A task of `kind` for `entity_id` (a column of the enclosing query) is in one of `states`."""
    other = tasks.alias("other_task")
    return exists().where(
        other.c.kind == kind.value,
        other.c.entity_id == entity_id,
        other.c.state.in_([state.value for state in states]),
    )


def queued_unneeded(kind: TaskKind, entity: Table, needed: Iterable[str]) -> ColumnElement[bool]:
    """A queued `kind` task whose entity is gone or in none of the `needed` states (repair).

    A task whose entity has a running task of the kind is left out: repair never touches the
    entity of a running task (tasks spec section 8).
    """
    return and_(
        tasks.c.kind == kind.value,
        tasks.c.state == TaskState.QUEUED.value,
        ~exists().where(entity.c.id == tasks.c.entity_id, entity.c.state.in_(list(needed))),
        ~_running_sibling(),
    )


@dataclass(frozen=True)
class Problem:
    """A broken entity/task pair a handler's repair scan found (tasks spec section 8).

    `name` is the stable `snake_case` problem name; `task_id` names the queued task concerned,
    when the problem is about one.
    """

    name: str
    entity_id: str
    novel_id: str
    task_id: str | None = None


@dataclass(frozen=True)
class Repair:
    """One applied repair: the problem's name, the task enqueued or cancelled, and the entity.

    `kind` and `attempts` are read in the fix's transaction, so the worker logs `task_repaired`
    without another read; `task_id` and `attempts` are None when no task was involved.
    """

    problem: str
    task_id: str | None
    entity_id: str
    kind: TaskKind
    attempts: int | None

    @classmethod
    def of(cls, problem: Problem, kind: TaskKind, task: TaskRow | None) -> Repair:
        """The repair of `problem` by the fix that enqueued or cancelled `task` (or none)."""
        return cls(
            problem=problem.name,
            task_id=None if task is None else task.id,
            entity_id=problem.entity_id,
            kind=kind,
            attempts=None if task is None else task.attempts,
        )


class TaskQueue:
    """Queue primitives over the `tasks` table (tasks spec section 4, "Operations")."""

    def __init__(self, context: RepoContext) -> None:
        self._engine = context.engine
        self._clock = context.clock

    def enqueue(
        self,
        conn: Connection,
        kind: TaskKind,
        entity_id: str,
        novel_id: str,
        *,
        priority: int,
        ordinal: int = 0,
        run_after: datetime,
        emit_event: bool = False,
        payload: Mapping[str, object] = {},  # noqa: B006  (read-only default)
    ) -> TaskRow:
        """Insert a queued task, or merge into the queued one; see the module function `enqueue`."""
        return enqueue(
            conn,
            kind,
            entity_id,
            novel_id,
            priority=priority,
            ordinal=ordinal,
            run_after=run_after,
            emit_event=emit_event,
            payload=payload,
            now=self._clock(),
        )

    def queued(self, conn: Connection, kind: TaskKind, entity_id: str) -> TaskRow | None:
        """The queued task of `(kind, entity_id)`, if there is one."""
        return queued(conn, kind, entity_id)

    def cancel(self, conn: Connection, kind: TaskKind, entity_id: str) -> bool:
        """Turn the queued task of `(kind, entity_id)` into `cancelled`; a running one stays."""
        return cancel(conn, kind, entity_id, now=self._clock())

    def delete_for_novel(self, conn: Connection, novel_id: str) -> None:
        """Delete every task of the novel in `id` order (`DELETE /novels`); no cursor, no events."""
        while _delete_batch(conn, tasks.c.novel_id == novel_id) == PRUNE_BATCH:
            pass

    def claim(self, worker_id: str, ttl: int) -> TaskRow | None:
        """Claim the first eligible queued task for `ttl` seconds (tasks spec 4, "Ordering").

        The task stays queued. Writes no events and leaves `change_seq` and `updated_at` alone.
        """
        now = self._clock()
        due = and_(tasks.c.state == TaskState.QUEUED.value, tasks.c.run_after <= now)
        with db.write(self._engine) as conn:
            rows = claims.claim(
                conn,
                tasks,
                eligible=and_(due, ~_running_sibling()),
                priority=[tasks.c.priority.desc(), tasks.c.run_after, tasks.c.ordinal],
                owner=worker_id,
                now=now,
                ttl=timedelta(seconds=ttl),
                returning=list(tasks.c),
                recheck=due,
            )
        return TaskRow.from_row(rows[0]) if rows else None

    def start(self, conn: Connection, task: TaskRow, worker_id: str) -> StartResult:
        """Move the claimed, queued task to `running` unless a sibling of it is running.

        `OK`: `attempts + 1` and `started_at = now`, `error` kept. `CANCELLED`: the task was
        cancelled after the claim. `DEFERRED`: a sibling is running; the claim is released and the
        task stays queued. `LOST`: the claim lapsed, was taken over or the row is gone.
        """
        now = self._clock()
        started = claims.fenced_update(
            conn,
            tasks,
            task.id,
            owner=worker_id,
            now=now,
            values={
                "state": TaskState.RUNNING.value,
                "attempts": tasks.c.attempts + 1,
                "started_at": now,
                "updated_at": now,
            },
            where=and_(tasks.c.state == TaskState.QUEUED.value, ~_running_sibling()),
        )
        if started:
            _touch(conn, task.id)
            return StartResult.OK
        current = _get(conn, task.id)
        if current is None:
            return StartResult.LOST
        if current.state is TaskState.CANCELLED:
            return StartResult.CANCELLED
        released = claims.release(
            conn,
            tasks,
            [task.id],
            owner=worker_id,
            now=now,
            where=tasks.c.state == TaskState.QUEUED.value,
        )
        return StartResult.DEFERRED if released else StartResult.LOST

    def _owned_running(
        self, conn: Connection, task_id: str, worker_id: str, now: datetime
    ) -> TaskRow | None:
        row = conn.execute(
            select(tasks).where(
                tasks.c.id == task_id,
                tasks.c.state == TaskState.RUNNING.value,
                claims.owned(tasks, worker_id, now),
            )
        ).first()
        return None if row is None else TaskRow.from_row(row)

    def finish(
        self,
        conn: Connection,
        task: TaskRow,
        worker_id: str,
        state: TaskState,
        error: str | None = None,
    ) -> bool:
        """Fenced: end the running task as `done`, `failed` or `skipped`; False if not owned.

        `done` clears `error`; `failed` stores `error` (required); `skipped` stores `error` if
        given and otherwise keeps the last one. Sets `finished_at` and `duration_ms` and clears
        the claim.
        """
        if state not in _OUTCOMES:
            message = f"finish takes done, failed or skipped, not {state.value}"
            raise ValueError(message)
        if state is TaskState.FAILED and error is None:
            message = "a failed task needs an error"
            raise ValueError(message)
        now = self._clock()
        current = self._owned_running(conn, task.id, worker_id, now)
        if current is None:
            return False
        values: dict[str, object] = {
            "state": state.value,
            "finished_at": now,
            "duration_ms": _duration_ms(current, now),
            **_released(now),
        }
        if state is TaskState.DONE:
            values["error"] = None
        elif error is not None:
            values["error"] = error
        if not _fenced_running_update(conn, task.id, worker_id, now, values):
            return False
        _touch(conn, task.id)
        return True

    def retry(
        self, conn: Connection, task: TaskRow, worker_id: str, *, run_after: datetime, error: str
    ) -> bool:
        """Fenced: put the running task back in the queue at `run_after`; False if not owned.

        The same row returns to `queued` with `error` and the run's `duration_ms`. If another task
        was queued for the entity meanwhile, the row merges into it (enqueue rules) and becomes
        `cancelled` instead.
        """
        now = self._clock()
        current = self._owned_running(conn, task.id, worker_id, now)
        if current is None:
            return False
        ended: dict[str, object] = {
            "error": error,
            "duration_ms": _duration_ms(current, now),
            **_released(now),
        }
        successor = _find(conn, current.kind, current.entity_id, TaskState.QUEUED)
        if successor is None:
            ended |= {"state": TaskState.QUEUED.value, "run_after": run_after}
        else:
            ended |= {"state": TaskState.CANCELLED.value, "finished_at": now}
        if not _fenced_running_update(conn, task.id, worker_id, now, ended):
            return False
        _touch(conn, task.id)
        if successor is not None:
            _merge(
                conn,
                successor,
                priority=current.priority,
                run_after=run_after,
                emit_event=current.emit_event,
                now=now,
            )
        return True

    def release(self, task: TaskRow, worker_id: str) -> bool:
        """Fenced: clear this worker's claim on the queued task; the cursor is not changed."""
        now = self._clock()
        with db.write(self._engine) as conn:
            released = claims.release(
                conn,
                tasks,
                [task.id],
                owner=worker_id,
                now=now,
                where=tasks.c.state == TaskState.QUEUED.value,
            )
        return released == 1

    def renew(self, task: TaskRow, worker_id: str, ttl: int) -> bool:
        """Heartbeat: push `claimed_until` to now + `ttl` seconds; False once the claim is lost."""
        now = self._clock()
        with db.write(self._engine) as conn:
            renewed = claims.renew(
                conn, tasks, [task.id], owner=worker_id, now=now, ttl=timedelta(seconds=ttl)
            )
        return renewed == 1

    def recover(self, conn: Connection, task: TaskRow) -> Recovery | None:
        """Recover one task whose claim lapsed (tasks spec section 8, "Recovery").

        A queued task: the claim is cleared (`CLAIM_CLEARED`; not a change of the Task object). A
        running task: back to `queued` with `run_after` kept and the claim cleared (`REQUEUED`);
        if another task is queued for the entity, the row merges into it (enqueue rules) and
        becomes `cancelled` instead (`MERGED`). The decision is made on the row as this
        transaction reads it, which the result carries; `task` only names it. Returns None when
        the claim is no longer lapsed (taken over or renewed meanwhile) or the row is gone.
        """
        now = self._clock()
        lapsed = and_(
            tasks.c.id == task.id, tasks.c.claimed_by.is_not(None), tasks.c.claimed_until < now
        )
        row = conn.execute(select(tasks).where(lapsed)).first()
        if row is None:
            return None
        current = TaskRow.from_row(row)
        if current.state is TaskState.QUEUED:
            cleared = conn.execute(
                update(tasks)
                .where(lapsed, tasks.c.state == TaskState.QUEUED.value)
                .values(claimed_by=None, claimed_until=None)
            ).rowcount
            return Recovery(Recovered.CLAIM_CLEARED, current) if cleared == 1 else None
        if current.state is not TaskState.RUNNING:
            return None
        successor = _find(conn, current.kind, current.entity_id, TaskState.QUEUED)
        values = _released(now)
        if successor is None:
            values["state"] = TaskState.QUEUED.value
        else:
            values |= {"state": TaskState.CANCELLED.value, "finished_at": now}
        reset = conn.execute(
            update(tasks).where(lapsed, tasks.c.state == TaskState.RUNNING.value).values(values)
        ).rowcount
        if reset != 1:
            return None
        _touch(conn, task.id)
        if successor is None:
            return Recovery(Recovered.REQUEUED, current)
        _merge(
            conn,
            successor,
            priority=current.priority,
            run_after=current.run_after,
            emit_event=current.emit_event,
            now=now,
        )
        return Recovery(Recovered.MERGED, current)

    def lapsed(self, now: datetime) -> list[TaskRow]:
        """Return the queued and running tasks whose claim lapsed before `now`, in `id` order."""
        with db.read(self._engine) as conn:
            rows = claims.expired(conn, tasks, now)
        return sorted((TaskRow.from_row(row) for row in rows), key=lambda task: task.id)

    def prune(self, before: datetime) -> int:
        """Delete finished tasks with `finished_at < before`, in `id` order, in batches of 500.

        Each batch is its own transaction. Returns the number of deleted tasks.
        """
        condition = tasks.c.finished_at < before
        deleted = 0
        while True:
            with db.write(self._engine) as conn:
                count = _delete_batch(conn, condition)
            deleted += count
            if count < PRUNE_BATCH:
                return deleted

    def stamp(self, conn: Connection) -> None:
        """Write the task events and the change cursor; call it last in a task-writing transaction.

        Appends one `TK` event per touched row with `emit_event`, in touch order; the row's
        `change_seq` is the seq of its own event. The other touched rows take the transaction's
        last event seq (the `TK` events included), or one clock value of their own if it appended
        no events. The touched rows are already locked by this transaction, so no new row lock is
        taken.
        """
        touched: dict[str, None] = db.transaction_info(conn).pop(db.TASKS_TOUCHED, {})
        if not touched:
            return
        now = self._clock()
        rows = {
            row.id: row
            for row in conn.execute(
                select(tasks.c.id, tasks.c.novel_id, tasks.c.emit_event).where(
                    tasks.c.id.in_(list(touched))
                )
            )
        }
        quiet: list[str] = []
        for task_id in touched:
            row = rows[task_id]
            if row.emit_event:
                events.append(conn, row.novel_id, task_id, _event_payload(conn, task_id), now=now)
            else:
                quiet.append(task_id)
        if not quiet:
            return
        seq = events.last_seq(conn)
        if seq is None:
            seq = events.take_clock(conn)
        conn.execute(update(tasks).where(tasks.c.id.in_(quiet)).values(change_seq=seq))


def _event_payload(conn: Connection, task_id: str) -> Callable[[], dict[str, object]]:
    """The payload of a task's `TK` event, built once `events.append` took the seq.

    It sets the row's `change_seq` to that seq, then returns the row's `Task` object.
    """

    def build() -> dict[str, object]:
        seq = events.last_seq(conn)
        if seq is None:
            message = "the event payload was built before the clock was taken"
            raise RuntimeError(message)
        conn.execute(update(tasks).where(tasks.c.id == task_id).values(change_seq=seq))
        return _require(conn, task_id).to_view().to_json()

    return build


def _fenced_running_update(
    conn: Connection, task_id: str, worker_id: str, now: datetime, values: Mapping[str, object]
) -> bool:
    """Apply `values` only while the task is running and owned by `worker_id` (v1 fencing)."""
    return claims.fenced_update(
        conn,
        tasks,
        task_id,
        owner=worker_id,
        now=now,
        values=values,
        where=tasks.c.state == TaskState.RUNNING.value,
    )


def _delete_batch(conn: Connection, condition: ColumnElement[bool]) -> int:
    """Delete up to `PRUNE_BATCH` tasks matching `condition`, lowest `id` first."""
    ids = list(
        conn.execute(
            select(tasks.c.id).where(condition).order_by(tasks.c.id).limit(PRUNE_BATCH)
        ).scalars()
    )
    if ids:
        conn.execute(delete(tasks).where(tasks.c.id.in_(ids)))
    return len(ids)


@dataclass(frozen=True)
class TaskPage:
    """A list of tasks in claim order, the number of matches and the cursor to poll from."""

    items: list[TaskView]
    total: int
    next_since: int


class UnregisteredTaskKindError(LookupError):
    """A task's kind has no registered handler (repo) or processor (worker): a programming error."""

    def __init__(self, kind: TaskKind, half: str = "handler") -> None:
        super().__init__(f"no task {half} is registered for kind {kind.value}")
        self.kind = kind


@dataclass(frozen=True)
class Ready[W]:
    """The task is running: `work` is the handler's work item, `task` the started row."""

    work: W
    task: TaskRow


@dataclass(frozen=True)
class Skipped:
    """The handler's `begin` found the work no longer needed; the task is `skipped`."""

    reason: str


@dataclass(frozen=True)
class NotStarted:
    """The queue did not start the task (`CANCELLED`, `DEFERRED` or `LOST`)."""

    result: StartResult


type StartOutcome[W] = Ready[W] | Skipped | NotStarted


@dataclass(frozen=True)
class Finished:
    """A fenced finish: the task row as its transaction left it, and the handler's report.

    `report` is what the handler's `complete` returned (None for kinds that report nothing).
    """

    task: TaskRow
    report: object = None


@dataclass(frozen=True)
class TaskRecoveryReport:
    """What `Tasks.recover` did: claims cleared, running tasks requeued or merged.

    `recovered` holds the running tasks put back (requeued or merged), as recovery's
    transaction read them before the reset.
    """

    claims_cleared: int
    requeued: int
    merged: int
    recovered: tuple[TaskRow, ...] = ()


class Tasks:
    """The public tasks sub-repository (`repo.tasks`).

    Runs tasks for the worker (claim, start, finish, release, renew, recover; tasks spec
    sections 5, 6 and 8) and serves the read API of section 7.
    """

    def __init__(self, context: RepoContext, handlers: HandlerRegistry | None = None) -> None:
        self._engine = context.engine
        self._clock = context.clock
        self._queue = TaskQueue(context)
        registry = default_handlers(context) if handlers is None else handlers
        for kind, handler in registry.items():
            if handler.kind is not kind:
                message = f"handler for {handler.kind.value} registered as {kind.value}"
                raise ValueError(message)
        self._handlers = dict(registry)

    def _handler(self, kind: TaskKind) -> TaskHandler[Any]:
        handler = self._handlers.get(kind)
        if handler is None:
            raise UnregisteredTaskKindError(kind)
        return handler

    def claim(self, worker_id: str, ttl: int) -> TaskRow | None:
        """Claim the first eligible queued task for `ttl` seconds; None when there is none."""
        return self._queue.claim(worker_id, ttl)

    def release(self, task: TaskRow, worker_id: str) -> bool:
        """Clear this worker's claim on the claimed, unstarted task; False if not held."""
        return self._queue.release(task, worker_id)

    def renew(self, task: TaskRow, worker_id: str, ttl: int) -> bool:
        """Heartbeat: extend this worker's claim by `ttl` seconds; False once it is lost."""
        return self._queue.renew(task, worker_id, ttl)

    def host_of(self, task: TaskRow) -> str | None:
        """The host of the task's novel, for per-host politeness; None when the novel is gone."""
        with db.read(self._engine) as conn:
            host = conn.execute(
                select(novels_table.c.host).where(novels_table.c.id == task.novel_id)
            ).scalar_one_or_none()
        return None if host is None else str(host)

    def start(self, task: TaskRow, worker_id: str) -> StartOutcome[Any]:
        """Start the claimed task and its handler in one transaction (tasks spec section 5).

        `NotStarted(LOST)` when the novel is gone; `NotStarted(result)` when the queue did not
        start it; `Skipped(reason)` when the handler's `begin` said `Skip` (the task is then
        `skipped`); else `Ready` with the handler's work item, read after `begin`.
        """
        handler = self._handler(task.kind)
        with db.write(self._engine) as conn:
            if not novels.lock_novel(conn, task.novel_id):
                return NotStarted(StartResult.LOST)
            now = self._clock()
            result = self._queue.start(conn, task, worker_id)
            if result is not StartResult.OK:
                self._queue.stamp(conn)
                return NotStarted(result)
            started = _require(conn, task.id)
            begin = handler.begin(conn, started, now)
            if isinstance(begin, Skip):
                self._queue.finish(conn, started, worker_id, TaskState.SKIPPED)
                self._queue.stamp(conn)
                return Skipped(begin.reason)
            work = handler.work(conn, started)
            self._queue.stamp(conn)
            return Ready(work, _require(conn, task.id))

    def finish(self, task: TaskRow, worker_id: str, outcome: Outcome[Any]) -> Finished | None:
        """Fenced: record the run's outcome and let the handler complete it, in one transaction.

        `Retry` puts the task back in the queue (`TaskQueue.retry`); `Done` and `Failed` end it.
        The handler's `complete` runs only when that write applied. Returns the task row as the
        transaction left it (its stored `duration_ms` included); None, with nothing changed,
        when the claim was lost or the novel is gone.
        """
        handler = self._handler(task.kind)
        with db.write(self._engine) as conn:
            if not novels.lock_novel(conn, task.novel_id):
                return None
            now = self._clock()
            if isinstance(outcome, Retry):
                fenced = self._queue.retry(
                    conn, task, worker_id, run_after=outcome.run_after, error=outcome.error
                )
            elif isinstance(outcome, Failed):
                fenced = self._queue.finish(conn, task, worker_id, TaskState.FAILED, outcome.error)
            else:
                fenced = self._queue.finish(conn, task, worker_id, TaskState.DONE)
            if not fenced:
                return None
            report = handler.complete(conn, task, outcome, now)
            self._queue.stamp(conn)
            return Finished(_require(conn, task.id), report)

    def recover(self) -> TaskRecoveryReport:
        """Recover every task whose claim lapsed, each in its own transaction (tasks spec 8).

        A running task goes back to the queue (or merges into a queued sibling), then its
        handler's `recover` resets the entity. A task whose novel is gone is skipped.
        """
        counts = dict.fromkeys(Recovered, 0)
        recovered: list[TaskRow] = []
        for lapsed in self._queue.lapsed(self._clock()):
            with db.write(self._engine) as conn:
                if not novels.lock_novel(conn, lapsed.novel_id):
                    continue
                now = self._clock()
                recovery = self._queue.recover(conn, lapsed)
                if recovery is None:
                    continue
                if recovery.result is not Recovered.CLAIM_CLEARED:
                    # Decided on the row read in this transaction: `lapsed` may be stale.
                    self._handler(recovery.task.kind).recover(conn, recovery.task, now)
                    recovered.append(recovery.task)
                self._queue.stamp(conn)
            counts[recovery.result] += 1
        return TaskRecoveryReport(
            claims_cleared=counts[Recovered.CLAIM_CLEARED],
            requeued=counts[Recovered.REQUEUED],
            merged=counts[Recovered.MERGED],
            recovered=tuple(recovered),
        )

    def repair(self) -> list[Repair]:
        """Find and fix the entity/task pairs that do not hold (tasks spec sections 2 and 8).

        Each handler scans for its problems in a plain read, in registry order, so a later scan
        sees the earlier fixes. Each fix then runs in its own transaction under `lock_novel`; the
        handler re-checks the problem there and leaves it when it no longer holds. A running task
        counts as present for its entity, so neither it nor its entity is touched. Returns the
        applied repairs, for the worker to log.
        """
        repairs: list[Repair] = []
        for handler in self._handlers.values():
            with db.read(self._engine) as conn:
                problems = handler.problems(conn)
            for problem in problems:
                with db.write(self._engine) as conn:
                    if not novels.lock_novel(conn, problem.novel_id):
                        continue
                    repair = handler.fix(conn, problem, self._clock())
                    self._queue.stamp(conn)
                if repair is not None:
                    repairs.append(repair)
        return repairs

    def prune(self, before: datetime) -> int:
        """Delete finished tasks that finished before `before`; return how many (retention)."""
        return self._queue.prune(before)

    def get(self, task_id: str) -> TaskView:
        """Return one task; raise `NotFound` for an unknown, malformed or pruned ID."""
        if not ids.is_valid(task_id, "TK"):
            raise NotFound("task", task_id)
        with db.read(self._engine) as conn:
            task = _get(conn, task_id)
        if task is None:
            raise NotFound("task", task_id)
        return task.to_view()

    def list(  # noqa: PLR0913  (the filters of `GET /tasks`)
        self,
        *,
        novel_id: str | None = None,
        entity_id: str | None = None,
        kinds: Sequence[TaskKind] = (),
        states: Sequence[TaskState] = (),
        since: int | None = None,
        offset: int = 0,
        limit: int = DEFAULT_LIMIT,
    ) -> TaskPage:
        """Return matching tasks in claim order, their count and the clock read before the query.

        With `since`, the result is every live task plus every task with `change_seq > since`,
        unpaged (`offset` and `limit` are ignored).
        """
        where: list[ColumnElement[bool]] = []
        if novel_id is not None:
            where.append(tasks.c.novel_id == novel_id)
        if entity_id is not None:
            where.append(tasks.c.entity_id == entity_id)
        if kinds:
            where.append(tasks.c.kind.in_([kind.value for kind in kinds]))
        if states:
            where.append(tasks.c.state.in_([state.value for state in states]))
        if since is not None:
            live = tasks.c.state.in_([state.value for state in LIVE_STATES])
            where.append(or_(live, tasks.c.change_seq > since))
        statement = (
            select(tasks)
            .where(*where)
            .order_by(tasks.c.priority.desc(), tasks.c.run_after, tasks.c.ordinal, tasks.c.id)
        )
        if since is None:
            statement = statement.offset(max(offset, 0)).limit(min(max(limit, 0), MAX_LIMIT))
        with db.read(self._engine) as conn:
            # The cursor is read first: a change that commits after it is found by the next poll.
            clock = int(
                conn.execute(select(event_clock.c.seq).where(event_clock.c.id == 1)).scalar_one()
            )
            items = [TaskRow.from_row(row).to_view() for row in conn.execute(statement)]
            total = len(items)
            if since is None:
                total = int(
                    conn.execute(select(func.count()).select_from(tasks).where(*where)).scalar_one()
                )
        return TaskPage(items=items, total=total, next_since=clock)
