"""The per-chapter download pipeline (spec section 6.4)."""

import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from novelhog import html_to_markdown, log
from novelhog.config import Settings
from novelhog.images import FetchedImage
from novelhog.repo import Repo
from novelhog.repo.work import ChapterClaim
from novelhog.scraper_adapter.adapter import FetchedChapter
from novelhog.scraper_adapter.errors import InfrastructureError, SiteUnreachable
from novelhog.worker.errors import summarize
from novelhog.worker.reachability import Reachability
from novelhog.worker.runtime import Heartbeat


class ChapterAdapter(Protocol):
    """What the download needs from `ScraperAdapter`."""

    def fetch_chapter(self, novel_id: str, host: str, toc_url: str, url: str) -> FetchedChapter: ...

    def request_delay(self, host: str) -> float:
        """The host's own minimum pause between requests in seconds (0 if unset)."""
        ...


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


class _Claims:
    """The ids the heartbeat renews: the chapter in flight plus the not yet started rest.

    One lock makes `renew` and the hand-over of the current chapter exclusive, so a finished
    chapter (whose claim `complete`/`fail` clears) is never renewed afterwards.
    """

    def __init__(self, repo: Repo, worker_id: str, ttl: int, ids: list[str]) -> None:
        self._repo = repo
        self._worker_id = worker_id
        self._ttl = ttl
        self._lock = threading.Lock()
        self._current: str | None = None
        self.pending = list(ids)

    def begin(self, chapter_id: str) -> None:
        """The chapter has been started; its claim must now be kept alive."""
        with self._lock:
            self._current = chapter_id

    def end(self) -> None:
        """Stop renewing the current chapter; call before its claim is cleared."""
        with self._lock:
            self._current = None

    def take_pending(self, chapter_id: str) -> None:
        with self._lock:
            self.pending = [i for i in self.pending if i != chapter_id]

    def clear_pending(self) -> list[str]:
        with self._lock:
            rest, self.pending = self.pending, []
        return rest

    def renew(self) -> bool:
        """Renew everything held; False only when the chapter in flight lost its claim.

        Unstarted chapters that are no longer owned (dequeued, removed by a sync) are dropped;
        `start` skips them anyway.
        """
        with self._lock:
            current = self._current
            ids = ([current] if current is not None else []) + self.pending
            owned = set(self._repo.work.renew_chapters(ids, self._worker_id, self._ttl))
            self.pending = [i for i in self.pending if i in owned]
            return current is None or current in owned


def download(  # noqa: PLR0913
    repo: Repo,
    adapter: ChapterAdapter,
    fetcher: ImageSource,
    batch: list[ChapterClaim],
    *,
    settings: Settings,
    stop: threading.Event,
    reachability: Reachability,
) -> BatchReport:
    """Download the claimed chapters one by one (spec 6.4).

    Stops early (releasing the unstarted rest) when `stop` is set or the heartbeat reports a lost
    claim. An `InfrastructureError` requeues the current chapter, releases the rest and is
    re-raised, except that a `SiteUnreachable` which `reachability` says has failed too often for
    its host fails the chapter like a site error. Between chapters it waits the larger of
    `REQUEST_DELAY` and the host's own delay.
    """
    worker_id = settings.worker_id
    held = _Claims(repo, worker_id, settings.claim_ttl, [claim.chapter_id for claim in batch])
    counts = {"downloaded": 0, "failed": 0, "skipped": 0, "released": 0, "lost": 0}

    def release_rest() -> int:
        rest = held.clear_pending()
        return repo.work.release_chapters(rest, worker_id) if rest else 0

    try:
        with Heartbeat(held.renew, settings.claim_ttl / 3) as beat:
            for index, claim in enumerate(batch):
                if stop.is_set() or beat.lost:
                    rest = len(held.pending)
                    released = release_rest()
                    counts["released"] += released
                    counts["lost"] += rest - released
                    break
                held.take_pending(claim.chapter_id)
                _download_one(
                    repo,
                    adapter,
                    fetcher,
                    claim,
                    settings,
                    held,
                    counts,
                    release_rest,
                    reachability,
                )
                if index + 1 < len(batch):
                    stop.wait(max(settings.request_delay, adapter.request_delay(claim.host)))
    except Exception:
        release_rest()
        raise
    log.get(worker_id=worker_id).info("batch_finished", **counts)
    return BatchReport(**counts)


def _download_one(  # noqa: PLR0913
    repo: Repo,
    adapter: ChapterAdapter,
    fetcher: ImageSource,
    claim: ChapterClaim,
    settings: Settings,
    held: _Claims,
    counts: dict[str, int],
    release_rest: Callable[[], int],
    reachability: Reachability,
) -> None:
    worker_id = settings.worker_id
    chapter_log = log.get(worker_id=worker_id, novel_id=claim.novel_id, chapter_id=claim.chapter_id)
    if not repo.work.start(claim, worker_id):
        counts["skipped"] += 1
        chapter_log.info("chapter_skipped")
        return
    held.begin(claim.chapter_id)
    try:
        fetched = adapter.fetch_chapter(claim.novel_id, claim.host, claim.toc_url, claim.url)
        conversion = html_to_markdown.convert(fetched.body_html, fetcher.fetch)
        title = fetched.title or f"Chapter {claim.position}"
        held.end()
        reachability.succeeded(claim.host)
        done = repo.work.complete(
            claim.chapter_id,
            worker_id,
            markdown=conversion.markdown,
            title=title,
            has_images=conversion.has_images,
        )
    except Exception as error:  # noqa: BLE001 - any failure is recorded on the chapter
        held.end()
        message = summarize(error)
        if isinstance(error, SiteUnreachable) and reachability.failed(claim.host):
            message = f"Site unreachable: {message}"
        elif isinstance(error, InfrastructureError):
            chapter_log.warning("download_infrastructure_error", error=message)
            repo.work.requeue(claim.chapter_id, worker_id)
            release_rest()
            raise
        chapter_log.warning("download_failed", error=message, exc_info=True)
        counts["failed" if repo.work.fail(claim.chapter_id, worker_id, message) else "lost"] += 1
    else:
        if done:
            counts["downloaded"] += 1
        else:
            counts["lost"] += 1
            chapter_log.warning("download_claim_lost")
