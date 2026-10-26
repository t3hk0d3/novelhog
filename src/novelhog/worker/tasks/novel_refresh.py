"""The worker half of `novel_refresh`: the network part of v1's refresh (v1 spec 6.2, tasks 5).

`run` fetches the TOC and stores a missing cover (not once the heartbeat lost the claim), then
hands the result to the repo handler as an outcome: `Done(SyncSucceeded)`; `Retry` at
`now + REFRESH_MIN_INTERVAL` when FlareSolverr is down (the interval is kept); `Retry` at the end
of the pause when the failure paused the host (`domain.hosts`); else `Failed(error, SyncFailed)`
with the backed-off schedule. A refresh has no chapter-style retries: its schedule backs off.
"""

from datetime import timedelta
from typing import Any, ClassVar

from novelhog import log
from novelhog.domain.hosts import FailureKind
from novelhog.domain.refresh import jittered, next_interval
from novelhog.domain.tasks import Done, Failed, Outcome, Retry, RetryCause, TaskKind
from novelhog.repo.chapters import TocDiff
from novelhog.repo.task_kinds.novel_refresh import NovelClaim, SyncFailed, SyncSucceeded
from novelhog.repo.tasks import TaskRow
from novelhog.scraper_adapter.adapter import TocResult
from novelhog.scraper_adapter.errors import (
    HostFailureError,
    InfrastructureError,
    SiteError,
    SiteUnreachable,
)
from novelhog.storage import StaticStorage, cover_key
from novelhog.worker.errors import log_host_paused, summarize, unreachable_message
from novelhog.worker.refresh import CoverFetcher, TocAdapter
from novelhog.worker.tasks.base import TaskContext

SHRINK_MIN_KNOWN = 20
_COVER_EXTENSIONS = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/gif": "gif",
    "image/webp": "webp",
}

type RefreshResult = SyncSucceeded | SyncFailed


def _fetch_toc(adapter: TocAdapter, claim: NovelClaim) -> TocResult:
    """Run the sync; raise `SiteError` for an unsupported host or a TOC that shrank too much."""
    if not claim.host_supported:
        msg = "Host no longer supported by the scraper"
        raise SiteError(msg)
    toc = adapter.sync_toc(claim.novel_id, claim.host, claim.toc_url)
    known = claim.known_pages
    if not claim.initial and known >= SHRINK_MIN_KNOWN and len(toc.urls) < known / 2:
        msg = f"TOC shrank from {known} to {len(toc.urls)}"
        raise SiteError(msg)
    return toc


def _store_cover(
    fetcher: CoverFetcher, files: StaticStorage, claim: NovelClaim, cover_url: str | None
) -> str | None:
    """Fetch and store the cover; any failure is logged and leaves the key unset."""
    if cover_url is None:
        return None
    image = fetcher.fetch(cover_url)
    if image is None:
        log.get(novel_id=claim.novel_id).warning("cover_fetch_failed", cover_url=cover_url)
        return None
    key = cover_key(claim.novel_id, image.data, _COVER_EXTENSIONS[image.mime])
    try:
        files.put(key, image.data, image.mime)
    except OSError as error:
        log.get(novel_id=claim.novel_id).warning("cover_store_failed", error=summarize(error))
        return None
    return key


class NovelRefreshProcessor:
    """Syncs one novel's TOC (v1 spec section 6.2) without holding a transaction."""

    kind: ClassVar[TaskKind] = TaskKind.NOVEL_REFRESH

    def run(self, task: TaskRow, work: NovelClaim, ctx: TaskContext) -> Outcome[RefreshResult]:
        """Fetch the TOC and the cover; every failure becomes an outcome, none is raised."""
        claim = work
        settings = ctx.settings
        logger = log.get(novel_id=claim.novel_id, host=claim.host, worker_id=settings.worker_id)
        min_s, max_s = settings.refresh_min_interval, settings.refresh_max_interval
        now = ctx.clock()
        try:
            toc = _fetch_toc(ctx.adapter, claim)
            ctx.hosts.succeeded(claim.host)
            cover = None
            if claim.cover_key is None and (ctx.beat is None or not ctx.beat.lost):
                cover = _store_cover(ctx.fetcher, ctx.storage, claim, toc.cover_url)
        except Exception as error:  # noqa: BLE001 - any other failure counts as a site error
            message = summarize(error)
            if isinstance(error, InfrastructureError):
                logger.warning("refresh_infrastructure_error", error=message)
                return Retry(now + timedelta(seconds=min_s), message, RetryCause.INFRASTRUCTURE)
            if isinstance(error, HostFailureError) and error.kind is not FailureKind.TRANSIENT:
                host = ctx.hosts.failed(claim.host, error.kind, retry_after=error.retry_after)
                if host.paused_until is not None:
                    log_host_paused(logger, claim.host, error.kind, host)
                    return Retry(host.paused_until, message, RetryCause.HOST_PAUSED)
                if isinstance(error, SiteUnreachable):
                    message = unreachable_message(error)
            elif isinstance(error, SiteError) and not isinstance(error, HostFailureError):
                ctx.hosts.succeeded(claim.host)  # the host answered
            logger.warning("refresh_failed", error=message, exc_info=True)
            interval = next_interval(
                claim.refresh_interval, found_new=False, failed=True, min_s=min_s, max_s=max_s
            )
            failed = SyncFailed(
                message if claim.initial else None,
                interval,
                now + timedelta(seconds=jittered(interval, ctx.rng)),
            )
            return Failed(message, failed)
        return Done(
            SyncSucceeded(
                toc.urls,
                toc.title,
                toc.language,
                cover,
                min_s,
                max_s,
                ctx.rng,
                chapter_titles=toc.chapter_titles,
            )
        )

    def stored_keys(self, outcome: Outcome[RefreshResult]) -> list[str]:
        """The cover `run` stored, if any."""
        result = outcome.result if isinstance(outcome, Done) else None
        if isinstance(result, SyncSucceeded) and result.cover_key is not None:
            return [result.cover_key]
        return []

    def finished(self, task: TaskRow, outcome: Outcome[Any], report: object) -> None:
        """Log the chapters the sync removed or dequeued, then `refresh_finished`."""
        if not isinstance(report, TocDiff):
            return
        logger = log.get(novel_id=task.entity_id)
        if report.removed:
            logger.warning("toc_chapters_removed", chapter_ids=report.removed)
        if report.dequeued:
            logger.warning("toc_chapters_dequeued", chapter_ids=report.dequeued)
        logger.info("refresh_finished", new=len(report.new), failed=isinstance(outcome, Failed))
