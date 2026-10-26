"""The repo half of `export_build`: v1's export claim, finish and fail as a task handler.

The build is still needed while the export is `waiting` and buildable (v1 spec 6.5; the one
definition is `exports.buildable`); `begin` then turns it `building` with an `EX` event and `work`
reads the `ExportClaim`. `complete` applies v1's `finish` (`Done`: `ready` with the stored file) or
`fail` (`Failed`: `failed` with the error); there is no `Retry` (tasks spec section 5). `recover`
hands a lapsed build's export back to `waiting`. Every export write is conditional on the export
still being `building`.

Repair (tasks spec section 8): `problems` finds buildable `waiting` exports without a live build
(`export_buildable_without_task`: enqueued), `building` ones without a running build
(`export_building_without_task`: back to `waiting` with an `EX` event, enqueued if buildable) and
queued builds of exports that are gone, `ready` or `failed` (`queued_task_not_needed`: cancelled).
An export with a running build is never a problem.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, ClassVar

from sqlalchemy import ColumnElement, Connection, delete, select, update

from novelhog.domain.exports import ExportState
from novelhog.domain.tasks import (
    LIVE_STATES,
    Begin,
    Done,
    Failed,
    Outcome,
    Skip,
    Started,
    TaskKind,
    TaskState,
)
from novelhog.repo import events
from novelhog.repo import exports as export_repo
from novelhog.repo import tasks as task_queue
from novelhog.repo.context import RepoContext
from novelhog.repo.exports import ExportClaim
from novelhog.repo.schema import exports, storage_trash, tasks
from novelhog.repo.tasks import Problem, Repair, TaskRow

_BUILDING = ExportState.BUILDING.value
_KIND = TaskKind.EXPORT_BUILD
BUILDABLE_WITHOUT_TASK = "export_buildable_without_task"
BUILDING_WITHOUT_TASK = "export_building_without_task"
TASK_NOT_NEEDED = "queued_task_not_needed"


def _export_checks() -> dict[str, ColumnElement[bool]]:
    """Each export problem of tasks spec section 8 as a condition on `exports`."""
    no_live = ~task_queue.has_task(_KIND, exports.c.id, LIVE_STATES)
    no_running = ~task_queue.has_task(_KIND, exports.c.id, [TaskState.RUNNING])
    return {
        BUILDABLE_WITHOUT_TASK: export_repo.buildable() & no_live,
        BUILDING_WITHOUT_TASK: (exports.c.state == _BUILDING) & no_running,
    }


def _not_needed() -> ColumnElement[bool]:
    """A queued build whose export is gone, `ready` or `failed`."""
    return task_queue.queued_unneeded(_KIND, exports, [ExportState.WAITING.value, _BUILDING])


@dataclass(frozen=True)
class BuildResult:
    """A built export: the static-storage key of the stored file and its size."""

    storage_key: str
    size_bytes: int


@dataclass(frozen=True)
class BuildReport:
    """What `complete` did: the export's new state, or None when it was no longer building."""

    export_id: str
    state: ExportState | None


class ExportBuildHandler:
    """The transactional half of `export_build`; `complete` reports the export's new state."""

    kind: ClassVar[TaskKind] = TaskKind.EXPORT_BUILD

    def __init__(self, context: RepoContext) -> None:
        self._static_base_url = context.static_base_url

    def work(self, conn: Connection, task: TaskRow) -> ExportClaim:
        """Read what the build needs (v1 `ExportClaim`)."""
        return export_repo.read_claim(conn, task.entity_id)

    def begin(self, conn: Connection, task: TaskRow, now: datetime) -> Begin:
        """Still needed while the export is waiting and buildable; it turns `building`."""
        if not export_repo.begin_build(conn, task.entity_id):
            return Skip("not buildable")
        self._emit(conn, task.novel_id, task.entity_id, now)
        return Started()

    def complete(
        self, conn: Connection, task: TaskRow, outcome: Outcome[Any], now: datetime
    ) -> BuildReport:
        """`Done` → `ready` with the stored file; `Failed` → `failed` with the error (v1 6.5)."""
        stored: str | None = None
        if isinstance(outcome, Done) and isinstance(outcome.result, BuildResult):
            stored = outcome.result.storage_key
            state = ExportState.READY
            values: dict[str, object] = {
                "storage_key": stored,
                "size_bytes": outcome.result.size_bytes,
                "ready_at": now,
            }
        elif isinstance(outcome, Failed):
            state = ExportState.FAILED
            values = {"error": outcome.error}
        else:
            message = f"export_build cannot complete with {outcome!r}"
            raise TypeError(message)
        ended = conn.execute(
            update(exports)
            .where(exports.c.id == task.entity_id, exports.c.state == _BUILDING)
            .values({**values, "state": state.value})
        ).rowcount
        if ended != 1:
            return BuildReport(task.entity_id, None)
        if stored is not None:
            # An earlier owner that lost its claim may have trashed this (deterministic) key.
            conn.execute(delete(storage_trash).where(storage_trash.c.key == stored))
        self._emit(conn, task.novel_id, task.entity_id, now)
        return BuildReport(task.entity_id, state)

    def recover(self, conn: Connection, task: TaskRow, now: datetime) -> None:
        """The lapsed build went back to the queue: its export returns to `waiting`."""
        if self._unbuild(conn, task.entity_id):
            self._emit(conn, task.novel_id, task.entity_id, now)

    def rescheduled(self, conn: Connection, task: TaskRow, now: datetime) -> None:
        """Builds contact no host, so they are never rescheduled; nothing to do."""

    @staticmethod
    def _unbuild(conn: Connection, export_id: str) -> bool:
        """`building` → `waiting`; rows only. False when the export was not building."""
        reset = conn.execute(
            update(exports)
            .where(exports.c.id == export_id, exports.c.state == _BUILDING)
            .values(state=ExportState.WAITING.value)
        ).rowcount
        return reset == 1

    def problems(self, conn: Connection) -> list[Problem]:
        """The exports whose build task is missing, and the unneeded builds."""
        found: list[Problem] = []
        for name, condition in _export_checks().items():
            rows = conn.execute(
                select(exports.c.id, exports.c.novel_id)
                .where(condition)
                .order_by(exports.c.created_at, exports.c.id)
            )
            found.extend(Problem(name, row.id, row.novel_id) for row in rows)
        rows = conn.execute(
            select(tasks.c.id, tasks.c.entity_id, tasks.c.novel_id)
            .where(_not_needed())
            .order_by(tasks.c.id)
        )
        found.extend(Problem(TASK_NOT_NEEDED, row.entity_id, row.novel_id, row.id) for row in rows)
        return found

    def fix(self, conn: Connection, problem: Problem, now: datetime) -> Repair | None:
        """Re-check the problem and fix it (tasks spec section 8); None if it no longer holds."""
        export_id = problem.entity_id
        if problem.name == TASK_NOT_NEEDED:
            holds = conn.execute(
                select(tasks.c.id).where(tasks.c.id == problem.task_id, _not_needed())
            ).first()
            if holds is None:
                return None
            unneeded = task_queue.queued(conn, self.kind, export_id)
            task_queue.cancel(conn, self.kind, export_id, now=now)
            return Repair.of(problem, self.kind, unneeded)
        holds = conn.execute(
            select(exports.c.id).where(exports.c.id == export_id, _export_checks()[problem.name])
        ).first()
        if holds is None:
            return None
        reset = problem.name == BUILDING_WITHOUT_TASK and self._unbuild(conn, export_id)
        export_repo.enqueue_buildable(conn, problem.novel_id, [export_id], now)
        queued = task_queue.queued(conn, self.kind, export_id)
        if reset:
            self._emit(conn, problem.novel_id, export_id, now)
        return Repair.of(problem, self.kind, queued)

    def _emit(self, conn: Connection, novel_id: str, export_id: str, now: datetime) -> None:
        """Append the export's `EX` event; the snapshot is built after the clock is taken."""
        events.append(
            conn,
            novel_id,
            export_id,
            lambda: export_repo.snapshot(
                conn, export_id, static_base_url=self._static_base_url
            ).to_json(),
            now=now,
        )
