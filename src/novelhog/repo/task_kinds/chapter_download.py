"""The repo half of `chapter_download`: v1's download writes as a task handler (v1 6.4, tasks 5).

The download is still needed while the chapter is `pending` and has a position; `begin` then turns
it `downloading` and `work` reads the `ChapterClaim`. `complete` applies v1's `complete` (`Done`:
`downloaded` with the content stored) or `fail` (`Failed`: `error`); on `Retry` the chapter goes
back in line: `pending`, or `not_requested` with its task cancelled when a sync removed it
meanwhile (v1 `_back_in_line`). `recover` applies the same reset to a lapsed download. Every
chapter write is conditional on the chapter still being `downloading`, and every change queues the
builds it made buildable before its events (tasks spec 5.1).

Repair (tasks spec section 8): `problems` finds positioned `pending` chapters without a live
download (`chapter_pending_without_task`: enqueued at user priority), unpositioned `pending` ones
(`chapter_pending_without_position`: `not_requested`, queued task cancelled), `downloading` ones
without a running download (`chapter_downloading_without_task`: back in line and enqueued) and
queued downloads of chapters that are gone or need none (`queued_task_not_needed`: cancelled). A
chapter with a running download is never a problem; each fix appends the chapter's `CH` event.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, ClassVar

from sqlalchemy import ColumnElement, Connection, case, func, select, update

from novelhog.domain.chapters import ChapterState
from novelhog.domain.tasks import (
    LIVE_STATES,
    PRIORITY_USER,
    Begin,
    Done,
    Failed,
    Outcome,
    Retry,
    Skip,
    Started,
    TaskKind,
    TaskState,
)
from novelhog.repo import contents, events
from novelhog.repo import exports as export_repo
from novelhog.repo import tasks as task_queue
from novelhog.repo.context import RepoContext
from novelhog.repo.schema import chapters, novels, tasks
from novelhog.repo.tasks import Problem, Repair, TaskRow

_PENDING = ChapterState.PENDING.value
_DOWNLOADING = ChapterState.DOWNLOADING.value
_KIND = TaskKind.CHAPTER_DOWNLOAD
PENDING_WITHOUT_TASK = "chapter_pending_without_task"
PENDING_WITHOUT_POSITION = "chapter_pending_without_position"
DOWNLOADING_WITHOUT_TASK = "chapter_downloading_without_task"
TASK_NOT_NEEDED = "queued_task_not_needed"


def _chapter_checks() -> dict[str, ColumnElement[bool]]:
    """Each chapter problem of tasks spec section 8 as a condition on `chapters`."""
    positioned = chapters.c.position.is_not(None)
    no_live = ~task_queue.has_task(_KIND, chapters.c.id, LIVE_STATES)
    no_running = ~task_queue.has_task(_KIND, chapters.c.id, [TaskState.RUNNING])
    return {
        PENDING_WITHOUT_TASK: (chapters.c.state == _PENDING) & positioned & no_live,
        PENDING_WITHOUT_POSITION: (chapters.c.state == _PENDING) & ~positioned & no_running,
        DOWNLOADING_WITHOUT_TASK: (chapters.c.state == _DOWNLOADING) & no_running,
    }


def _not_needed() -> ColumnElement[bool]:
    """A queued download whose chapter is gone, `not_requested`, `downloaded` or `error`."""
    return task_queue.queued_unneeded(_KIND, chapters, [_PENDING, _DOWNLOADING])


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
        self._emit(conn, task.novel_id, task.entity_id, now)
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
                # A title the TOC gave wins over the page's own (spec section 6.2).
                "title": func.coalesce(chapters.c.title, outcome.result.title),
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
        self._emit(conn, task.novel_id, chapter_id, now, errors)
        return DownloadReport(chapter_id, state)

    def recover(self, conn: Connection, task: TaskRow, now: datetime) -> None:
        """The lapsed download went back to the queue: its chapter goes back in line."""
        self._back_in_line(conn, task, now)

    def rescheduled(self, conn: Connection, task: TaskRow, now: datetime) -> None:
        """Nothing changes for the chapter: it stays `pending` and `queued_at` is the task's."""

    def _back_in_line(self, conn: Connection, task: TaskRow, now: datetime) -> ChapterState | None:
        """Undo a started download: `pending`, keeping its place in line through the task.

        Returns the new state, or None when the chapter was no longer downloading.
        """
        state = self._reset(conn, task.entity_id, now)
        if state is not None:
            self._emit(conn, task.novel_id, task.entity_id, now)
        return state

    def _reset(self, conn: Connection, chapter_id: str, now: datetime) -> ChapterState | None:
        """`downloading` → `pending`, or `not_requested` without a position; rows only.

        A chapter without a position was removed by a sync while it downloaded, so it goes to
        `not_requested` and its queued task is cancelled (v1 `_back_in_line`). Returns the new
        state, or None when the chapter was not downloading.
        """
        positioned = chapters.c.position.is_not(None)
        state = conn.execute(
            update(chapters)
            .where(chapters.c.id == chapter_id, chapters.c.state == _DOWNLOADING)
            .values(
                state=case((positioned, _PENDING), else_=ChapterState.NOT_REQUESTED.value),
                updated_at=now,
            )
            .returning(chapters.c.state)
        ).scalar_one_or_none()
        if state is None:
            return None
        if state != _PENDING:
            task_queue.cancel(conn, self.kind, chapter_id, now=now)
        return ChapterState(state)

    def problems(self, conn: Connection) -> list[Problem]:
        """The chapters whose download task is missing or extra, and the unneeded downloads."""
        found: list[Problem] = []
        for name, condition in _chapter_checks().items():
            rows = conn.execute(
                select(chapters.c.id, chapters.c.novel_id).where(condition).order_by(chapters.c.id)
            )
            found.extend(Problem(name, row.id, row.novel_id) for row in rows)
        rows = conn.execute(
            select(tasks.c.id, tasks.c.entity_id, tasks.c.novel_id)
            .where(_not_needed())
            .order_by(tasks.c.id)
        )
        found.extend(Problem(TASK_NOT_NEEDED, row.entity_id, row.novel_id, row.id) for row in rows)
        return found

    def fix(self, conn: Connection, problem: Problem, now: datetime) -> Repair | None:
        """Re-check the problem and fix it (tasks spec section 8); None if it no longer holds."""
        chapter_id = problem.entity_id
        if problem.name == TASK_NOT_NEEDED:
            holds = conn.execute(
                select(tasks.c.id).where(tasks.c.id == problem.task_id, _not_needed())
            ).first()
            if holds is None:
                return None
            unneeded = task_queue.queued(conn, self.kind, chapter_id)
            task_queue.cancel(conn, self.kind, chapter_id, now=now)
            chapter = conn.execute(select(chapters.c.id).where(chapters.c.id == chapter_id)).first()
            if chapter is not None:
                self._emit(conn, problem.novel_id, chapter_id, now)
            return Repair.of(problem, self.kind, unneeded)
        row = conn.execute(
            select(chapters.c.position).where(
                chapters.c.id == chapter_id, _chapter_checks()[problem.name]
            )
        ).first()
        if row is None:
            return None
        task = task_queue.queued(conn, self.kind, chapter_id)
        if problem.name == PENDING_WITHOUT_POSITION:
            conn.execute(
                update(chapters)
                .where(chapters.c.id == chapter_id)
                .values(state=ChapterState.NOT_REQUESTED.value, updated_at=now)
            )
            task_queue.cancel(conn, self.kind, chapter_id, now=now)
            requeue = False
        elif problem.name == PENDING_WITHOUT_TASK:
            requeue = True
        else:  # downloading without a running task: back in line, as recovery would
            requeue = self._reset(conn, chapter_id, now) is ChapterState.PENDING
        if requeue:
            task = task_queue.enqueue(
                conn,
                self.kind,
                chapter_id,
                problem.novel_id,
                priority=PRIORITY_USER,
                ordinal=row.position,
                run_after=now,
                now=now,
            )
        self._emit(conn, problem.novel_id, chapter_id, now)
        return Repair.of(problem, self.kind, task)

    def _emit(
        self,
        conn: Connection,
        novel_id: str,
        chapter_id: str,
        now: datetime,
        errors: Mapping[str, tuple[str, datetime]] | None = None,
    ) -> None:
        """Queue the builds the change made buildable (tasks spec 5.1), then append the events."""
        changed = [chapter_id]
        affected = export_repo.affected_waiting(conn, novel_id, changed)
        export_repo.enqueue_buildable(conn, novel_id, affected, now)
        events.emit_novel_change(
            conn,
            novel_id,
            changed,
            now=now,
            static_base_url=self._static_base_url,
            errors=errors,
        )
