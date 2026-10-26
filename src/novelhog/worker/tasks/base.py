"""The worker half of a task kind: the `TaskProcessor` protocol and its context (tasks spec 5).

A processor is the network half of a kind. Its `run` gets the work item that the kind's repo
handler read when the task started, holds no transaction, and returns an `Outcome` that the
runner records through `repo.tasks.finish`.
"""

import random
import threading
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, ClassVar, Protocol

from novelhog.chapter_store import ChapterStore
from novelhog.config import Settings
from novelhog.domain.hosts import FailureKind
from novelhog.domain.tasks import Outcome, TaskKind
from novelhog.repo.hosts import HostState
from novelhog.repo.tasks import TaskRow
from novelhog.storage import StaticStorage
from novelhog.worker.refresh import TocAdapter
from novelhog.worker.tasks.chapter_download import ChapterAdapter, ImageSource


class WorkerAdapter(TocAdapter, ChapterAdapter, Protocol):
    """What the worker needs from `ScraperAdapter`: hosts, TOC syncs and chapter fetches."""


class Beat(Protocol):
    """A running heartbeat; `lost` turns True once a renewal found the claim gone."""

    lost: bool


type HeartbeatFactory = Callable[[Callable[[], bool], float], AbstractContextManager[Beat]]
"""Builds a heartbeat from a `renew` callback and an interval in seconds (`runtime.Heartbeat`)."""


class HostGate(Protocol):
    """Where a processor reports how its host answered (`repo.hosts`)."""

    def succeeded(self, host: str) -> None:
        """The host answered; its failure count starts again."""
        ...

    def failed(self, host: str, kind: FailureKind, *, retry_after: float | None) -> HostState:
        """Count a failure that can pause the host; says whether it is now paused."""
        ...


@dataclass(frozen=True)
class TaskContext:
    """The dependencies a processor's `run` may use: the same as v1's worker stages.

    `beat` is the running task's heartbeat; the runner sets it for each `run` (None otherwise).
    """

    adapter: WorkerAdapter
    fetcher: ImageSource
    storage: StaticStorage
    chapter_store: ChapterStore
    settings: Settings
    stop: threading.Event
    hosts: HostGate
    rng: random.Random
    heartbeat: HeartbeatFactory
    clock: Callable[[], datetime]
    beat: Beat | None = None


class TaskProcessor[W, R](Protocol):
    """The network half of one task kind; `W` is the work item, `R` the result it produces."""

    kind: ClassVar[TaskKind]

    def run(self, task: TaskRow, work: W, ctx: TaskContext) -> Outcome[R]:
        """Do the work without holding a transaction; an exception counts as `Failed`."""
        ...

    def stored_keys(self, outcome: Outcome[R]) -> list[str]:
        """Static-storage keys `run` stored for `outcome`; trashed when the finish fence fails."""
        ...

    def finished(self, task: TaskRow, outcome: Outcome[R], report: object) -> None:
        """After a fenced finish: log what the handler's `complete` reported (`report`)."""
        ...


type ProcessorRegistry = Mapping[TaskKind, TaskProcessor[Any, Any]]
