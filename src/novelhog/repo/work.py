"""Download work: chapter claims, the download pipeline's writes and recovery (spec 4, 6.3, 6.4).

Every per-novel write transaction follows the lock order of spec section 4: `lock_novel`
first (a missing novel means a lost claim), then the child rows, then the events via
`events.emit_novel_change`. Cross-novel statements (the batch claim, heartbeat renewals and
the recovery scans) are exempt.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import Connection, and_, case, select, update

from novelhog.domain.chapters import ChapterState
from novelhog.domain.exports import ExportState
from novelhog.repo import claims, contents, db, events
from novelhog.repo import exports as export_repo
from novelhog.repo.context import RepoContext
from novelhog.repo.novels import lock_novel, snapshot
from novelhog.repo.schema import chapters, consumers, novels
from novelhog.repo.schema import exports as exports_table

_PENDING = ChapterState.PENDING.value
_DOWNLOADING = ChapterState.DOWNLOADING.value


@dataclass(frozen=True)
class ChapterClaim:
    """A pending chapter claimed by a worker, with what it needs to download it."""

    chapter_id: str
    novel_id: str
    host: str
    toc_url: str
    position: int
    url: str


@dataclass(frozen=True)
class RecoveryReport:
    """What `recover` reset.

    `chapters_freed` counts expired `pending` claims (no events) and also removed chapters that
    were downloading, which return to `not_requested` (with events).
    """

    chapters_requeued: int
    chapters_freed: int
    novels_freed: int
    exports_reset: int = 0
    consumers_freed: int = 0


def _back_in_line(now: datetime) -> dict[str, object]:
    """Values that undo a started download: back to `pending` and its place in line.

    A removed chapter (no position) was dequeued by a sync while it downloaded, so it goes back
    to `not_requested` instead, the way `apply_toc` treats removed chapters.
    """
    positioned = chapters.c.position.is_not(None)
    return {
        "state": case((positioned, _PENDING), else_=ChapterState.NOT_REQUESTED.value),
        "queued_at": case((positioned, chapters.c.queued_at), else_=None),
        "claimed_by": None,
        "claimed_until": None,
        "updated_at": now,
    }


class Work:
    """The worker's download-side writes; every state change appends its events."""

    def __init__(self, context: RepoContext) -> None:
        self._engine = context.engine
        self._clock = context.clock
        self._static_base_url = context.static_base_url

    def _emit(
        self,
        conn: Connection,
        novel_id: str,
        changed: Sequence[str],
        now: datetime,
        errors: Mapping[str, tuple[str, datetime]] | None = None,
    ) -> None:
        events.emit_novel_change(
            conn, novel_id, changed, now=now, static_base_url=self._static_base_url, errors=errors
        )

    def claim_batch(self, worker_id: str, size: int, ttl: int) -> list[ChapterClaim]:
        """Claim up to `size` pending chapters of any novel for `ttl` seconds; no events.

        Ordered by `priority` (auto-queued releases first), `queued_at`, `position`. The
        chapters stay `pending`; removed chapters are never claimed.
        """
        if size <= 0:
            return []
        now = self._clock()
        with db.write(self._engine) as conn:
            rows = claims.claim(
                conn,
                chapters,
                eligible=and_(chapters.c.state == _PENDING, chapters.c.position.is_not(None)),
                priority=[chapters.c.priority.desc(), chapters.c.queued_at, chapters.c.position],
                owner=worker_id,
                now=now,
                ttl=timedelta(seconds=ttl),
                n=size,
                returning=[chapters.c.id, chapters.c.novel_id, chapters.c.position, chapters.c.url],
            )
            novel_ids = list({row.novel_id for row in rows})
            hosts = {
                row.id: (row.host, row.toc_url)
                for row in conn.execute(
                    select(novels.c.id, novels.c.host, novels.c.toc_url).where(
                        novels.c.id.in_(novel_ids)
                    )
                ).all()
            }
        return [
            ChapterClaim(
                chapter_id=row.id,
                novel_id=row.novel_id,
                host=hosts[row.novel_id][0],
                toc_url=hosts[row.novel_id][1],
                position=row.position,
                url=row.url,
            )
            for row in rows
            if row.novel_id in hosts
        ]

    def start(self, claim: ChapterClaim, worker_id: str) -> bool:
        """Turn a chapter claimed by `worker_id` from `pending` to `downloading`.

        Returns False and changes nothing if the chapter was dequeued or removed, or the claim
        was lost.
        """
        now = self._clock()
        with db.write(self._engine) as conn:
            if not lock_novel(conn, claim.novel_id):
                return False
            started = claims.fenced_update(
                conn,
                chapters,
                claim.chapter_id,
                owner=worker_id,
                now=now,
                values={"state": _DOWNLOADING, "updated_at": now},
                where=and_(chapters.c.state == _PENDING, chapters.c.position.is_not(None)),
            )
            if started:
                self._emit(conn, claim.novel_id, [claim.chapter_id], now)
            return started

    def _finish(
        self,
        chapter_id: str,
        worker_id: str,
        values: dict[str, object],
        *,
        markdown: str | None = None,
        error: str | None = None,
    ) -> bool:
        """Fenced write that ends a started download (`complete` or `fail`)."""
        now = self._clock()
        with db.write(self._engine) as conn:
            novel_id = conn.execute(
                select(chapters.c.novel_id).where(chapters.c.id == chapter_id)
            ).scalar_one_or_none()
            if novel_id is None or not lock_novel(conn, novel_id):
                return False
            cleared: dict[str, object] = {
                "queued_at": None,
                "priority": False,
                "claimed_by": None,
                "claimed_until": None,
                "updated_at": now,
            }
            applied = claims.fenced_update(
                conn,
                chapters,
                chapter_id,
                owner=worker_id,
                now=now,
                values=values | cleared,
                where=chapters.c.state == _DOWNLOADING,
            )
            if not applied:
                return False
            if markdown is not None:
                contents.put(conn, chapter_id, markdown, now)
            errors = None if error is None else {chapter_id: (error, now)}
            self._emit(conn, novel_id, [chapter_id], now, errors)
            return True

    def complete(
        self, chapter_id: str, worker_id: str, *, markdown: str, title: str, has_images: bool
    ) -> bool:
        """Store the downloaded chapter and mark it `downloaded`; False if the claim was lost."""
        values: dict[str, object] = {
            "state": ChapterState.DOWNLOADED.value,
            "title": title,
            "has_images": has_images,
        }
        return self._finish(chapter_id, worker_id, values, markdown=markdown)

    def fail(self, chapter_id: str, worker_id: str, message: str) -> bool:
        """Mark the chapter `error`; its `CH` event carries `error` and `error_at`."""
        values: dict[str, object] = {"state": ChapterState.ERROR.value}
        return self._finish(chapter_id, worker_id, values, error=message)

    def requeue(self, chapter_id: str, worker_id: str) -> bool:
        """Move this worker's `downloading` chapter back to `pending`, keeping its `queued_at`."""
        now = self._clock()
        with db.write(self._engine) as conn:
            novel_id = conn.execute(
                select(chapters.c.novel_id).where(chapters.c.id == chapter_id)
            ).scalar_one_or_none()
            if novel_id is None or not lock_novel(conn, novel_id):
                return False
            applied = claims.fenced_update(
                conn,
                chapters,
                chapter_id,
                owner=worker_id,
                now=now,
                values=_back_in_line(now),
                where=chapters.c.state == _DOWNLOADING,
            )
            if applied:
                self._emit(conn, novel_id, [chapter_id], now)
            return applied

    def release_chapters(self, chapter_ids: list[str], worker_id: str) -> int:
        """Free this worker's unstarted claims; the chapters stay pending. Returns the count."""
        now = self._clock()
        with db.write(self._engine) as conn:
            return claims.release(
                conn,
                chapters,
                chapter_ids,
                owner=worker_id,
                now=now,
                where=chapters.c.state == _PENDING,
            )

    def renew_chapters(self, chapter_ids: list[str], worker_id: str, ttl: int) -> list[str]:
        """Push `claimed_until` forward on this worker's chapters; return the ids still owned."""
        if not chapter_ids:
            return []
        now = self._clock()
        with db.write(self._engine) as conn:
            claims.renew(
                conn, chapters, chapter_ids, owner=worker_id, now=now, ttl=timedelta(seconds=ttl)
            )
            still_owned = conn.execute(
                select(chapters.c.id).where(
                    chapters.c.id.in_(chapter_ids), claims.owned(chapters, worker_id, now)
                )
            ).scalars()
            owned_ids = set(still_owned)
        return [chapter_id for chapter_id in chapter_ids if chapter_id in owned_ids]

    def recover(self) -> RecoveryReport:
        """Reset every lapsed claim of chapters, novels and exports (spec section 4, "Recovery").

        Also frees consumers whose claim lapsed (no events).
        """
        now = self._clock()
        requeued, freed = self._recover_chapters(now)
        novels_freed = self._recover_novels(now)
        exports_reset = self._recover_exports(now)
        consumers_freed = self._recover_consumers(now)
        return RecoveryReport(
            chapters_requeued=requeued,
            chapters_freed=freed,
            novels_freed=novels_freed,
            exports_reset=exports_reset,
            consumers_freed=consumers_freed,
        )

    def _recover_consumers(self, now: datetime) -> int:
        """Free consumers whose claim lapsed; their cursor stays where it was."""
        with db.write(self._engine) as conn:
            return conn.execute(
                update(consumers)
                .where(consumers.c.claimed_by.is_not(None), consumers.c.claimed_until < now)
                .values(claimed_by=None, claimed_until=None)
            ).rowcount

    def _recover_exports(self, now: datetime) -> int:
        """Return expired `building` exports to `waiting` and append an `EX` event for each."""
        with db.read(self._engine) as conn:
            lapsed = [
                (row.id, row.novel_id)
                for row in claims.expired(conn, exports_table, now)
                if row.state == ExportState.BUILDING.value
            ]
        reset = 0
        for export_id, novel_id in lapsed:
            with db.write(self._engine) as conn:
                if not lock_novel(conn, novel_id):
                    continue
                released = conn.execute(
                    update(exports_table)
                    .where(
                        exports_table.c.id == export_id,
                        exports_table.c.state == ExportState.BUILDING.value,
                        exports_table.c.claimed_until < now,
                    )
                    .values(state=ExportState.WAITING.value, claimed_by=None, claimed_until=None)
                ).rowcount
                if released:
                    events.append(
                        conn, novel_id, export_id, self._export_payload(conn, export_id), now=now
                    )
                    reset += 1
        return reset

    def _recover_chapters(self, now: datetime) -> tuple[int, int]:
        """Requeue expired `downloading` chapters (with events); free expired `pending` ones."""
        lapsed = and_(chapters.c.claimed_by.is_not(None), chapters.c.claimed_until < now)
        with db.write(self._engine) as conn:
            freed = conn.execute(
                update(chapters)
                .where(chapters.c.state == _PENDING, lapsed)
                .values(claimed_by=None, claimed_until=None)
            ).rowcount
            by_novel: dict[str, list[str]] = {}
            for row in claims.expired(conn, chapters, now):
                if row.state == _DOWNLOADING:
                    by_novel.setdefault(row.novel_id, []).append(row.id)
        requeued = 0
        for novel_id, chapter_ids in by_novel.items():
            with db.write(self._engine) as conn:
                if not lock_novel(conn, novel_id):
                    continue
                rows = conn.execute(
                    update(chapters)
                    .where(chapters.c.id.in_(chapter_ids), chapters.c.state == _DOWNLOADING, lapsed)
                    .values(_back_in_line(now))
                    .returning(chapters.c.id, chapters.c.state)
                ).all()
                if rows:
                    self._emit(conn, novel_id, [row.id for row in rows], now)
            requeued += sum(1 for row in rows if row.state == _PENDING)
            freed += sum(1 for row in rows if row.state != _PENDING)
        return requeued, freed

    def _recover_novels(self, now: datetime) -> int:
        """Free novels whose sync claim lapsed and append an `NV` event for each."""
        with db.read(self._engine) as conn:
            lapsed_ids = [row.id for row in claims.expired(conn, novels, now)]
        freed = 0
        for novel_id in lapsed_ids:
            with db.write(self._engine) as conn:
                if not lock_novel(conn, novel_id):
                    continue
                released = conn.execute(
                    update(novels)
                    .where(
                        novels.c.id == novel_id,
                        novels.c.claimed_by.is_not(None),
                        novels.c.claimed_until < now,
                    )
                    .values(claimed_by=None, claimed_until=None)
                ).rowcount
                if released:
                    events.append(
                        conn, novel_id, novel_id, self._novel_payload(conn, novel_id, now), now=now
                    )
                    freed += 1
        return freed

    def _export_payload(self, conn: Connection, export_id: str) -> events.Payload:
        """A lazy `EX` payload: the export's snapshot, built after the event clock is taken."""
        return lambda: export_repo.snapshot(
            conn, export_id, static_base_url=self._static_base_url
        ).to_json()

    def _novel_payload(self, conn: Connection, novel_id: str, now: datetime) -> events.Payload:
        """A lazy `NV` payload: the novel's snapshot, built after the event clock is taken."""
        return lambda: snapshot(
            conn, novel_id, now=now, static_base_url=self._static_base_url
        ).to_json()
