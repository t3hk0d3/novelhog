"""The worker main loop (spec section 6.1).

`Worker.step` is one iteration and runs these stages in priority order, each returning early
when it did work:

1. recover expired claims (always),
2. `_dispatch_notifications` (always runs, never returns early; a failure is logged),
3. `_empty_trash` (always runs, never returns early; a failure is logged),
4. refresh a due novel,
5. `_build_export` (claim and build a ready export),
6. download a batch of pending chapters,
7. otherwise `_maintain` (at most hourly; a failure is logged) and report "idle".
"""

import random
import threading
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Literal, Protocol

from novelhog import log
from novelhog.chapter_store import ChapterStore
from novelhog.config import Settings
from novelhog.notifications import dispatcher
from novelhog.repo import Repo, utcnow
from novelhog.storage import StaticStorage
from novelhog.worker import builds, downloads, maintenance, refresh, runtime
from novelhog.worker.downloads import ChapterAdapter, ImageSource
from novelhog.worker.reachability import Reachability
from novelhog.worker.refresh import TocAdapter

MAINTENANCE_INTERVAL = timedelta(hours=1)

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
        # A channel mapping, or a callable returning one (production passes `RegistryHolder.get`).
        channels: dispatcher.Registry,
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
        self._last_maintenance: datetime | None = None
        self._rng = random.Random()  # noqa: S311 - refresh jitter, not security
        self._backoff = runtime.Backoff(settings.idle_sleep)
        self._reachability = Reachability()

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
        self._empty_trash()
        if self._refresh_due():
            return "refresh"
        if self._build_export():
            return "build"
        if self._download_batch():
            return "download"
        self._maintain()
        return "idle"

    def _dispatch_notifications(self) -> None:
        """Deliver the event log to the channels; a failure is logged and never stops the step."""
        settings = self._settings
        try:
            dispatcher.dispatch(
                self._repo,
                self._channels,
                owner=settings.worker_id,
                ttl=settings.claim_ttl,
                public_url=settings.public_url or "",
            )
        except Exception as error:  # noqa: BLE001 - notifications must not block novel work
            log.get(worker_id=settings.worker_id).error(
                "notification_dispatch_failed",
                error=f"{type(error).__name__}: {error}",
                exc_info=True,
            )

    def _empty_trash(self) -> None:
        """Delete trashed static files; a failure is logged and never stops the step."""
        try:
            maintenance.empty_trash(self._repo, self._storage)
        except Exception as error:  # noqa: BLE001 - trash must not block novel work
            log.get(worker_id=self._settings.worker_id).error(
                "empty_trash_failed", error=f"{type(error).__name__}: {error}", exc_info=True
            )

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

    def _build_export(self) -> bool:
        """Claim and build a ready export; True if one was built."""
        settings = self._settings
        claim = self._repo.exports.claim_ready(settings.worker_id, settings.claim_ttl)
        if claim is None:
            return False
        builds.build(
            self._repo,
            ChapterStore(self._repo),
            self._storage,
            claim,
            settings=settings,
            tmp_dir=settings.data_dir / "tmp",
        )
        return True

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
            reachability=self._reachability,
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
            self._repo,
            self._adapter,
            self._fetcher,
            batch,
            settings=settings,
            stop=self._stop,
            reachability=self._reachability,
        )
        return True
