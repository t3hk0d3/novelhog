"""The worker half of `chapter_download`: the network part of v1's download (v1 6.4, tasks 5).

`run` fetches the chapter and converts it to Markdown, then hands the result to the repo handler:
`Done(Downloaded)`; `Failed('<Type>: <summary>')` for a site error or any other failure; or `Retry`
at the task's own `run_after` for an infrastructure error, so the chapter keeps its place in line.
A `SiteUnreachable` that `Reachability` says has failed too often for the host counts as a site
error, with the message "Site unreachable: <message>" (v1 spec 6.1).

The adapter protocols the download needs live here as well (`ChapterAdapter`, `ImageSource`).
"""

from typing import TYPE_CHECKING, Any, ClassVar, Protocol

from novelhog import html_to_markdown, log
from novelhog.domain.tasks import Done, Failed, Outcome, Retry, TaskKind
from novelhog.images import FetchedImage
from novelhog.repo.task_kinds.chapter_download import ChapterClaim, Downloaded, DownloadReport
from novelhog.repo.tasks import TaskRow
from novelhog.scraper_adapter.adapter import FetchedChapter
from novelhog.scraper_adapter.errors import InfrastructureError, SiteError, SiteUnreachable
from novelhog.worker.errors import summarize, unreachable_message

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
            message = summarize(error)
            if isinstance(error, SiteError):
                ctx.reachability.succeeded(claim.host)  # the host answered
            gave_up = isinstance(error, SiteUnreachable) and ctx.reachability.failed(claim.host)
            if gave_up:
                message = unreachable_message(error)
            if isinstance(error, InfrastructureError) and not gave_up:
                logger.warning("download_infrastructure_error", error=message)
                return Retry(task.run_after, message)
            logger.warning("download_failed", error=message, exc_info=True)
            return Failed(message)
        ctx.reachability.succeeded(claim.host)
        title = fetched.title or f"Chapter {claim.position}"
        return Done(Downloaded(conversion.markdown, title, conversion.has_images))

    def stored_keys(self, outcome: Outcome[Downloaded]) -> list[str]:
        """Nothing: the content goes to the database inside the finish transaction."""
        return []

    def finished(self, task: TaskRow, outcome: Outcome[Any], report: object) -> None:
        """Warn when the chapter was no longer downloading, so the result was not applied."""
        if isinstance(report, DownloadReport) and report.state is None:
            log.get(chapter_id=report.chapter_id, novel_id=task.novel_id).warning(
                "chapter_not_downloading"
            )
