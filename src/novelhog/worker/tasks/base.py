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
from typing import Any, ClassVar, Protocol

from novelhog.chapter_store import ChapterStore
from novelhog.config import Settings
from novelhog.domain.tasks import Outcome, TaskKind
from novelhog.repo.tasks import TaskRow
from novelhog.storage import StaticStorage
from novelhog.worker.downloads import ChapterAdapter, ImageSource
from novelhog.worker.reachability import Reachability
from novelhog.worker.refresh import TocAdapter


class WorkerAdapter(TocAdapter, ChapterAdapter, Protocol):
    """What the worker needs from `ScraperAdapter`: hosts, TOC syncs and chapter fetches."""


class Beat(Protocol):
    """A running heartbeat; `lost` turns True once a renewal found the claim gone."""

    lost: bool


type HeartbeatFactory = Callable[[Callable[[], bool], float], AbstractContextManager[Beat]]
"""Builds a heartbeat from a `renew` callback and an interval in seconds (`runtime.Heartbeat`)."""


@dataclass(frozen=True)
class TaskContext:
    """The dependencies a processor's `run` may use: the same as v1's worker stages."""

    adapter: WorkerAdapter
    fetcher: ImageSource
    storage: StaticStorage
    chapter_store: ChapterStore
    settings: Settings
    stop: threading.Event
    reachability: Reachability
    rng: random.Random
    heartbeat: HeartbeatFactory


class TaskProcessor[W, R](Protocol):
    """The network half of one task kind; `W` is the work item, `R` the result it produces."""

    kind: ClassVar[TaskKind]

    def run(self, task: TaskRow, work: W, ctx: TaskContext) -> Outcome[R]:
        """Do the work without holding a transaction; an exception counts as `Failed`."""
        ...

    def stored_keys(self, outcome: Outcome[R]) -> list[str]:
        """Static-storage keys `run` stored for `outcome`; trashed when the finish fence fails."""
        ...


type ProcessorRegistry = Mapping[TaskKind, TaskProcessor[Any, Any]]
