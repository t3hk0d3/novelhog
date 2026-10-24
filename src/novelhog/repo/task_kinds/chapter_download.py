"""The repo half of `chapter_download`: v1's download writes as a task handler (v1 6.4, tasks 5).

The download is still needed while the chapter is `pending` and has a position; `begin` then turns
it `downloading` and `work` reads the `ChapterClaim`. `complete` applies v1's `complete` (`Done`:
`downloaded` with the content stored) or `fail` (`Failed`: `error`); on `Retry` the chapter goes
back in line: `pending`, or `not_requested` with its task cancelled when a sync removed it
meanwhile (v1 `_back_in_line`). `recover` applies the same reset to a lapsed download. Every
chapter write is conditional on the chapter still being `downloading`, and every change queues the
builds it made buildable before its events (tasks spec 5.1).
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, ClassVar

from sqlalchemy import Connection, case, select, update

from novelhog.domain.chapters import ChapterState
from novelhog.domain.tasks import Begin, Done, Failed, Outcome, Retry, Skip, Started, TaskKind
from novelhog.repo import contents, events
from novelhog.repo import exports as export_repo
from novelhog.repo import tasks as task_queue
from novelhog.repo.context import RepoContext
from novelhog.repo.schema import chapters, novels
from novelhog.repo.tasks import TaskRow

_PENDING = ChapterState.PENDING.value
_DOWNLOADING = ChapterState.DOWNLOADING.value


@dataclass(frozen=True)
class ChapterClaim:
    """A chapter whose download started, with what the worker needs to download it."""

    chapter_id: str
    novel_id: str
    host: str
    toc_url: str
    position: int
    url: str


@dataclass(frozen=True)
class Downloaded:
    """A fetched and converted chapter: its Markdown, title and whether it holds images."""

    markdown: str
    title: str
    has_images: bool


@dataclass(frozen=True)
class DownloadReport:
    """What `complete` did: the chapter's new state, or None when it was no longer downloading."""

    chapter_id: str
    state: ChapterState | None


class ChapterDownloadHandler:
    """The transactional half of `chapter_download`; `complete` reports the chapter's new state."""

    kind: ClassVar[TaskKind] = TaskKind.CHAPTER_DOWNLOAD

    def __init__(self, context: RepoContext) -> None:
        self._static_base_url = context.static_base_url

    def work(self, conn: Connection, task: TaskRow) -> ChapterClaim:
        """Read what the download needs (v1 `ChapterClaim`)."""
        row = conn.execute(
            select(
                chapters.c.id,
                chapters.c.novel_id,
                chapters.c.position,
                chapters.c.url,
                novels.c.host,
                novels.c.toc_url,
            )
            .join(novels, novels.c.id == chapters.c.novel_id)
            .where(chapters.c.id == task.entity_id)
        ).one()
        return ChapterClaim(
            chapter_id=row.id,
            novel_id=row.novel_id,
            host=row.host,
            toc_url=row.toc_url,
            position=row.position,
            url=row.url,
        )

    def begin(self, conn: Connection, task: TaskRow, now: datetime) -> Begin:
        """Still needed while the chapter is pending and positioned; it turns `downloading`."""
        started = conn.execute(
            update(chapters)
            .where(
                chapters.c.id == task.entity_id,
                chapters.c.state == _PENDING,
                chapters.c.position.is_not(None),
            )
            .values(state=_DOWNLOADING, updated_at=now)
        ).rowcount
        if started != 1:
            return Skip("not pending")
        self._emit(conn, task, now)
        return Started()

    def complete(
        self, conn: Connection, task: TaskRow, outcome: Outcome[Any], now: datetime
    ) -> DownloadReport:
        """`Done` → `downloaded` with the content; `Failed` → `error`; `Retry` → back in line."""
        chapter_id = task.entity_id
        if isinstance(outcome, Retry):
            return DownloadReport(chapter_id, self._back_in_line(conn, task, now))
        markdown: str | None = None
        errors: dict[str, tuple[str, datetime]] | None = None
        if isinstance(outcome, Done) and isinstance(outcome.result, Downloaded):
            state = ChapterState.DOWNLOADED
            markdown = outcome.result.markdown
            values: dict[str, object] = {
                "title": outcome.result.title,
                "has_images": outcome.result.has_images,
            }
        elif isinstance(outcome, Failed):
            state = ChapterState.ERROR
            values = {}
            errors = {chapter_id: (outcome.error, now)}
        else:
            message = f"chapter_download cannot complete with {outcome!r}"
            raise TypeError(message)
        ended = conn.execute(
            update(chapters)
            .where(chapters.c.id == chapter_id, chapters.c.state == _DOWNLOADING)
            .values({**values, "state": state.value, "updated_at": now})
        ).rowcount
        if ended != 1:
            return DownloadReport(chapter_id, None)
        if markdown is not None:
            contents.put(conn, chapter_id, markdown, now)
        self._emit(conn, task, now, errors)
        return DownloadReport(chapter_id, state)

    def recover(self, conn: Connection, task: TaskRow, now: datetime) -> None:
        """The lapsed download went back to the queue: its chapter goes back in line."""
        self._back_in_line(conn, task, now)

    def _back_in_line(self, conn: Connection, task: TaskRow, now: datetime) -> ChapterState | None:
        """Undo a started download: `pending`, keeping its place in line through the task.

        A chapter without a position was removed by a sync while it downloaded, so it goes to
        `not_requested` and its re-queued task is cancelled (v1 `_back_in_line`). Returns the new
        state, or None when the chapter was no longer downloading.
        """
        positioned = chapters.c.position.is_not(None)
        state = conn.execute(
            update(chapters)
            .where(chapters.c.id == task.entity_id, chapters.c.state == _DOWNLOADING)
            .values(
                state=case((positioned, _PENDING), else_=ChapterState.NOT_REQUESTED.value),
                updated_at=now,
            )
            .returning(chapters.c.state)
        ).scalar_one_or_none()
        if state is None:
            return None
        if state != _PENDING:
            task_queue.cancel(conn, self.kind, task.entity_id, now=now)
        self._emit(conn, task, now)
        return ChapterState(state)

    def _emit(
        self,
        conn: Connection,
        task: TaskRow,
        now: datetime,
        errors: Mapping[str, tuple[str, datetime]] | None = None,
    ) -> None:
        """Queue the builds the change made buildable (tasks spec 5.1), then append the events."""
        changed = [task.entity_id]
        affected = export_repo.affected_waiting(conn, task.novel_id, changed)
        export_repo.enqueue_buildable(conn, task.novel_id, affected, now)
        events.emit_novel_change(
            conn,
            task.novel_id,
            changed,
            now=now,
            static_base_url=self._static_base_url,
            errors=errors,
        )
