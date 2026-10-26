"""The repo half of a task kind: the `TaskHandler` protocol and its registry (tasks spec 5).

A handler is the transactional half of a kind. Its hooks run inside the transactions that the
public `Tasks` methods compose, after `lock_novel` and the queue primitive and before
`TaskQueue.stamp`; they write entity rows and then entity events, and never do network I/O.
`problems` and `fix` are the kind's repair checks (tasks spec section 8): `problems` runs in a
plain read and `fix` in its own transaction under `lock_novel`, before `TaskQueue.stamp`.
"""

from collections.abc import Mapping
from datetime import datetime
from typing import TYPE_CHECKING, Any, ClassVar, Protocol

from sqlalchemy import Connection

from novelhog.domain.tasks import Begin, Outcome, TaskKind

if TYPE_CHECKING:
    from novelhog.repo.tasks import Problem, Repair, TaskRow


class TaskHandler[W](Protocol):
    """The transactional half of one task kind; `W` is the work item handed to the processor."""

    kind: ClassVar[TaskKind]

    def work(self, conn: Connection, task: TaskRow) -> W:
        """Read the work item inside start's transaction, after `begin` said `Started`."""
        ...

    def begin(self, conn: Connection, task: TaskRow, now: datetime) -> Begin:
        """Inside start's transaction: `Started` (with the entity change on start) or `Skip`."""
        ...

    def complete(
        self, conn: Connection, task: TaskRow, outcome: Outcome[Any], now: datetime
    ) -> object:
        """Inside finish's or retry's transaction, only when the fenced write applied.

        Returns a report of what it changed for the processor to log (`Finished.report`), or None.
        """
        ...

    def recover(self, conn: Connection, task: TaskRow, now: datetime) -> None:
        """Inside recovery's transaction, after the lapsed running task went back to the queue."""
        ...

    def rescheduled(self, conn: Connection, task: TaskRow, now: datetime) -> None:
        """Inside `Tasks.reschedule`'s transaction, after the task moved to its host's next slot."""
        ...

    def problems(self, conn: Connection) -> list[Problem]:
        """In a plain read: the kind's broken entity/task pairs (tasks spec section 8)."""
        ...

    def fix(self, conn: Connection, problem: Problem, now: datetime) -> Repair | None:
        """In the fix's own transaction: re-check `problem` and fix it; None if it no longer holds.

        Follows the lock order: entity and task rows first, then the entity events.
        """
        ...


type HandlerRegistry = Mapping[TaskKind, TaskHandler[Any]]
