"""Export snapshots shared by the event writers (spec sections 4 and 5.2).

Task 11 extends this module with the exports repository; for now it only builds `ExportView`s
and finds the waiting exports a chapter change affects. These helpers are internal to `repo`.
"""

from collections.abc import Sequence

from sqlalchemy import Connection, and_, select

from novelhog.domain.chapters import ChapterState
from novelhog.domain.errors import NotFound
from novelhog.domain.exports import ExportState
from novelhog.domain.payloads import ExportBlocked, ExportProgress, ExportView
from novelhog.domain.urls import static_url
from novelhog.repo.schema import chapters, exports


def snapshot(conn: Connection, export_id: str, *, static_base_url: str) -> ExportView:
    """Return the export as the API would show it now; raise `NotFound` if it does not exist.

    The range is every chapter whose current position lies between those of the two end
    chapters (spec section 5.5); `progress` and `blocked` are derived from it (section 4,
    "Derived values"). If an end chapter was removed upstream the range is empty and both
    `chapter_from` and `chapter_to` are `None`.
    """
    row = conn.execute(select(exports).where(exports.c.id == export_id)).first()
    if row is None:
        raise NotFound("export", export_id)
    ends = {
        r.id: r.position
        for r in conn.execute(
            select(chapters.c.id, chapters.c.position).where(
                chapters.c.id.in_([row.first_chapter_id, row.last_chapter_id])
            )
        )
    }
    first, last = ends.get(row.first_chapter_id), ends.get(row.last_chapter_id)
    downloaded = 0
    in_range: list[tuple[int, str]] = []
    if first is not None and last is not None:
        low, high = sorted((first, last))
        in_range = [
            (r.position, r.state)
            for r in conn.execute(
                select(chapters.c.position, chapters.c.state)
                .where(
                    chapters.c.novel_id == row.novel_id,
                    chapters.c.position >= low,
                    chapters.c.position <= high,
                )
                .order_by(chapters.c.position)
            )
        ]
        downloaded = sum(1 for _, state in in_range if state == ChapterState.DOWNLOADED)
    state = ExportState(row.state)
    blocks = state is ExportState.WAITING and not row.skip_missing
    return ExportView(
        id=row.id,
        novel_id=row.novel_id,
        filename=row.filename,
        chapter_from=first,
        chapter_to=last,
        skip_missing=row.skip_missing,
        mime=row.mime,
        state=state,
        progress=ExportProgress(downloaded=downloaded, total=len(in_range)),
        blocked=ExportBlocked(
            errors=tuple(p for p, s in in_range if blocks and s == ChapterState.ERROR),
            not_queued=tuple(p for p, s in in_range if blocks and s == ChapterState.NOT_REQUESTED),
        ),
        waited=row.waited,
        created_at=row.created_at,
        ready_at=row.ready_at,
        error=row.error,
        size_bytes=row.size_bytes,
        url=None if row.storage_key is None else static_url(static_base_url, row.storage_key),
    )


def affected_waiting(
    conn: Connection,
    novel_id: str,
    changed_chapter_ids: Sequence[str],
    *,
    positions_changed: bool,
) -> list[str]:
    """Return the ids of the novel's waiting exports whose range contains a changed chapter.

    With `positions_changed` (a sync moved, removed or returned chapters, so ranges may have
    gained or lost members that are no longer where they were) every waiting export qualifies.
    """
    waiting = conn.execute(
        select(exports.c.id, exports.c.first_chapter_id, exports.c.last_chapter_id)
        .where(and_(exports.c.novel_id == novel_id, exports.c.state == ExportState.WAITING))
        .order_by(exports.c.created_at, exports.c.id)
    ).all()
    if not waiting:
        return []
    if positions_changed:
        return [row.id for row in waiting]
    if not changed_chapter_ids:
        return []
    wanted = {i for row in waiting for i in (row.first_chapter_id, row.last_chapter_id)}
    wanted.update(changed_chapter_ids)
    positions = {
        r.id: r.position
        for r in conn.execute(
            select(chapters.c.id, chapters.c.position).where(chapters.c.id.in_(wanted))
        )
    }
    changed = {positions[i] for i in changed_chapter_ids if positions.get(i) is not None}
    affected = []
    for row in waiting:
        first, last = positions.get(row.first_chapter_id), positions.get(row.last_chapter_id)
        if first is None or last is None:
            continue
        low, high = sorted((first, last))
        if any(low <= p <= high for p in changed):
            affected.append(row.id)
    return affected
