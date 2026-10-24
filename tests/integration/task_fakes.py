"""A recording task handler and processor for a real kind, and helpers to queue tasks.

No kind has a real handler or processor until Tasks 6-8 register them, so the tests of the
generic task path inject these through `Repo(handlers=...)` and `Runner(processors=...)`.
"""

from collections.abc import Callable
from datetime import datetime
from typing import Any, ClassVar

from sqlalchemy import Connection, select, update

from novelhog.domain.tasks import Begin, Done, Outcome, Started, TaskKind, TaskState
from novelhog.repo import Busy, Repo, db
from novelhog.repo.novels import lock_novel
from novelhog.repo.schema import novels, tasks
from novelhog.repo.tasks import TaskRow
from novelhog.worker.tasks.base import TaskContext

KIND = TaskKind.NOVEL_REFRESH
"""The real kind the fakes are registered for."""


class RecordingHandler:
    """A `TaskHandler` that records each hook with the task state its transaction sees.

    `work` returns the novel's `toc_url` read in the transaction. `fail_in` names a hook that
    raises `Busy`, which rolls the transaction back.
    """

    kind: ClassVar[TaskKind] = KIND

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, TaskState]] = []
        self.outcomes: list[Outcome[Any]] = []
        self.begin_result: Begin = Started()
        self.fail_in: str | None = None

    @property
    def hooks(self) -> list[str]:
        """The names of the hooks called so far, in order."""
        return [hook for hook, _, _ in self.calls]

    def _seen(self, hook: str, conn: Connection, task: TaskRow) -> None:
        state = conn.execute(select(tasks.c.state).where(tasks.c.id == task.id)).scalar_one()
        self.calls.append((hook, task.id, TaskState(state)))
        if self.fail_in == hook:
            message = "database is locked"
            raise Busy(message)

    def work(self, conn: Connection, task: TaskRow) -> str:
        self._seen("work", conn, task)
        return str(
            conn.execute(select(novels.c.toc_url).where(novels.c.id == task.novel_id)).scalar_one()
        )

    def begin(self, conn: Connection, task: TaskRow, now: datetime) -> Begin:
        self._seen("begin", conn, task)
        return self.begin_result

    def complete(
        self, conn: Connection, task: TaskRow, outcome: Outcome[Any], now: datetime
    ) -> None:
        self._seen("complete", conn, task)
        self.outcomes.append(outcome)

    def recover(self, conn: Connection, task: TaskRow, now: datetime) -> None:
        self._seen("recover", conn, task)


class RecordingProcessor:
    """A `TaskProcessor` that records its runs and returns `outcome`.

    `during` runs inside `run` (to stop the worker or lose the claim); `error`, if set, is raised
    instead of returning. `keys` are what `stored_keys` reports.
    """

    kind: ClassVar[TaskKind] = KIND

    def __init__(self) -> None:
        self.runs: list[tuple[str, object]] = []
        self.outcome: Outcome[Any] = Done("result")
        self.error: Exception | None = None
        self.during: Callable[[], object] | None = None
        self.keys: list[str] = []

    def run(self, task: TaskRow, work: object, ctx: TaskContext) -> Outcome[Any]:
        self.runs.append((task.id, work))
        if self.during is not None:
            self.during()
        if self.error is not None:
            raise self.error
        return self.outcome

    def stored_keys(self, outcome: Outcome[Any]) -> list[str]:
        return list(self.keys)


def enqueue_task(repo: Repo, novel_id: str, **options: Any) -> TaskRow:
    """Queue a `KIND` task for the novel, due now, following the lock order."""
    queue = repo.tasks._queue  # noqa: SLF001  (tests set up rows through the internal queue)
    arguments: dict[str, Any] = {"priority": 300, "run_after": repo.clock()} | options
    with db.write(repo.engine) as conn:
        assert lock_novel(conn, novel_id)
        task = queue.enqueue(conn, KIND, novel_id, novel_id, **arguments)
        queue.stamp(conn)
    return task


def task_row(repo: Repo, task_id: str) -> TaskRow:
    """Read one task row."""
    with db.read(repo.engine) as conn:
        return TaskRow.from_row(conn.execute(select(tasks).where(tasks.c.id == task_id)).one())


def task_rows(repo: Repo) -> list[TaskRow]:
    """Read every task row in `id` order."""
    with db.read(repo.engine) as conn:
        return [TaskRow.from_row(row) for row in conn.execute(select(tasks).order_by(tasks.c.id))]


def force_claim(repo: Repo, task_id: str, owner: str, until: datetime) -> TaskRow:
    """Give the task a claim directly, bypassing `claim`'s eligibility rules."""
    with db.write(repo.engine) as conn:
        conn.execute(
            update(tasks).where(tasks.c.id == task_id).values(claimed_by=owner, claimed_until=until)
        )
    return task_row(repo, task_id)
