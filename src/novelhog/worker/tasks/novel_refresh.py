"""The worker half of `novel_refresh`: the network part of v1's refresh (v1 spec 6.2, tasks 5).

`run` fetches the TOC and stores a missing cover (not once the heartbeat lost the claim), then
hands the result to the repo handler as an outcome: `Done(SyncSucceeded)`; `Failed(error,
SyncFailed)` for a site error, with the backed-off schedule; or `Retry` at
`now + REFRESH_MIN_INTERVAL` for an infrastructure error, which keeps the interval. A
`SiteUnreachable` that `Reachability` says has failed too often for the host counts as a site
error.
"""

from datetime import timedelta
from typing import Any, ClassVar

from novelhog import log
from novelhog.domain.refresh import jittered, next_interval
from novelhog.domain.tasks import Done, Failed, Outcome, Retry, TaskKind
from novelhog.repo.chapters import TocDiff
from novelhog.repo.task_kinds.novel_refresh import NovelClaim, SyncFailed, SyncSucceeded
from novelhog.repo.tasks import TaskRow
from novelhog.scraper_adapter.adapter import TocResult
from novelhog.scraper_adapter.errors import InfrastructureError, SiteError, SiteUnreachable
from novelhog.storage import StaticStorage, cover_key
from novelhog.worker.errors import summarize, unreachable_message
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
            ctx.reachability.succeeded(claim.host)
            cover = None
            if claim.cover_key is None and (ctx.beat is None or not ctx.beat.lost):
                cover = _store_cover(ctx.fetcher, ctx.storage, claim, toc.cover_url)
        except Exception as error:  # noqa: BLE001 - any other failure counts as a site error
            message = summarize(error)
            if isinstance(error, SiteError):
                ctx.reachability.succeeded(claim.host)  # the host answered
            gave_up = isinstance(error, SiteUnreachable) and ctx.reachability.failed(claim.host)
            if gave_up:
                message = unreachable_message(error)
            if isinstance(error, InfrastructureError) and not gave_up:
                logger.warning("refresh_infrastructure_error", error=message)
                return Retry(now + timedelta(seconds=min_s), message)
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
        return Done(SyncSucceeded(toc.urls, toc.title, toc.language, cover, min_s, max_s, ctx.rng))

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
