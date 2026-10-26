"""The worker half of `chapter_download`: the network part of v1's download (v1 6.4, tasks 5).

`run` fetches the chapter and converts it to Markdown, then hands the result to the repo handler.
The outcome follows how the request failed (`domain.hosts`):

- FlareSolverr down: `Retry` at the task's own `run_after` (it keeps its place in line) and the
  loop backs off;
- a failure that pauses the host (throttled, challenged, or the 2nd unreachable in a row):
  `Retry` at the end of the pause, which uses none of the chapter's retries;
- an unreachable or transient failure that does not pause the host: `Retry` after the chapter's
  backoff (1, 2, 4, 8, 16 min), then `Failed` once `CHAPTER_MAX_RETRIES` are used; an unreachable
  one fails as "Site unreachable: <message>";
- any other failure: `Failed` at once (the host answered, so its failure count is cleared).

The adapter protocols the download needs live here as well (`ChapterAdapter`, `ImageSource`).
"""

from typing import TYPE_CHECKING, Any, ClassVar, Protocol

from structlog.typing import FilteringBoundLogger

from novelhog import html_to_markdown, log
from novelhog.domain.hosts import CHAPTER_MAX_RETRIES, FailureKind, chapter_retry_delay
from novelhog.domain.tasks import Done, Failed, Outcome, Retry, RetryCause, TaskKind
from novelhog.images import FetchedImage
from novelhog.repo.task_kinds.chapter_download import ChapterClaim, Downloaded, DownloadReport
from novelhog.repo.tasks import TaskRow
from novelhog.scraper_adapter.adapter import FetchedChapter
from novelhog.scraper_adapter.errors import (
    HostFailureError,
    InfrastructureError,
    SiteError,
    SiteUnreachable,
)
from novelhog.worker.errors import log_host_paused, summarize, unreachable_message

if TYPE_CHECKING:
    # `base` imports the protocols below; `TaskContext` is needed for annotations only.
    from novelhog.worker.tasks.base import TaskContext


class ChapterAdapter(Protocol):
    """What the download needs from `ScraperAdapter`."""

    def fetch_chapter(self, novel_id: str, host: str, toc_url: str, url: str) -> FetchedChapter: ...

    def request_delay(self, host: str) -> float:
        """The host's own minimum pause between requests in seconds (0 if unset)."""
        ...


class ImageSource(Protocol):
    """What the download needs from `ImageFetcher`."""

    def fetch(self, url: str) -> FetchedImage | None: ...


class ChapterDownloadProcessor:
    """Downloads one chapter (v1 spec section 6.4) without holding a transaction."""

    kind: ClassVar[TaskKind] = TaskKind.CHAPTER_DOWNLOAD

    def run(self, task: TaskRow, work: ChapterClaim, ctx: TaskContext) -> Outcome[Downloaded]:
        """Fetch and convert the chapter; every failure becomes an outcome, none is raised."""
        claim = work
        logger = log.get(
            worker_id=ctx.settings.worker_id, novel_id=claim.novel_id, chapter_id=claim.chapter_id
        )
        try:
            fetched = ctx.adapter.fetch_chapter(
                claim.novel_id, claim.host, claim.toc_url, claim.url
            )
            conversion = html_to_markdown.convert(fetched.body_html, ctx.fetcher.fetch)
        except Exception as error:  # noqa: BLE001 - any failure is recorded on the chapter
            return self._failure(task, claim, ctx, error, logger)
        ctx.hosts.succeeded(claim.host)
        title = fetched.title or f"Chapter {claim.position}"
        return Done(Downloaded(conversion.markdown, title, conversion.has_images))

    def _failure(
        self,
        task: TaskRow,
        claim: ChapterClaim,
        ctx: TaskContext,
        error: Exception,
        logger: FilteringBoundLogger,
    ) -> Outcome[Downloaded]:
        message = summarize(error)
        if isinstance(error, InfrastructureError):
            logger.warning("download_infrastructure_error", error=message)
            return Retry(task.run_after, message, RetryCause.INFRASTRUCTURE)
        if not isinstance(error, HostFailureError):
            if isinstance(error, SiteError):
                ctx.hosts.succeeded(claim.host)  # the host answered
            logger.warning("download_failed", error=message, exc_info=True)
            return Failed(message)
        if error.kind is not FailureKind.TRANSIENT:
            host = ctx.hosts.failed(claim.host, error.kind, retry_after=error.retry_after)
            if host.paused_until is not None:
                log_host_paused(logger, claim.host, error.kind, host)
                return Retry(host.paused_until, message, RetryCause.HOST_PAUSED)
        if isinstance(error, SiteUnreachable):
            message = unreachable_message(error)
        if task.retries >= CHAPTER_MAX_RETRIES:
            logger.warning("download_failed", error=message)
            return Failed(message)
        run_after = ctx.clock() + chapter_retry_delay(task.retries)
        logger.warning("download_retry", error=message, run_after=run_after)
        return Retry(run_after, message, RetryCause.TRANSIENT)

    def stored_keys(self, outcome: Outcome[Downloaded]) -> list[str]:
        """Nothing: the content goes to the database inside the finish transaction."""
        return []

    def finished(self, task: TaskRow, outcome: Outcome[Any], report: object) -> None:
        """Warn when the chapter was no longer downloading, so the result was not applied."""
        if isinstance(report, DownloadReport) and report.state is None:
            log.get(chapter_id=report.chapter_id, novel_id=task.novel_id).warning(
                "chapter_not_downloading"
            )
