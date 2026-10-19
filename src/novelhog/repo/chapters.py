"""Chapters: listing, client state patches, queueing and the TOC diff (spec sections 4-6).

`queue` and `apply_toc` are internal helpers that take a `Connection`: the caller starts the
transaction, calls `novels.lock_novel` first, and appends the events last with
`events.emit_novel_change` (spec section 4, "Lock order").
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Connection,
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
from novelhog.domain.urls import normalize_url
from novelhog.repo import db, events
from novelhog.repo.context import RepoContext
from novelhog.repo.novels import lock_novel
from novelhog.repo.schema import chapters, novels
from novelhog.repo.schema import events as events_table

MAX_LIMIT = 5000
_ERROR_BATCH = 500


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
    row = conn.execute(select(chapters).where(chapters.c.id == chapter_id)).first()
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

    Sets `queued_at = now` and `priority = 0`; the stored content stays. Other chapters are
    reported as skipped with their current state. Writes rows only; the caller appends events for
    `changed_ids` (and must already hold `lock_novel`).
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
        values: dict[str, object]
        if target is ChapterState.PENDING:
            values = {"state": target.value, "queued_at": now, "priority": 0}
        else:
            values = {
                "state": target.value,
                "queued_at": None,
                "claimed_by": None,
                "claimed_until": None,
            }
        conn.execute(
            update(chapters)
            .where(chapters.c.id.in_([r.id for r in movable]))
            .values(**values, updated_at=now)
        )
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

    New URLs are inserted (queued with priority 1 when the novel is followed and this is not the
    initial import), known URLs take their new position (a removed one that came back gets its
    position again), and missing URLs lose their position; a missing chapter that is `pending`
    is also dequeued (spec section 6.2). Writes rows only; the caller holds `lock_novel` and
    appends the events for `TocDiff.changed_ids` last.
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
                "priority": 1 if queued else 0,
                "released_at": None if initial else now,
                "queued_at": now if queued else None,
                "updated_at": now,
            }
        )
    if new_rows:
        conn.execute(insert(chapters), new_rows)

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
            .values(
                state=ChapterState.NOT_REQUESTED.value,
                queued_at=None,
                claimed_by=None,
                claimed_until=None,
            )
        )
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


class Chapters:
    """Reads chapters and applies client state patches (the worker's writes live elsewhere)."""

    def __init__(self, context: RepoContext) -> None:
        self._engine = context.engine
        self._clock = context.clock
        self._static_base_url = context.static_base_url

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
                select(chapters)
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
            row = conn.execute(select(chapters).where(chapters.c.id == chapter_id)).first()
            if row is None or row.position is None:
                raise NotFound("chapter", chapter_id)
            return _views(conn, [row])[0]

    def patch(self, novel_id: str, target: ChapterState, selector: Selector) -> PatchResult:
        """Move the selected chapters to `target` (`pending` or `not_requested`).

        Chapters whose current state does not allow it are left alone and reported as skipped.
        Appends one `CH` event per updated chapter, one `NV` and the affected `EX` events.
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
            if result.changed_ids:
                events.emit_novel_change(
                    conn,
                    novel_id,
                    result.changed_ids,
                    now=now,
                    static_base_url=self._static_base_url,
                )
            return PatchResult(updated=result.updated, skipped=result.skipped)
