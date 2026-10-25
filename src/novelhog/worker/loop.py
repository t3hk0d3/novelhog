"""The worker main loop (spec section 6.1, tasks spec section 6).

`Worker.step` is one iteration of the tasks spec section 6 loop:

1. recover lapsed claims (always): the notifier's consumers, then lapsed tasks (`Runner.recover`),
2. `_dispatch_notifications` (always runs, never returns early; a failure is logged; keeps
   dispatching while full batches come back, for up to `DISPATCH_BUDGET_S`),
3. `_empty_trash` (always runs, never returns early; a failure is logged),
4. `_repair`: task repair at most every `TASK_REPAIR_INTERVAL` on the loop's clock, idle or busy;
   each fix is logged as `task_repaired`, a failure as `task_repair_failed` (the step goes on),
5. claim and run one task (`Runner.step`): refreshes, builds and downloads, one per claim,
6. otherwise "notify" while the notifier is still behind (no maintenance, no idle sleep), else
   `_maintain` (at most hourly; a failure is logged) and "idle".

A `RunnerBackoff` from stage 5 backs the loop off like a failed step; the runner has logged the
task event, so the loop logs nothing.
"""

import random
import threading
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Literal

from novelhog import log
from novelhog.chapter_store import ChapterStore
from novelhog.config import Settings
from novelhog.domain.errors import NotFound
from novelhog.domain.tasks import TASK_REPAIR_INTERVAL, TaskKind
from novelhog.notifications import dispatcher
from novelhog.repo import Repo, utcnow
from novelhog.repo.tasks import Repair
from novelhog.storage import StaticStorage
from novelhog.worker import maintenance, refresh, runtime
from novelhog.worker.reachability import Reachability
from novelhog.worker.runner import Runner, RunnerBackoff
from novelhog.worker.tasks import default_processors
from novelhog.worker.tasks.base import ProcessorRegistry, TaskContext, WorkerAdapter
from novelhog.worker.tasks.chapter_download import ImageSource

MAINTENANCE_INTERVAL = timedelta(hours=1)
DISPATCH_BUDGET_S = 2.0

StepResult = Literal["task", "notify", "idle"]


class Worker:
    """Runs the loop's steps until asked to stop."""

    def __init__(  # noqa: PLR0913
        self,
        repo: Repo,
        adapter: WorkerAdapter,
        fetcher: ImageSource,
        storage: StaticStorage,
        # A channel mapping, or a callable returning one (production passes `RegistryHolder.get`).
        channels: dispatcher.Registry,
        settings: Settings,
        *,
        stop: threading.Event | None = None,
        clock: Callable[[], datetime] = utcnow,
        monotonic: Callable[[], float] = time.monotonic,
        processors: ProcessorRegistry | None = None,
    ) -> None:
        self._repo = repo
        self._adapter = adapter
        self._storage = storage
        self._channels = channels
        self._settings = settings
        self._stop = stop if stop is not None else threading.Event()
        self._clock = clock
        self._monotonic = monotonic
        self._last_maintenance: datetime | None = None
        self._last_repair: datetime | None = None
        self._rng = random.Random()  # noqa: S311 - refresh jitter, not security
        self._backoff = runtime.Backoff(settings.idle_sleep)
        self._reachability = Reachability()
        self._runner = Runner(
            repo,
            default_processors() if processors is None else processors,
            TaskContext(
                adapter=adapter,
                fetcher=fetcher,
                storage=storage,
                chapter_store=ChapterStore(repo),
                settings=settings,
                stop=self._stop,
                reachability=self._reachability,
                rng=self._rng,
                heartbeat=runtime.Heartbeat,
                clock=repo.clock,
            ),
        )

    def request_stop(self) -> None:
        """Ask the loop to return after the current step; wakes any wait."""
        self._stop.set()

    def run(self) -> None:
        """Start up, then step until stopped. Raises `SchemaOutdated` at startup."""
        runtime.configure_runtime(self._settings)
        self._repo.require_current()
        refresh.publish_hosts(self._repo, self._adapter)
        self._repo.work.recover()
        self._runner.recover()
        logger = log.get(worker_id=self._settings.worker_id)
        for variable in self._settings.ignored:
            logger.warning("config_ignored", variable=variable)
        logger.info("worker_started")
        while not self._stop.is_set():
            try:
                result = self.step()
            except RunnerBackoff:
                self._stop.wait(self._backoff.next())
                continue
            except Exception as error:  # noqa: BLE001 - the loop must survive any step failure
                delay = self._backoff.next()
                logger.error(
                    "worker_step_failed",
                    error=f"{type(error).__name__}: {error}",
                    retry_in=delay,
                    exc_info=True,
                )
                self._stop.wait(delay)
                continue
            self._backoff.reset()
            if result == "idle":
                self._stop.wait(self._settings.idle_sleep)
        logger.info("worker_stopped")

    def step(self) -> StepResult:
        """Do one unit of work and name it (tasks spec section 6)."""
        self._repo.work.recover()
        self._runner.recover()
        behind = self._dispatch_notifications()
        self._empty_trash()
        self._repair()
        if self._runner.step():
            return "task"
        if behind:
            return "notify"
        self._maintain()
        return "idle"

    def _dispatch_notifications(self) -> bool:
        """Deliver the event log to the channels; True when the notifier is still behind.

        Dispatches batch after batch while full batches come back, until `DISPATCH_BUDGET_S` has
        passed or a stop is requested. A failure is logged and never stops the step.
        """
        settings = self._settings
        deadline = self._monotonic() + DISPATCH_BUDGET_S
        try:
            while True:
                report = dispatcher.dispatch(
                    self._repo,
                    self._channels,
                    owner=settings.worker_id,
                    ttl=settings.claim_ttl,
                    public_url=settings.public_url or "",
                )
                behind = report.advanced and report.events >= dispatcher.BATCH
                if not behind or self._stop.is_set() or self._monotonic() >= deadline:
                    return behind
        except Exception as error:  # noqa: BLE001 - notifications must not block novel work
            log.get(worker_id=settings.worker_id).error(
                "notification_dispatch_failed",
                error=f"{type(error).__name__}: {error}",
                exc_info=True,
            )
            return False

    def _empty_trash(self) -> None:
        """Delete trashed static files; a failure is logged and never stops the step."""
        try:
            maintenance.empty_trash(self._repo, self._storage)
        except Exception as error:  # noqa: BLE001 - trash must not block novel work
            log.get(worker_id=self._settings.worker_id).error(
                "empty_trash_failed", error=f"{type(error).__name__}: {error}", exc_info=True
            )

    def _repair(self) -> None:
        """Repair tasks at most once per `TASK_REPAIR_INTERVAL`; a failure is logged.

        Logs `task_repaired` with the problem for each fix (tasks spec section 8).
        """
        now = self._clock()
        last = self._last_repair
        if last is not None and now - last < TASK_REPAIR_INTERVAL:
            return
        self._last_repair = now  # a failing run is retried next interval, not every pass
        logger = log.get(worker_id=self._settings.worker_id)
        try:
            repairs = self._repo.tasks.repair()
        except Exception as error:  # noqa: BLE001 - repair must not stop the loop
            logger.error(
                "task_repair_failed", error=f"{type(error).__name__}: {error}", exc_info=True
            )
            return
        for repair in repairs:
            logger.warning("task_repaired", problem=repair.problem, **self._repaired(repair))

    def _repaired(self, repair: Repair) -> dict[str, object]:
        """The task fields of a `task_repaired` line; `attempts` is None without a task."""
        attempts: int | None = None
        if repair.task_id is not None:
            try:
                attempts = self._repo.tasks.get(repair.task_id).attempts
            except NotFound:  # pruned or deleted with its novel since the fix
                pass
        return {
            "task_id": repair.task_id,
            "kind": TaskKind.for_entity(repair.entity_id).value,
            "entity_id": repair.entity_id,
            "attempts": attempts,
        }

    def _maintain(self) -> None:
        """Run maintenance at most once per `MAINTENANCE_INTERVAL`; a failure is logged."""
        now = self._clock()
        last = self._last_maintenance
        if last is not None and now - last < MAINTENANCE_INTERVAL:
            return
        self._last_maintenance = now  # a failing run is retried next hour, not every pass
        try:
            maintenance.maintain(self._repo, now=now)
        except Exception as error:  # noqa: BLE001 - maintenance must not stop the loop
            log.get(worker_id=self._settings.worker_id).error(
                "maintenance_failed", error=f"{type(error).__name__}: {error}", exc_info=True
            )
