"""Host publishing and the TOC refresh (spec sections 6.1 to 6.3)."""

import random
from datetime import timedelta
from typing import Protocol

from novelhog import log
from novelhog.config import Settings
from novelhog.domain.refresh import jittered, next_interval
from novelhog.images import FetchedImage
from novelhog.repo import Repo
from novelhog.repo.novels import NovelClaim, SyncFailed, SyncOutcome, SyncSucceeded
from novelhog.scraper_adapter.adapter import TocResult
from novelhog.scraper_adapter.errors import InfrastructureError, SiteError, SiteUnreachable
from novelhog.storage import StaticStorage, cover_key
from novelhog.worker.errors import summarize, unreachable_message
from novelhog.worker.reachability import Reachability
from novelhog.worker.runtime import Heartbeat

SHRINK_MIN_KNOWN = 20
_COVER_EXTENSIONS = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/gif": "gif",
    "image/webp": "webp",
}


class TocAdapter(Protocol):
    """What the refresh needs from `ScraperAdapter`."""

    def supported_hosts(self) -> list[str]: ...

    def sync_toc(self, novel_id: str, host: str, toc_url: str) -> TocResult: ...


class CoverFetcher(Protocol):
    """What the refresh needs from `ImageFetcher`."""

    def fetch(self, url: str) -> FetchedImage | None: ...


def publish_hosts(repo: Repo, adapter: TocAdapter) -> None:
    """Publish the adapter's hosts as supported; hosts it dropped are marked unsupported."""
    report = repo.hosts.publish(adapter.supported_hosts())
    log.get().info("hosts_published", added=report.added, unsupported=report.unsupported)


def _fetch_toc(repo: Repo, adapter: TocAdapter, claim: NovelClaim) -> TocResult:
    """Run the sync; raise `SiteError` for an unsupported host or a TOC that shrank too much."""
    if not repo.hosts.is_supported(claim.host):
        msg = "Host no longer supported by the scraper"
        raise SiteError(msg)
    toc = adapter.sync_toc(claim.novel_id, claim.host, claim.toc_url)
    if not claim.initial:
        known = repo.novels.get(claim.novel_id).total_pages
        if known >= SHRINK_MIN_KNOWN and len(toc.urls) < known / 2:
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


def refresh(  # noqa: PLR0913
    repo: Repo,
    adapter: TocAdapter,
    fetcher: CoverFetcher,
    files: StaticStorage,
    claim: NovelClaim,
    *,
    settings: Settings,
    rng: random.Random,
    reachability: Reachability,
) -> None:
    """Sync one claimed novel and release its claim (spec 6.2).

    Site errors are recorded and backed off; an `InfrastructureError` is recorded as an
    unchanged-interval retry and re-raised so the loop can back off. A `SiteUnreachable` that
    `reachability` says has failed too often for the host is a site error.
    """
    logger = log.get(novel_id=claim.novel_id, host=claim.host, worker_id=settings.worker_id)
    worker_id = settings.worker_id
    ttl = settings.claim_ttl
    min_s, max_s = settings.refresh_min_interval, settings.refresh_max_interval
    now = repo.clock()
    outcome: SyncOutcome
    infrastructure: InfrastructureError | None = None

    with Heartbeat(
        lambda: repo.novels.renew_claim(claim.novel_id, worker_id, ttl), ttl / 3
    ) as beat:
        try:
            toc = _fetch_toc(repo, adapter, claim)
            reachability.succeeded(claim.host)
            cover = None
            if claim.cover_key is None and not beat.lost:
                cover = _store_cover(fetcher, files, claim, toc.cover_url)
            outcome = SyncSucceeded(toc.urls, toc.title, toc.language, cover, min_s, max_s, rng)
        except Exception as error:  # noqa: BLE001 - any other failure counts as a site error
            message = summarize(error)
            if isinstance(error, SiteError):
                reachability.succeeded(claim.host)  # the host answered
            gave_up = isinstance(error, SiteUnreachable) and reachability.failed(claim.host)
            if gave_up:
                message = unreachable_message(error)
            if isinstance(error, InfrastructureError) and not gave_up:
                logger.warning("refresh_infrastructure_error", error=message)
                infrastructure = error
                outcome = SyncFailed(None, claim.refresh_interval, now + timedelta(seconds=min_s))
            else:
                logger.warning("refresh_failed", error=message, exc_info=True)
                interval = next_interval(
                    claim.refresh_interval, found_new=False, failed=True, min_s=min_s, max_s=max_s
                )
                outcome = SyncFailed(
                    message if claim.initial else None,
                    interval,
                    now + timedelta(seconds=jittered(interval, rng)),
                )

    report = repo.novels.finish_sync(claim.novel_id, worker_id, outcome)
    if report is None:
        logger.warning("refresh_claim_lost", initial=claim.initial)
        if isinstance(outcome, SyncSucceeded) and outcome.cover_key is not None:
            repo.trash.discard([outcome.cover_key])  # stored for a sync that changed nothing
    else:
        if report.diff.removed:
            logger.warning("toc_chapters_removed", chapter_ids=report.diff.removed)
        if report.diff.dequeued:
            logger.warning("toc_chapters_dequeued", chapter_ids=report.diff.dequeued)
        logger.info(
            "refresh_finished", new=len(report.diff.new), failed=isinstance(outcome, SyncFailed)
        )
    if infrastructure is not None:
        raise infrastructure
