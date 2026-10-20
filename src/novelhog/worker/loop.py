"""The worker main loop (spec section 6.1).

`Worker.step` is one iteration and runs these stages in priority order, each returning early
when it did work:

1. recover expired claims (always),
2. `_dispatch_notifications` (extension point for Task 27; always runs, never returns early),
3. refresh a due novel,
4. `_build_export` (extension point for Task 26),
5. download a batch of pending chapters,
6. otherwise report "idle".
"""

import random
import threading
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any, Literal, Protocol

from novelhog import log
from novelhog.config import Settings
from novelhog.repo import Repo, utcnow
from novelhog.storage import StaticStorage
from novelhog.worker import downloads, refresh, runtime
from novelhog.worker.downloads import ChapterAdapter, ImageSource
from novelhog.worker.refresh import TocAdapter

StepResult = Literal["refresh", "build", "download", "idle"]


class WorkerAdapter(TocAdapter, ChapterAdapter, Protocol):
    """What the loop needs from `ScraperAdapter`: hosts, TOC syncs and chapter fetches."""


class Worker:
    """Runs recover, refresh, build and download steps until asked to stop."""

    def __init__(  # noqa: PLR0913
        self,
        repo: Repo,
        adapter: WorkerAdapter,
        fetcher: ImageSource,
        storage: StaticStorage,
        # TODO(Task 27): type as Mapping[str, NotificationChannel] once channels.py exists.
        channels: Mapping[str, Any],
        settings: Settings,
        *,
        stop: threading.Event | None = None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._repo = repo
        self._adapter = adapter
        self._fetcher = fetcher
        self._storage = storage
        self._channels = channels
        self._settings = settings
        self._stop = stop if stop is not None else threading.Event()
        self._clock = clock
        self._rng = random.Random()  # noqa: S311 - refresh jitter, not security
        self._backoff = runtime.Backoff(settings.idle_sleep)

    def request_stop(self) -> None:
        """Ask the loop to return after the current step; wakes any wait."""
        self._stop.set()

    def run(self) -> None:
        """Start up, then step until stopped. Raises `SchemaOutdated` at startup."""
        runtime.configure_runtime(self._settings)
        self._repo.require_current()
        refresh.publish_hosts(self._repo, self._adapter)
        self._repo.work.recover()
        logger = log.get(worker_id=self._settings.worker_id)
        logger.info("worker_started")
        while not self._stop.is_set():
            try:
                result = self.step()
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
        """Do one unit of work and name it."""
        self._repo.work.recover()
        self._dispatch_notifications()
        if self._refresh_due():
            return "refresh"
        if self._build_export():
            return "build"
        if self._download_batch():
            return "download"
        return "idle"

    def _dispatch_notifications(self) -> None:
        """Extension point (Task 27): deliver the event log to the channels. No-op until then."""

    def _build_export(self) -> bool:
        """Extension point (Task 26): claim and build a ready export; True if one was built."""
        return False

    def _refresh_due(self) -> bool:
        settings = self._settings
        claim = self._repo.novels.claim_due_refresh(settings.worker_id, settings.claim_ttl)
        if claim is None:
            return False
        refresh.refresh(
            self._repo,
            self._adapter,
            self._fetcher,
            self._storage,
            claim,
            settings=settings,
            rng=self._rng,
        )
        return True

    def _download_batch(self) -> bool:
        settings = self._settings
        batch = self._repo.work.claim_batch(
            settings.worker_id, settings.batch_size, settings.claim_ttl
        )
        if not batch:
            return False
        downloads.download(
            self._repo, self._adapter, self._fetcher, batch, settings=settings, stop=self._stop
        )
        return True
