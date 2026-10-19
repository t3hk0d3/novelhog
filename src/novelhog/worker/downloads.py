"""The per-chapter download pipeline (spec section 6.4)."""

import threading
from dataclasses import dataclass
from typing import Protocol

from novelhog import html_to_markdown, log
from novelhog.config import Settings
from novelhog.images import FetchedImage
from novelhog.repo import Repo
from novelhog.repo.work import ChapterClaim
from novelhog.scraper_adapter.adapter import FetchedChapter
from novelhog.scraper_adapter.errors import InfrastructureError
from novelhog.worker.runtime import Heartbeat

_SUMMARY_LIMIT = 300


class ChapterAdapter(Protocol):
    """What the download needs from `ScraperAdapter`."""

    def fetch_chapter(self, novel_id: str, host: str, toc_url: str, url: str) -> FetchedChapter: ...


class ImageSource(Protocol):
    """What the download needs from `ImageFetcher`."""

    def fetch(self, url: str) -> FetchedImage | None: ...


@dataclass(frozen=True)
class BatchReport:
    """What happened to a claimed batch."""

    downloaded: int
    failed: int
    skipped: int
    released: int
    lost: int


def _summary(error: Exception) -> str:
    text = " ".join(str(error).split())[:_SUMMARY_LIMIT]
    return f"{type(error).__name__}: {text}"


def download(  # noqa: PLR0913, PLR0912, PLR0915
    repo: Repo,
    adapter: ChapterAdapter,
    fetcher: ImageSource,
    batch: list[ChapterClaim],
    *,
    settings: Settings,
    stop: threading.Event,
) -> BatchReport:
    """Download the claimed chapters one by one (spec 6.4).

    Stops early (releasing the unstarted rest) when `stop` is set or the heartbeat reports a lost
    claim. An `InfrastructureError` requeues the current chapter, releases the rest and is
    re-raised.
    """
    worker_id = settings.worker_id
    logger = log.get(worker_id=worker_id)
    remaining = [
        claim.chapter_id for claim in batch
    ]  # replaced, never mutated (heartbeat reads it)
    downloaded = failed = skipped = lost = released = 0

    def renew() -> bool:
        ids = remaining
        return len(repo.work.renew_chapters(ids, worker_id, settings.claim_ttl)) == len(ids)

    def release_rest(ids: list[str]) -> int:
        return repo.work.release_chapters(ids, worker_id) if ids else 0

    with Heartbeat(renew, settings.claim_ttl / 3) as beat:
        for index, claim in enumerate(batch):
            if stop.is_set() or beat.lost:
                released += release_rest(remaining)
                remaining = []
                break
            remaining = [c.chapter_id for c in batch[index:]]
            chapter_log = log.get(
                worker_id=worker_id, novel_id=claim.novel_id, chapter_id=claim.chapter_id
            )
            if not repo.work.start(claim, worker_id):
                skipped += 1
                chapter_log.info("chapter_skipped")
                continue
            try:
                fetched = adapter.fetch_chapter(
                    claim.novel_id, claim.host, claim.toc_url, claim.url
                )
                conversion = html_to_markdown.convert(fetched.body_html, fetcher.fetch)
                title = fetched.title or f"Chapter {claim.position}"
                done = repo.work.complete(
                    claim.chapter_id,
                    worker_id,
                    markdown=conversion.markdown,
                    title=title,
                    has_images=conversion.has_images,
                )
            except InfrastructureError as error:
                chapter_log.warning("download_infrastructure_error", error=_summary(error))
                repo.work.requeue(claim.chapter_id, worker_id)
                remaining = [c.chapter_id for c in batch[index + 1 :]]
                release_rest(remaining)
                raise
            except Exception as error:  # noqa: BLE001 - any failure is recorded on the chapter
                chapter_log.warning("download_failed", error=_summary(error), exc_info=True)
                done = None
                if repo.work.fail(claim.chapter_id, worker_id, _summary(error)):
                    failed += 1
                else:
                    lost += 1
            else:
                if done:
                    downloaded += 1
                else:
                    lost += 1
                    chapter_log.warning("download_claim_lost")
            remaining = [c.chapter_id for c in batch[index + 1 :]]
            if remaining:
                stop.wait(settings.request_delay)
    logger.info(
        "batch_finished",
        downloaded=downloaded,
        failed=failed,
        skipped=skipped,
        released=released,
        lost=lost,
    )
    return BatchReport(downloaded, failed, skipped, released, lost)
