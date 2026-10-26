"""Background task types: kinds, states, priorities and processor outcomes (tasks spec 2, 5)."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum


class TaskKind(StrEnum):
    """What a task does; each kind accepts one entity ID prefix."""

    NOVEL_REFRESH = "novel_refresh"
    CHAPTER_DOWNLOAD = "chapter_download"
    EXPORT_BUILD = "export_build"

    @property
    def prefix(self) -> str:
        """The ID prefix of the entities this kind works on."""
        return _PREFIXES[self]


_PREFIXES: dict[TaskKind, str] = {
    TaskKind.NOVEL_REFRESH: "NV",
    TaskKind.CHAPTER_DOWNLOAD: "CH",
    TaskKind.EXPORT_BUILD: "EX",
}


class TaskState(StrEnum):
    """Execution state of a task."""

    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


LIVE_STATES: frozenset[TaskState] = frozenset({TaskState.QUEUED, TaskState.RUNNING})
FINISHED_STATES: frozenset[TaskState] = frozenset(
    {TaskState.DONE, TaskState.FAILED, TaskState.SKIPPED, TaskState.CANCELLED}
)

PRIORITY_REFRESH = 300
PRIORITY_BUILD = 200
PRIORITY_RELEASE = 110
PRIORITY_USER = 100

TASK_RETENTION = timedelta(days=7)
TASK_REPAIR_INTERVAL = timedelta(hours=1)


class StartResult(StrEnum):
    """What `TaskQueue.start` reports to the runner."""

    OK = "ok"
    CANCELLED = "cancelled"
    DEFERRED = "deferred"
    LOST = "lost"


@dataclass(frozen=True)
class Started:
    """The task's repo handler started it; the processor goes on."""


@dataclass(frozen=True)
class Skip:
    """The work is no longer needed."""

    reason: str


Begin = Started | Skip


@dataclass(frozen=True)
class Done[R]:
    """The work finished."""

    result: R


@dataclass(frozen=True)
class Failed[R]:
    """The work failed for good."""

    error: str
    result: R | None = None


class RetryCause(StrEnum):
    """Why work is tried again; decides the loop's backoff and whether a retry is used up."""

    INFRASTRUCTURE = "infrastructure"  # FlareSolverr down: the loop backs off
    HOST_PAUSED = "host_paused"  # the host is paused: waiting costs nothing
    TRANSIENT = "transient"  # the request failed: one of the chapter's retries is used


@dataclass(frozen=True)
class Retry:
    """The work failed and is tried again at `run_after`."""

    run_after: datetime
    error: str
    cause: RetryCause = RetryCause.INFRASTRUCTURE


type Outcome[R] = Done[R] | Failed[R] | Retry
