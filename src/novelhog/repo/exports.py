"""Exports: snapshots, the repository and the worker's claim, finish and fail (spec 4, 5.5, 6.5).

`snapshot` and `affected_waiting` are internal helpers shared with the event writers. Every
per-novel write transaction of `Exports` follows the lock order of spec section 4: `lock_novel`
first, then the rows, then the events last. `claim_ready` selects across novels and is exempt;
its per-export follow-up takes the novel lock.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Connection, and_, delete, exists, func, insert, or_, select, update
from sqlalchemy.orm import aliased

from novelhog import ids
from novelhog.domain.chapters import ChapterRange, ChapterState, resolve_selector
from novelhog.domain.errors import Conflict, NotFound, ValidationFailed
from novelhog.domain.exports import FORMATS, ExportState, Format, export_filename, format_for_mime
from novelhog.domain.payloads import ExportBlocked, ExportProgress, ExportView, deleted_export
from novelhog.domain.urls import static_url
from novelhog.repo import chapters as chapters_repo
from novelhog.repo import claims, db, events, trash
from novelhog.repo.context import RepoContext
from novelhog.repo.novels import lock_novel
from novelhog.repo.schema import chapters, exports, novels, storage_trash

_WAITING = ExportState.WAITING.value
_BUILDING = ExportState.BUILDING.value
_DOWNLOADED = ChapterState.DOWNLOADED.value


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
    conn: Connection, novel_id: str, changed_chapter_ids: Sequence[str]
) -> list[str]:
    """Return the ids of the novel's waiting exports that a chapter change affects.

    An export is affected when one of its end chapters is among the changed ones (its range may
    have become empty), or when its current range contains a changed chapter that still has a
    position (spec section 4, "Event log").
    """
    waiting = conn.execute(
        select(exports.c.id, exports.c.first_chapter_id, exports.c.last_chapter_id)
        .where(and_(exports.c.novel_id == novel_id, exports.c.state == ExportState.WAITING))
        .order_by(exports.c.created_at, exports.c.id)
    ).all()
    if not waiting or not changed_chapter_ids:
        return []
    changed_ids = set(changed_chapter_ids)
    wanted = {i for row in waiting for i in (row.first_chapter_id, row.last_chapter_id)}
    wanted.update(changed_ids)
    positions = {
        r.id: r.position
        for r in conn.execute(
            select(chapters.c.id, chapters.c.position).where(chapters.c.id.in_(wanted))
        )
    }
    changed = {positions[i] for i in changed_ids if positions.get(i) is not None}
    affected = []
    for row in waiting:
        if row.first_chapter_id in changed_ids or row.last_chapter_id in changed_ids:
            affected.append(row.id)
            continue
        first, last = positions.get(row.first_chapter_id), positions.get(row.last_chapter_id)
        if first is None or last is None:
            continue
        low, high = sorted((first, last))
        if any(low <= p <= high for p in changed):
            affected.append(row.id)
    return affected


def fail_cancelled(
    conn: Connection, novel_id: str, cancelled_chapter_ids: Sequence[str]
) -> list[str]:
    """Fail the novel's waiting exports that need a chapter the user just dequeued.

    An export needs a cancelled chapter when it is waiting, not `skip_missing` and the chapter
    lies in its range. It moves to `failed` with "Chapter <N> was cancelled" ("Chapters <N>,
    <M>, … were cancelled", positions ascending). Returns the failed export ids; writes rows
    only: the caller holds `lock_novel` and appends the `EX` events last (`emit_novel_change`
    takes them as `failed_export_ids`). Not for the TOC sync, which never fails exports.
    """
    candidates = affected_waiting(conn, novel_id, cancelled_chapter_ids)
    if not candidates:
        return []
    rows = conn.execute(
        select(exports.c.id, exports.c.first_chapter_id, exports.c.last_chapter_id)
        .where(exports.c.id.in_(candidates), exports.c.skip_missing.is_(False))
        .order_by(exports.c.created_at, exports.c.id)
    ).all()
    wanted = {i for row in rows for i in (row.first_chapter_id, row.last_chapter_id)}
    wanted.update(cancelled_chapter_ids)
    positions = {
        r.id: r.position
        for r in conn.execute(
            select(chapters.c.id, chapters.c.position).where(chapters.c.id.in_(wanted))
        )
    }
    cancelled = sorted(
        {positions[i] for i in cancelled_chapter_ids if positions.get(i) is not None}
    )
    failed: list[str] = []
    for row in rows:
        first, last = positions.get(row.first_chapter_id), positions.get(row.last_chapter_id)
        if first is None or last is None:
            continue
        low, high = sorted((first, last))
        needed = [p for p in cancelled if low <= p <= high]
        if not needed:
            continue
        numbers = ", ".join(str(p) for p in needed)
        message = (
            f"Chapter {numbers} was cancelled"
            if len(needed) == 1
            else f"Chapters {numbers} were cancelled"
        )
        conn.execute(
            update(exports)
            .where(exports.c.id == row.id, exports.c.state == _WAITING)
            .values(state=ExportState.FAILED.value, error=message)
        )
        failed.append(row.id)
    return failed


@dataclass(frozen=True)
class ExportClaim:
    """A waiting export claimed by a worker, with what the build needs.

    `chapter_ids` are the downloaded, positioned chapters of the range in order; it is `None`
    when an end chapter lost its position (the build fails with "Range changed upstream").
    """

    export_id: str
    novel_id: str
    fmt: Format
    filename: str
    novel_title: str | None
    language: str | None
    cover_key: str | None
    chapter_ids: list[str] | None


def _end_position(chapter_id: Any) -> Any:
    return select(chapters.c.position).where(chapters.c.id == chapter_id).scalar_subquery()


def _range_ready() -> Any:
    """The range may be built: skip_missing, an end without position, or range downloaded."""
    first, last = (
        _end_position(exports.c.first_chapter_id),
        _end_position(exports.c.last_chapter_id),
    )
    other = aliased(chapters)
    incomplete = exists().where(
        other.c.novel_id == exports.c.novel_id,
        other.c.position.is_not(None),
        other.c.state != _DOWNLOADED,
        or_(
            and_(other.c.position >= first, other.c.position <= last),
            and_(other.c.position >= last, other.c.position <= first),
        ),
    )
    return or_(exports.c.skip_missing, first.is_(None), last.is_(None), ~incomplete)


def _buildable() -> Any:
    """A waiting export whose range may be built."""
    return and_(exports.c.state == _WAITING, _range_ready())


class Exports:
    """Creates, lists, shows and deletes exports and runs the worker's build claims."""

    def __init__(self, context: RepoContext) -> None:
        self._engine = context.engine
        self._clock = context.clock
        self._static_base_url = context.static_base_url

    def _view(self, conn: Connection, export_id: str) -> ExportView:
        return snapshot(conn, export_id, static_base_url=self._static_base_url)

    def _append(self, conn: Connection, novel_id: str, export_id: str, now: datetime) -> None:
        events.append(
            conn, novel_id, export_id, lambda: self._view(conn, export_id).to_json(), now=now
        )

    def create(
        self, novel_id: str, start: int, end: int, fmt: Format, *, skip_missing: bool = False
    ) -> tuple[ExportView, bool]:
        """Create an export of the chapters at positions `start`..`end`; return it and whether
        it is new.

        Without `skip_missing`, chapters that are `not_requested` or `error` are queued (same
        rules and events as `chapters.patch`) and `waited` is set if any chapter was not yet
        downloaded. An identical export still `waiting` or `building` is returned instead.
        """
        now = self._clock()
        info = FORMATS[fmt]
        with db.write(self._engine) as conn:
            if not ids.is_valid(novel_id, "NV") or not lock_novel(conn, novel_id):
                raise NotFound("novel", novel_id)
            total = conn.execute(
                select(func.count()).select_from(chapters).where(chapters_repo.positioned(novel_id))
            ).scalar_one()
            try:
                resolve_selector(ChapterRange(start, end), total)
            except ValidationFailed as error:
                if error.code == "no_chapters":
                    raise ValidationFailed("invalid_selector", error.message) from None
                raise
            rows = conn.execute(
                select(chapters.c.id, chapters.c.position, chapters.c.state)
                .where(
                    chapters_repo.positioned(novel_id),
                    chapters.c.position >= start,
                    chapters.c.position <= end,
                )
                .order_by(chapters.c.position)
            ).all()
            if not rows or rows[0].position != start or rows[-1].position != end:
                raise ValidationFailed("invalid_selector", "The range names a missing chapter")
            first_id, last_id = rows[0].id, rows[-1].id
            # Only `create` inserts exports and always under `lock_novel`, so this check cannot
            # race another create (the `exports_active` index stays as a backstop).
            existing = conn.execute(
                select(exports.c.id).where(
                    exports.c.novel_id == novel_id,
                    exports.c.first_chapter_id == first_id,
                    exports.c.last_chapter_id == last_id,
                    exports.c.mime == info.mime,
                    exports.c.skip_missing == skip_missing,
                    exports.c.state.in_([_WAITING, _BUILDING]),
                )
            ).scalar_one_or_none()
            if existing is not None:
                return self._view(conn, existing), False
            missing = [r for r in rows if r.state != _DOWNLOADED]
            if skip_missing and len(missing) == len(rows):
                raise ValidationFailed("nothing_to_export", "No chapter of the range is downloaded")
            title = conn.execute(select(novels.c.title).where(novels.c.id == novel_id)).scalar_one()
            export_id = ids.new_id("EX")
            conn.execute(
                insert(exports).values(
                    id=export_id,
                    novel_id=novel_id,
                    first_chapter_id=first_id,
                    last_chapter_id=last_id,
                    skip_missing=skip_missing,
                    mime=info.mime,
                    filename=export_filename(title, start, end, info.extension),
                    waited=bool(missing) and not skip_missing,
                    created_at=now,
                )
            )
            queued = (
                []
                if skip_missing
                else [
                    r.position
                    for r in missing
                    if r.state in (ChapterState.NOT_REQUESTED.value, ChapterState.ERROR.value)
                ]
            )
            changed = chapters_repo.queue(conn, novel_id, queued, now=now).changed_ids
            if changed:
                # The new export is waiting and its range holds the changed chapters, so this
                # appends its EX event (and no other event for it).
                events.emit_novel_change(
                    conn, novel_id, changed, now=now, static_base_url=self._static_base_url
                )
            else:
                self._append(conn, novel_id, export_id, now)
            return self._view(conn, export_id), True

    def get(self, novel_id: str, export_id: str) -> ExportView:
        """Return one export of the novel; raise `NotFound` for an unknown or foreign one."""
        if not ids.is_valid(novel_id, "NV") or not ids.is_valid(export_id, "EX"):
            raise NotFound("export", export_id)
        with db.read(self._engine) as conn:
            owner = conn.execute(
                select(exports.c.novel_id).where(exports.c.id == export_id)
            ).scalar_one_or_none()
            if owner != novel_id:
                raise NotFound("export", export_id)
            return self._view(conn, export_id)

    def delete(self, novel_id: str, export_id: str) -> None:
        """Delete a waiting, ready or failed export; a ready one's file goes to the trash."""
        now = self._clock()
        with db.write(self._engine) as conn:
            if (
                not ids.is_valid(novel_id, "NV")
                or not ids.is_valid(export_id, "EX")
                or not lock_novel(conn, novel_id)
            ):
                raise NotFound("export", export_id)
            row = conn.execute(
                exports.delete()
                .where(
                    exports.c.id == export_id,
                    exports.c.novel_id == novel_id,
                    exports.c.state != _BUILDING,
                )
                .returning(exports.c.state, exports.c.storage_key)
            ).first()
            if row is None:
                building = conn.execute(
                    select(exports.c.id).where(
                        exports.c.id == export_id,
                        exports.c.novel_id == novel_id,
                        exports.c.state == _BUILDING,
                    )
                ).first()
                if building is not None:
                    raise Conflict("export_building", "The export is being built")
                raise NotFound("export", export_id)
            if row.state == ExportState.READY.value and row.storage_key is not None:
                trash.add(conn, [row.storage_key], now)
            events.append(conn, novel_id, export_id, deleted_export(export_id, novel_id), now=now)

    def claim_ready(self, worker_id: str, ttl: int) -> ExportClaim | None:
        """Claim the oldest buildable waiting export for `ttl` seconds and mark it `building`.

        Buildable: `skip_missing`, an end chapter without position, or every chapter between
        the ends downloaded. Returns `None` when nothing is buildable.

        The claim commits first (it selects across novels and takes no novel lock); the `EX`
        event and the chapter list follow in a second transaction under the novel lock, which
        re-checks the range and hands the export back to `waiting` if it is no longer complete.
        """
        while True:
            now = self._clock()
            with db.write(self._engine) as conn:
                rows = claims.claim(
                    conn,
                    exports,
                    eligible=_buildable(),
                    priority=[exports.c.created_at],
                    owner=worker_id,
                    now=now,
                    ttl=timedelta(seconds=ttl),
                    extra_set={"state": _BUILDING},
                    returning=[exports.c.id, exports.c.novel_id],
                )
            if not rows:
                return None
            claim = self._started(rows[0].id, rows[0].novel_id, worker_id, now)
            if claim is not None:
                return claim

    def _started(
        self, export_id: str, novel_id: str, worker_id: str, now: datetime
    ) -> ExportClaim | None:
        """Follow-up of a claim under the novel lock: build the claim and append its EX event."""
        with db.write(self._engine) as conn:
            if not lock_novel(conn, novel_id):
                return None
            row = conn.execute(
                select(exports).where(
                    exports.c.id == export_id,
                    exports.c.state == _BUILDING,
                    claims.owned(exports, worker_id, now),
                )
            ).first()
            if row is None:
                return None
            # Authoritative re-check: chapter writers hold the novel lock too, so the range
            # cannot change between this check and the chapter list below.
            if (
                conn.execute(
                    select(exports.c.id).where(exports.c.id == export_id, _range_ready())
                ).first()
                is None
            ):
                claims.release(
                    conn,
                    exports,
                    [export_id],
                    owner=worker_id,
                    now=now,
                    extra_set={"state": _WAITING},
                    where=exports.c.state == _BUILDING,
                )
                return None
            novel = conn.execute(select(novels).where(novels.c.id == novel_id)).one()
            ends = {
                r.id: r.position
                for r in conn.execute(
                    select(chapters.c.id, chapters.c.position).where(
                        chapters.c.id.in_([row.first_chapter_id, row.last_chapter_id])
                    )
                )
            }
            first, last = ends.get(row.first_chapter_id), ends.get(row.last_chapter_id)
            chapter_ids: list[str] | None = None
            if first is not None and last is not None:
                chapter_ids = list(
                    conn.execute(
                        select(chapters.c.id)
                        .where(
                            chapters.c.novel_id == novel_id,
                            chapters.c.position >= min(first, last),
                            chapters.c.position <= max(first, last),
                            chapters.c.state == _DOWNLOADED,
                        )
                        .order_by(chapters.c.position)
                    ).scalars()
                )
            self._append(conn, novel_id, export_id, now)
            return ExportClaim(
                export_id=export_id,
                novel_id=novel_id,
                fmt=format_for_mime(row.mime),
                filename=row.filename,
                novel_title=novel.title,
                language=novel.language,
                cover_key=novel.cover_key,
                chapter_ids=chapter_ids,
            )

    def renew(self, export_id: str, worker_id: str, ttl: int) -> bool:
        """Push the claim forward; False if the worker no longer holds it."""
        now = self._clock()
        with db.write(self._engine) as conn:
            return (
                claims.renew(
                    conn,
                    exports,
                    [export_id],
                    owner=worker_id,
                    now=now,
                    ttl=timedelta(seconds=ttl),
                )
                == 1
            )

    def _end_build(
        self,
        export_id: str,
        worker_id: str,
        values: dict[str, object],
        now: datetime,
        *,
        published_key: str | None = None,
    ) -> bool:
        """Fenced end of a build: apply `values`, clear the claim and append the EX event."""
        with db.write(self._engine) as conn:
            novel_id = conn.execute(
                select(exports.c.novel_id).where(exports.c.id == export_id)
            ).scalar_one_or_none()
            if novel_id is None or not lock_novel(conn, novel_id):
                return False
            released = claims.release(
                conn,
                exports,
                [export_id],
                owner=worker_id,
                now=now,
                extra_set=values,
                where=exports.c.state == _BUILDING,
            )
            if released != 1:
                return False
            if published_key is not None:
                # An earlier owner that lost its claim may have trashed this (deterministic) key.
                conn.execute(delete(storage_trash).where(storage_trash.c.key == published_key))
            self._append(conn, novel_id, export_id, now)
            return True

    def finish(self, export_id: str, worker_id: str, *, storage_key: str, size_bytes: int) -> bool:
        """Mark the build `ready`; False and no change when the claim was lost."""
        now = self._clock()
        return self._end_build(
            export_id,
            worker_id,
            {
                "state": ExportState.READY.value,
                "storage_key": storage_key,
                "size_bytes": size_bytes,
                "ready_at": now,
            },
            now,
            published_key=storage_key,
        )

    def fail(self, export_id: str, worker_id: str, message: str) -> bool:
        """Mark the build `failed` with `message`; False and no change when the claim was lost."""
        return self._end_build(
            export_id,
            worker_id,
            {"state": ExportState.FAILED.value, "error": message},
            self._clock(),
        )

    def list(self, novel_id: str) -> list[ExportView]:
        """Return the novel's exports, newest first; raise `NotFound` for an unknown novel."""
        if not ids.is_valid(novel_id, "NV"):
            raise NotFound("novel", novel_id)
        with db.read(self._engine) as conn:
            if conn.execute(select(novels.c.id).where(novels.c.id == novel_id)).first() is None:
                raise NotFound("novel", novel_id)
            export_ids = (
                conn.execute(
                    select(exports.c.id)
                    .where(exports.c.novel_id == novel_id)
                    .order_by(exports.c.created_at.desc(), exports.c.id.desc())
                )
                .scalars()
                .all()
            )
            return [self._view(conn, export_id) for export_id in export_ids]
