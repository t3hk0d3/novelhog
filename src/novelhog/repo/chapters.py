"""Chapters: listing, client state patches, queueing and the TOC diff (spec sections 4-6).

`queue` and `apply_toc` are internal helpers that take a `Connection`: the caller starts the
transaction, calls `novels.lock_novel` first, and appends the events last with
`events.emit_novel_change` (spec section 4, "Lock order").

A chapter's download is a `chapter_download` task (tasks spec section 3). Every move to `pending`
enqueues it and every move from `pending` to `not_requested` cancels it, in the same transaction,
before the events. `queued_at` is derived: the live download task's `created_at`.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import (
    ColumnElement,
    Connection,
    FromClause,
    Select,
    and_,
    bindparam,
    func,
    insert,
    select,
    update,
)

from novelhog import ids
from novelhog.domain.chapters import (
    CLIENT_TARGETS,
    ChapterState,
    Selector,
    resolve_selector,
    transition_allowed,
)
from novelhog.domain.errors import NotFound, ValidationFailed
from novelhog.domain.payloads import ChapterView
from novelhog.domain.tasks import PRIORITY_RELEASE, PRIORITY_USER, TaskKind, TaskState
from novelhog.domain.urls import normalize_url
from novelhog.repo import db, events
from novelhog.repo.context import RepoContext
from novelhog.repo.novels import lock_novel
from novelhog.repo.schema import chapter_contents, chapters, novels, tasks
from novelhog.repo.schema import events as events_table

# Imported after `novels`, which `tasks` itself imports.
from novelhog.repo.tasks import TaskQueue, cancel_many, enqueue_many

MAX_LIMIT = 5000
_ERROR_BATCH = 500
_DOWNLOAD = TaskKind.CHAPTER_DOWNLOAD
_queued_download = tasks.alias("queued_download")
_running_download = tasks.alias("running_download")


@dataclass(frozen=True)
class PatchResult:
    """The outcome of a state patch: updated positions, and skipped ones with their state."""

    updated: list[int]
    skipped: list[tuple[int, ChapterState]]


@dataclass(frozen=True)
class QueuedChapters(PatchResult):
    """A `PatchResult` that also names the changed chapters, for the caller's events."""

    changed_ids: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class TocDiff:
    """What a TOC sync changed (spec section 6.2).

    `new` holds the positions of newly announced chapters (empty on the initial import); the
    other lists hold chapter ids. `inserted` names every inserted chapter (the initial import
    included) and `dequeued` the removed chapters that were also dequeued, for the caller to log.
    """

    new: list[int]
    moved: list[str]
    removed: list[str]
    returned: list[str]
    inserted: list[str] = field(default_factory=list)
    dequeued: list[str] = field(default_factory=list)

    @property
    def changed_ids(self) -> list[str]:
        """Every inserted, moved, removed or returned chapter, for `emit_novel_change`."""
        return [*self.inserted, *self.moved, *self.removed, *self.returned]


def _download_task(alias: FromClause, state: TaskState) -> ColumnElement[bool]:
    """Join condition: the chapter's download task in `state` (at most one, tasks spec 3)."""
    return and_(
        alias.c.kind == _DOWNLOAD.value,
        alias.c.entity_id == chapters.c.id,
        alias.c.state == state.value,
    )


def _select_chapters() -> Select[Any]:
    """Chapter rows with the derived `queued_at`: the live download task's `created_at`."""
    queued_at = func.coalesce(_queued_download.c.created_at, _running_download.c.created_at)
    return select(chapters, queued_at.label("queued_at")).select_from(
        chapters.outerjoin(
            _queued_download, _download_task(_queued_download, TaskState.QUEUED)
        ).outerjoin(_running_download, _download_task(_running_download, TaskState.RUNNING))
    )


def _enqueue_downloads(
    conn: Connection,
    novel_id: str,
    positioned_ids: Sequence[tuple[str, int]],
    *,
    release: bool,
    now: datetime,
) -> None:
    """Queue a download for each `(chapter id, position)`, due now (tasks spec section 4).

    `release` gives an auto-queued release its higher priority. The caller holds `lock_novel`
    and calls `TaskQueue.stamp` last.
    """
    priority = PRIORITY_RELEASE if release else PRIORITY_USER
    enqueue_many(
        conn, _DOWNLOAD, novel_id, positioned_ids, priority=priority, run_after=now, now=now
    )


def _cancel_downloads(conn: Connection, chapter_ids: Sequence[str], *, now: datetime) -> None:
    """Cancel the queued download of each chapter; a running one is left to finish."""
    cancel_many(conn, _DOWNLOAD, chapter_ids, now=now)


def _parse_time(value: object) -> datetime:
    return datetime.fromisoformat(str(value))


def errors_of(conn: Connection, chapter_ids: Iterable[str]) -> dict[str, tuple[str, datetime]]:
    """Return `(error, error_at)` from the newest `CH` event of each chapter that carries one."""
    wanted = list(dict.fromkeys(chapter_ids))
    found: dict[str, tuple[str, datetime]] = {}
    for start in range(0, len(wanted), _ERROR_BATCH):
        batch = wanted[start : start + _ERROR_BATCH]
        newest = (
            select(func.max(events_table.c.seq))
            .where(events_table.c.entity_id.in_(batch))
            .group_by(events_table.c.entity_id)
        )
        rows = conn.execute(
            select(events_table.c.entity_id, events_table.c.payload).where(
                events_table.c.seq.in_(newest)
            )
        ).all()
        for row in rows:
            error, error_at = row.payload.get("error"), row.payload.get("error_at")
            if error is not None and error_at is not None:
                found[row.entity_id] = (str(error), _parse_time(error_at))
    return found


def error_of(conn: Connection, chapter_id: str) -> tuple[str, datetime] | None:
    """Return the chapter's `(error, error_at)` from its newest `CH` event, if that carries one."""
    return errors_of(conn, [chapter_id]).get(chapter_id)


def _views(
    conn: Connection, rows: Sequence[Any], errors: dict[str, tuple[str, datetime]] | None = None
) -> list[ChapterView]:
    """Build views for positioned chapter rows; errors come from the newest events by default."""
    if errors is None:
        errors = errors_of(conn, [r.id for r in rows if r.state == ChapterState.ERROR])
    views = []
    for row in rows:
        error, error_at = errors.get(row.id, (None, None))
        views.append(
            ChapterView(
                id=row.id,
                position=row.position,
                url=row.url,
                title=row.title,
                state=ChapterState(row.state),
                has_images=row.has_images,
                released_at=row.released_at,
                queued_at=row.queued_at,
                updated_at=row.updated_at,
                error=error,
                error_at=error_at,
            )
        )
    return views


def event_payload(
    conn: Connection,
    novel_id: str,
    chapter_id: str,
    errors: dict[str, tuple[str, datetime]],
) -> dict[str, object]:
    """Return the `CH` event payload: the chapter as `GET` would show it, plus `novel_id`.

    A chapter in `error` takes its message from `errors` (a failure just recorded) or else
    carries the previous snapshot's over; removed chapters have a null `position`.
    """
    row = conn.execute(_select_chapters().where(chapters.c.id == chapter_id)).first()
    if row is None:
        raise NotFound("chapter", chapter_id)
    error, error_at = None, None
    if row.state == ChapterState.ERROR:
        known = errors.get(chapter_id) or error_of(conn, chapter_id)
        if known is not None:
            error, error_at = known
    view = _views(conn, [row], {})[0]
    return view.to_event(novel_id, error, error_at)


def positioned(novel_id: str) -> Any:
    return (chapters.c.novel_id == novel_id) & chapters.c.position.is_not(None)


def queue(
    conn: Connection, novel_id: str, positions: Sequence[int], *, now: datetime
) -> QueuedChapters:
    """Move the chapters at `positions` to `pending` where the transition table allows it.

    Each moved chapter gets its download task at user priority, due now; the stored content
    stays. Other chapters are reported as skipped with their current state. Writes rows only; the
    caller appends events for `changed_ids` (and must already hold `lock_novel`) and calls
    `TaskQueue.stamp` last.
    """
    return _transition(conn, novel_id, positions, ChapterState.PENDING, now=now)


def _transition(
    conn: Connection,
    novel_id: str,
    positions: Sequence[int],
    target: ChapterState,
    *,
    now: datetime,
) -> QueuedChapters:
    found = conn.execute(
        select(chapters.c.id, chapters.c.position, chapters.c.state)
        .where(positioned(novel_id), chapters.c.position.in_(positions))
        .order_by(chapters.c.position)
    ).all()
    movable = [r for r in found if transition_allowed(ChapterState(r.state), target)]
    movable_ids = {r.id for r in movable}
    skipped = [(r.position, ChapterState(r.state)) for r in found if r.id not in movable_ids]
    if movable:
        conn.execute(
            update(chapters)
            .where(chapters.c.id.in_([r.id for r in movable]))
            .values(state=target.value, updated_at=now)
        )
        if target is ChapterState.PENDING:
            positions_of = [(r.id, r.position) for r in movable]
            _enqueue_downloads(conn, novel_id, positions_of, release=False, now=now)
        else:
            _cancel_downloads(conn, [r.id for r in movable], now=now)
    return QueuedChapters(
        updated=[r.position for r in movable],
        skipped=skipped,
        changed_ids=[r.id for r in movable],
    )


def apply_toc(
    conn: Connection,
    novel_id: str,
    urls: Sequence[str],
    *,
    initial: bool,
    followed: bool,
    now: datetime,
) -> TocDiff:
    """Diff the TOC against the novel's chapter rows by normalised URL and write the changes.

    New URLs are inserted (`pending` with a download task at release priority when the novel is
    followed and this is not the initial import), known URLs take their new position (a removed
    one that came back gets its position again), and missing URLs lose their position; a missing
    chapter that is `pending` is also dequeued and its download task cancelled (spec section
    6.2). Writes rows only; the caller holds `lock_novel`, appends the events for
    `TocDiff.changed_ids` and calls `TaskQueue.stamp` last.
    """
    toc = list(dict.fromkeys(normalize_url(url) for url in urls))
    position_of = {url: index for index, url in enumerate(toc, start=1)}
    existing = {
        row.url: row
        for row in conn.execute(select(chapters).where(chapters.c.novel_id == novel_id)).all()
    }
    queued = followed and not initial
    new_rows: list[dict[str, Any]] = []
    for url in toc:
        if url in existing:
            continue
        new_rows.append(
            {
                "id": ids.new_id("CH"),
                "novel_id": novel_id,
                "url": url,
                "position": position_of[url],
                "state": ChapterState.PENDING.value if queued else ChapterState.NOT_REQUESTED.value,
                "released_at": None if initial else now,
                "updated_at": now,
            }
        )
    if new_rows:
        conn.execute(insert(chapters), new_rows)
        if queued:
            releases = [(r["id"], r["position"]) for r in new_rows]
            _enqueue_downloads(conn, novel_id, releases, release=True, now=now)

    moved: list[str] = []
    returned: list[str] = []
    shifts: list[dict[str, Any]] = []
    for url, row in existing.items():
        position = position_of.get(url)
        if position is None or position == row.position:
            continue
        (moved if row.position is not None else returned).append(row.id)
        shifts.append({"b_id": row.id, "b_position": position})
    if shifts:
        conn.execute(
            update(chapters)
            .where(chapters.c.id == bindparam("b_id"))
            .values(position=bindparam("b_position"), removed_at=None, updated_at=now),
            shifts,
        )

    gone = [
        row for url, row in existing.items() if url not in position_of and row.position is not None
    ]
    gone.sort(key=lambda row: row.position)
    removed = [row.id for row in gone]
    dequeued = [row.id for row in gone if row.state == ChapterState.PENDING]
    if removed:
        conn.execute(
            update(chapters)
            .where(chapters.c.id.in_(removed))
            .values(position=None, removed_at=now, updated_at=now)
        )
    if dequeued:
        conn.execute(
            update(chapters)
            .where(chapters.c.id.in_(dequeued))
            .values(state=ChapterState.NOT_REQUESTED.value)
        )
        _cancel_downloads(conn, dequeued, now=now)
    by_position = {row.id: position_of[url] for url, row in existing.items() if url in position_of}
    new_ids = [r["id"] for r in new_rows]
    return TocDiff(
        new=[] if initial else [r["position"] for r in new_rows],
        moved=sorted(moved, key=lambda i: by_position[i]),
        removed=removed,
        returned=sorted(returned, key=lambda i: by_position[i]),
        inserted=new_ids,
        dequeued=dequeued,
    )


@dataclass(frozen=True)
class ChapterText:
    """A chapter to read: the chapter, its stored Markdown and the ids of the chapters around it.

    `previous_id` and `next_id` are the chapters at the positions before and after it, in any
    state; None at either end of the table of contents.
    """

    chapter: ChapterView
    markdown: str
    fetched_at: datetime
    previous_id: str | None
    next_id: str | None


class Chapters:
    """Reads chapters and applies client state patches (the worker's writes live elsewhere)."""

    def __init__(self, context: RepoContext) -> None:
        self._engine = context.engine
        self._clock = context.clock
        self._static_base_url = context.static_base_url
        self._queue = TaskQueue(context)

    def list(
        self,
        novel_id: str,
        *,
        offset: int = 0,
        limit: int = 1000,
        state: ChapterState | None = None,
    ) -> tuple[list[ChapterView], int]:
        """Return a page of the novel's positioned chapters by position, and the total count.

        The total counts the chapters matching `state`. `limit` is capped at 5000.
        """
        if not ids.is_valid(novel_id, "NV"):
            raise NotFound("novel", novel_id)
        where = positioned(novel_id)
        if state is not None:
            where = where & (chapters.c.state == state.value)
        with db.read(self._engine) as conn:
            if conn.execute(select(novels.c.id).where(novels.c.id == novel_id)).first() is None:
                raise NotFound("novel", novel_id)
            total = conn.execute(select(func.count()).select_from(chapters).where(where)).scalar()
            rows = conn.execute(
                _select_chapters()
                .where(where)
                .order_by(chapters.c.position)
                .offset(max(offset, 0))
                .limit(min(max(limit, 0), MAX_LIMIT))
            ).all()
            return _views(conn, rows), int(total or 0)

    def get(self, chapter_id: str) -> ChapterView:
        """Return one chapter; raise `NotFound` for an unknown, malformed or removed one."""
        if not ids.is_valid(chapter_id, "CH"):
            raise NotFound("chapter", chapter_id)
        with db.read(self._engine) as conn:
            row = conn.execute(_select_chapters().where(chapters.c.id == chapter_id)).first()
            if row is None or row.position is None:
                raise NotFound("chapter", chapter_id)
            return _views(conn, [row])[0]

    def text(self, novel_id: str, chapter_id: str) -> ChapterText:
        """Return the chapter of the novel with its stored Markdown, in one read.

        Raise `NotFound` for an unknown novel (`novel`), a chapter that is not the novel's or was
        removed from its table of contents (`chapter`), or one with no stored content
        (`chapter_content`). A chapter being downloaded again keeps its earlier content.
        """
        if not ids.is_valid(novel_id, "NV"):
            raise NotFound("novel", novel_id)
        if not ids.is_valid(chapter_id, "CH"):
            raise NotFound("chapter", chapter_id)
        with db.read(self._engine) as conn:
            if conn.execute(select(novels.c.id).where(novels.c.id == novel_id)).first() is None:
                raise NotFound("novel", novel_id)
            row = conn.execute(
                _select_chapters().where(chapters.c.id == chapter_id, positioned(novel_id))
            ).first()
            if row is None:
                raise NotFound("chapter", chapter_id)
            stored = conn.execute(
                select(chapter_contents.c.markdown, chapter_contents.c.fetched_at).where(
                    chapter_contents.c.chapter_id == chapter_id
                )
            ).first()
            if stored is None:
                raise NotFound("chapter_content", chapter_id)
            previous_id = conn.execute(
                select(chapters.c.id)
                .where(positioned(novel_id), chapters.c.position < row.position)
                .order_by(chapters.c.position.desc())
                .limit(1)
            ).scalar_one_or_none()
            next_id = conn.execute(
                select(chapters.c.id)
                .where(positioned(novel_id), chapters.c.position > row.position)
                .order_by(chapters.c.position)
                .limit(1)
            ).scalar_one_or_none()
            return ChapterText(
                _views(conn, [row])[0], stored.markdown, stored.fetched_at, previous_id, next_id
            )

    def headings(self, chapter_ids: Sequence[str]) -> dict[str, tuple[int, str | None]]:
        """Return `{id: (position, title)}` in one read for those chapters that have a position.

        Unknown, malformed and removed chapters are left out.
        """
        if not chapter_ids:
            return {}
        with db.read(self._engine) as conn:
            rows = conn.execute(
                select(chapters.c.id, chapters.c.position, chapters.c.title).where(
                    chapters.c.id.in_(list(chapter_ids)), chapters.c.position.is_not(None)
                )
            )
            return {r.id: (r.position, r.title) for r in rows}

    def patch(self, novel_id: str, target: ChapterState, selector: Selector) -> PatchResult:
        """Move the selected chapters to `target` (`pending` or `not_requested`).

        Chapters whose current state does not allow it are left alone and reported as skipped.
        Appends one `CH` event per updated chapter, one `NV` and the affected `EX` events.

        Dequeuing (`not_requested`) fails the novel's waiting, non-`skip_missing` exports whose
        range contains a dequeued chapter (spec section 5.5) and cancels their queued builds; the
        TOC sync never does. Waiting exports the change made buildable get their build queued
        (tasks spec 5.1).
        """
        if target not in CLIENT_TARGETS:
            raise ValidationFailed(  # noqa: TRY003
                "invalid_target_state", f"Chapters cannot be set to {target.value}"
            )
        now = self._clock()
        with db.write(self._engine) as conn:
            if not ids.is_valid(novel_id, "NV") or not lock_novel(conn, novel_id):
                raise NotFound("novel", novel_id)
            total = conn.execute(
                select(func.count()).select_from(chapters).where(positioned(novel_id))
            ).scalar_one()
            positions = resolve_selector(selector, total)
            result = _transition(conn, novel_id, positions, target, now=now)
            if not result.changed_ids:
                return PatchResult(updated=result.updated, skipped=result.skipped)
            # Imported here: exports imports this module.
            from novelhog.repo import exports  # noqa: PLC0415

            failed: list[str] = []
            if target is ChapterState.NOT_REQUESTED:
                failed = exports.fail_cancelled(conn, novel_id, result.changed_ids, now)
            affected = exports.affected_waiting(conn, novel_id, result.changed_ids)
            exports.enqueue_buildable(conn, novel_id, affected, now)
            events.emit_novel_change(
                conn,
                novel_id,
                result.changed_ids,
                now=now,
                static_base_url=self._static_base_url,
                failed_export_ids=failed,
            )
            self._queue.stamp(conn)
            return PatchResult(updated=result.updated, skipped=result.skipped)
