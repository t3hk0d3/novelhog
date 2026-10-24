"""The repo half of `export_build`: v1's export claim, finish and fail as a task handler.

The build is still needed while the export is `waiting` and buildable (v1 spec 6.5; the one
definition is `exports._buildable`); `begin` then turns it `building` with an `EX` event and `work`
reads the `ExportClaim`. `complete` applies v1's `finish` (`Done`: `ready` with the stored file) or
`fail` (`Failed`: `failed` with the error); there is no `Retry` (tasks spec section 5). `recover`
hands a lapsed build's export back to `waiting`. Every export write is conditional on the export
still being `building`.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, ClassVar

from sqlalchemy import Connection, delete, update

from novelhog.domain.exports import ExportState
from novelhog.domain.tasks import Begin, Done, Failed, Outcome, Skip, Started, TaskKind
from novelhog.repo import events
from novelhog.repo import exports as export_repo
from novelhog.repo.context import RepoContext
from novelhog.repo.exports import ExportClaim
from novelhog.repo.schema import exports, storage_trash
from novelhog.repo.tasks import TaskRow

_BUILDING = ExportState.BUILDING.value


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
        self._emit(conn, task, now)
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
        self._emit(conn, task, now)
        return BuildReport(task.entity_id, state)

    def recover(self, conn: Connection, task: TaskRow, now: datetime) -> None:
        """The lapsed build went back to the queue: its export returns to `waiting`."""
        reset = conn.execute(
            update(exports)
            .where(exports.c.id == task.entity_id, exports.c.state == _BUILDING)
            .values(state=ExportState.WAITING.value)
        ).rowcount
        if reset == 1:
            self._emit(conn, task, now)

    def _emit(self, conn: Connection, task: TaskRow, now: datetime) -> None:
        """Append the export's `EX` event; the snapshot is built after the clock is taken."""
        export_id = task.entity_id
        events.append(
            conn,
            task.novel_id,
            export_id,
            lambda: export_repo.snapshot(
                conn, export_id, static_base_url=self._static_base_url
            ).to_json(),
            now=now,
        )
