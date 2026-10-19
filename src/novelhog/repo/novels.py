"""Novels: create, read, update, sync requests and delete (spec sections 4 and 5.3)."""

import random
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import Connection, and_, delete, func, insert, or_, select, update
from sqlalchemy.exc import IntegrityError

from novelhog import ids
from novelhog.domain import payloads
from novelhog.domain.errors import Conflict, NotFound, ServiceUnavailable, ValidationFailed
from novelhog.domain.payloads import NovelView
from novelhog.domain.refresh import jittered, next_interval
from novelhog.domain.text import strip_xml_invalid
from novelhog.domain.urls import host_of, normalize_toc_url, static_url
from novelhog.repo import claims, db, events, hosts, trash
from novelhog.repo.context import RepoContext
from novelhog.repo.schema import (
    chapter_contents,
    chapters,
    exports,
)
from novelhog.repo.schema import novels as novels_table

if TYPE_CHECKING:
    from novelhog.repo.chapters import TocDiff

_counters = (
    select(
        chapters.c.novel_id.label("novel_id"),
        func.count().label("total"),
        func.count().filter(chapters.c.state == "downloaded").label("downloaded"),
        func.count().filter(chapters.c.state.in_(["pending", "downloading"])).label("pending"),
        func.count().filter(chapters.c.state == "error").label("error"),
        func.count()
        .filter(
            and_(
                chapters.c.released_at.is_not(None),
                or_(
                    novels_table.c.seen_at.is_(None),
                    chapters.c.released_at > novels_table.c.seen_at,
                ),
            )
        )
        .label("new"),
    )
    .join(novels_table, novels_table.c.id == chapters.c.novel_id)
    .where(chapters.c.position.is_not(None))
    .group_by(chapters.c.novel_id)
    .subquery("counters")
)


@dataclass(frozen=True)
class NovelClaim:
    """A novel claimed for a TOC sync, with what the sync needs to know about it."""

    novel_id: str
    host: str
    toc_url: str
    title: str | None
    cover_key: str | None
    followed: bool
    initial: bool
    refresh_interval: int


@dataclass(frozen=True)
class SyncSucceeded:
    """A fetched TOC. `title`, `language` and `cover_key` are None when nothing was found."""

    urls: list[str]
    title: str | None
    language: str | None
    cover_key: str | None
    min_s: int
    max_s: int
    rng: random.Random


@dataclass(frozen=True)
class SyncFailed:
    """A failed sync: `import_error` is set on the initial import only; the schedule is given."""

    import_error: str | None
    interval: int
    next_at: datetime


SyncOutcome = SyncSucceeded | SyncFailed


@dataclass(frozen=True)
class FinishReport:
    """What `finish_sync` changed (an empty diff after a failure)."""

    diff: TocDiff


def _views(conn: Connection, where: Any, *, now: datetime, static_base_url: str) -> list[NovelView]:
    """Build views (spec section 4, "Derived values") for the novels matching `where`."""
    statement = (
        select(
            novels_table,
            _counters.c.total,
            _counters.c.downloaded,
            _counters.c.pending,
            _counters.c.error,
            _counters.c.new,
        )
        .outerjoin(_counters, _counters.c.novel_id == novels_table.c.id)
        .where(where)
        .order_by(novels_table.c.created_at.desc(), novels_table.c.id.desc())
    )
    views = []
    for row in conn.execute(statement).all():
        cover = row.cover_key
        views.append(
            NovelView(
                id=row.id,
                title=row.title,
                host=row.host,
                toc_url=row.toc_url,
                import_error=row.import_error,
                importing=row.last_refreshed_at is None and row.import_error is None,
                cover_url=None if cover is None else static_url(static_base_url, cover),
                followed=row.followed,
                syncing=row.claimed_by is not None and row.claimed_until >= now,
                refresh_interval=row.refresh_interval,
                next_refresh_at=row.next_refresh_at,
                last_refreshed_at=row.last_refreshed_at,
                total_pages=row.total or 0,
                downloaded_pages=row.downloaded or 0,
                pending_pages=row.pending or 0,
                error_pages=row.error or 0,
                new_pages=row.new or 0,
                created_at=row.created_at,
                updated_at=row.updated_at,
            )
        )
    return views


def lock_novel(conn: Connection, novel_id: str) -> bool:
    """Lock the novel's row with a no-op update; return False if the novel no longer exists.

    First statement of every transaction that writes the novel or any of its child rows (spec
    section 4, "Lock order"); a False result means the novel was deleted and the write is
    abandoned (`NotFound`, or a lost claim).
    """
    statement = (
        update(novels_table).where(novels_table.c.id == novel_id).values(id=novels_table.c.id)
    )
    return conn.execute(statement).rowcount == 1


def snapshot(conn: Connection, novel_id: str, *, now: datetime, static_base_url: str) -> NovelView:
    """Return the novel as the API would show it now; raise `NotFound` if it does not exist."""
    views = _views(conn, novels_table.c.id == novel_id, now=now, static_base_url=static_base_url)
    if not views:
        raise NotFound("novel", novel_id)
    return views[0]


class Novels:
    """Reads and writes novels; every write appends its `NV` event in the same transaction."""

    def __init__(self, context: RepoContext) -> None:
        self._engine = context.engine
        self._clock = context.clock
        self._static_base_url = context.static_base_url
        self._refresh_min_interval = context.refresh_min_interval

    def _snapshot(self, conn: Connection, novel_id: str, now: datetime) -> NovelView:
        return snapshot(conn, novel_id, now=now, static_base_url=self._static_base_url)

    def _emit(self, conn: Connection, novel_id: str, now: datetime) -> NovelView:
        """Take the clock, then build the snapshot and store it as the novel's `NV` event."""
        views: list[NovelView] = []

        def payload() -> dict[str, object]:
            views.append(self._snapshot(conn, novel_id, now))
            return views[0].to_json()

        events.append(conn, novel_id, novel_id, payload, now=now)
        return views[0]

    @staticmethod
    def _require(conn: Connection, novel_id: str) -> None:
        """Lock the novel row (first in the transaction) or raise `NotFound`."""
        if not ids.is_valid(novel_id, "NV") or not lock_novel(conn, novel_id):
            raise NotFound("novel", novel_id)

    def create(self, toc_url: str, followed: bool) -> NovelView:
        """Create a novel from a TOC URL and return it; the worker imports it later."""
        normalized = normalize_toc_url(toc_url)
        host = host_of(normalized)
        now = self._clock()
        try:
            with db.write(self._engine) as conn:
                if not hosts.any_published(conn):
                    raise ServiceUnavailable(  # noqa: TRY003
                        "hosts_not_published", "The worker has not published its hosts yet"
                    )
                supported = hosts.supported_hosts(conn)
                if host not in supported:
                    raise ValidationFailed(  # noqa: TRY003
                        "unsupported_host", f"{host} is not supported", {"hosts": supported}
                    )
                existing = self._existing_id(conn, normalized)
                if existing is not None:
                    raise self._exists(existing)
                novel_id = ids.new_id("NV")
                conn.execute(
                    insert(novels_table).values(
                        id=novel_id,
                        host=host,
                        toc_url=normalized,
                        followed=followed,
                        refresh_interval=self._refresh_min_interval,
                        next_refresh_at=now,
                        created_at=now,
                        updated_at=now,
                    )
                )
                return self._emit(conn, novel_id, now)
        except IntegrityError:
            with db.read(self._engine) as conn:
                existing = self._existing_id(conn, normalized)
            if existing is None:
                raise
            raise self._exists(existing) from None

    @staticmethod
    def _existing_id(conn: Connection, toc_url: str) -> str | None:
        statement = select(novels_table.c.id).where(novels_table.c.toc_url == toc_url)
        return conn.execute(statement).scalar_one_or_none()

    @staticmethod
    def _exists(novel_id: str) -> Conflict:
        return Conflict("novel_exists", "The novel already exists", {"novel_id": novel_id})

    def get(self, novel_id: str) -> NovelView:
        """Return one novel; raise `NotFound` for an unknown or malformed id."""
        if not ids.is_valid(novel_id, "NV"):
            raise NotFound("novel", novel_id)
        with db.read(self._engine) as conn:
            return self._snapshot(conn, novel_id, self._clock())

    def update(
        self,
        novel_id: str,
        *,
        followed: bool | None = None,
        seen: bool | None = None,
        title: str | None = None,
    ) -> NovelView:
        """Change the given fields (`seen=True` stamps `seen_at`) and append one `NV` event."""
        now = self._clock()
        with db.write(self._engine) as conn:
            self._require(conn, novel_id)
            cleaned: str | None = None
            if title is not None:
                cleaned = strip_xml_invalid(title).strip()
                if not cleaned:
                    raise ValidationFailed("invalid_title", "The title must not be empty")  # noqa: TRY003
            values: dict[str, object] = {}
            if followed is not None:
                values["followed"] = followed
            if seen:
                values["seen_at"] = now
            if cleaned is not None:
                values["title"] = cleaned
            if not values:
                return self._snapshot(conn, novel_id, now)
            conn.execute(
                update(novels_table)
                .where(novels_table.c.id == novel_id)
                .values(**values, updated_at=now)
            )
            return self._emit(conn, novel_id, now)

    def request_sync(self, novel_id: str) -> NovelView:
        """Ask for an immediate sync: `next_refresh_at = now`, `sync_requested = true`."""
        now = self._clock()
        with db.write(self._engine) as conn:
            self._require(conn, novel_id)
            conn.execute(
                update(novels_table)
                .where(novels_table.c.id == novel_id)
                .values(next_refresh_at=now, sync_requested=True, updated_at=now)
            )
            return self._emit(conn, novel_id, now)

    def delete(self, novel_id: str) -> None:
        """Delete the novel with its chapters, contents and exports; trash its static files.

        Rows are deleted first (the keys come back from `DELETE ... RETURNING`) and the event is
        appended last, as the lock order in `events.append` requires.
        """
        now = self._clock()
        with db.write(self._engine) as conn:
            self._require(conn, novel_id)
            chapter_ids = select(chapters.c.id).where(chapters.c.novel_id == novel_id)
            conn.execute(
                delete(chapter_contents).where(chapter_contents.c.chapter_id.in_(chapter_ids))
            )
            export_rows = conn.execute(
                delete(exports)
                .where(exports.c.novel_id == novel_id)
                .returning(exports.c.storage_key, exports.c.state)
            ).all()
            conn.execute(delete(chapters).where(chapters.c.novel_id == novel_id))
            novel_row = conn.execute(
                delete(novels_table)
                .where(novels_table.c.id == novel_id)
                .returning(novels_table.c.cover_key)
            ).first()
            if novel_row is None:
                raise NotFound("novel", novel_id)
            ready_keys = [row.storage_key for row in export_rows if row.state == "ready"]
            keys = [k for k in dict.fromkeys([novel_row.cover_key, *ready_keys]) if k is not None]
            trash.add(conn, keys, now)
            events.append(conn, novel_id, novel_id, payloads.deleted_novel(novel_id), now=now)

    def claim_due_refresh(self, worker_id: str, ttl: int) -> NovelClaim | None:
        """Claim the available novel with the oldest `next_refresh_at <= now` for `ttl` seconds.

        Appends an `NV` event (`syncing` true). Returns None when nothing is due.
        """
        now = self._clock()
        with db.write(self._engine) as conn:
            rows = claims.claim(
                conn,
                novels_table,
                eligible=novels_table.c.next_refresh_at <= now,
                priority=[novels_table.c.next_refresh_at],
                owner=worker_id,
                now=now,
                ttl=timedelta(seconds=ttl),
                returning=[
                    novels_table.c.id,
                    novels_table.c.host,
                    novels_table.c.toc_url,
                    novels_table.c.title,
                    novels_table.c.cover_key,
                    novels_table.c.followed,
                    novels_table.c.last_refreshed_at,
                    novels_table.c.refresh_interval,
                ],
            )
            if not rows:
                return None
            row = rows[0]
            self._emit(conn, row.id, now)
        return NovelClaim(
            novel_id=row.id,
            host=row.host,
            toc_url=row.toc_url,
            title=row.title,
            cover_key=row.cover_key,
            followed=row.followed,
            initial=row.last_refreshed_at is None,
            refresh_interval=row.refresh_interval,
        )

    def renew_claim(self, novel_id: str, worker_id: str, ttl: int) -> bool:
        """Extend the sync claim by `ttl` seconds; False once the claim was lost."""
        now = self._clock()
        with db.write(self._engine) as conn:
            renewed = claims.renew(
                conn,
                novels_table,
                [novel_id],
                owner=worker_id,
                now=now,
                ttl=timedelta(seconds=ttl),
            )
        return renewed == 1

    def finish_sync(
        self, novel_id: str, worker_id: str, outcome: SyncOutcome
    ) -> FinishReport | None:
        """Apply a sync result and release the claim in one fenced transaction (spec 6.2).

        Lock order: the novel row, the claim fence, the chapter diff, then the events. Returns
        None, having changed nothing, when the novel is gone or the claim was lost.
        """
        from novelhog.repo import chapters as chapter_repo  # noqa: PLC0415  (import cycle)

        now = self._clock()
        with db.write(self._engine) as conn:
            if not lock_novel(conn, novel_id):
                return None
            fenced = claims.fenced_update(
                conn, novels_table, novel_id, owner=worker_id, now=now, values={"updated_at": now}
            )
            if not fenced:
                return None
            row = conn.execute(select(novels_table).where(novels_table.c.id == novel_id)).one()
            if isinstance(outcome, SyncSucceeded):
                initial = row.last_refreshed_at is None
                diff = chapter_repo.apply_toc(
                    conn, novel_id, outcome.urls, initial=initial, followed=row.followed, now=now
                )
                interval = next_interval(
                    row.refresh_interval,
                    found_new=bool(diff.new),
                    failed=False,
                    min_s=outcome.min_s,
                    max_s=outcome.max_s,
                )
                due = now + timedelta(seconds=jittered(interval, outcome.rng))
                values: dict[str, object] = {
                    "title": row.title if row.title is not None else outcome.title,
                    "language": outcome.language if outcome.language else row.language,
                    "cover_key": outcome.cover_key if outcome.cover_key else row.cover_key,
                    "import_error": None,
                    "last_refreshed_at": now,
                    "refresh_interval": interval,
                }
            else:
                diff = chapter_repo.TocDiff(new=[], moved=[], removed=[], returned=[])
                due = outcome.next_at
                values = {"refresh_interval": outcome.interval}
                if outcome.import_error is not None:
                    values["import_error"] = outcome.import_error
            values |= {
                "next_refresh_at": now if row.sync_requested else due,
                "sync_requested": False,
                "updated_at": now,
            }
            claims.release(
                conn, novels_table, [novel_id], owner=worker_id, now=now, extra_set=values
            )
            events.emit_novel_change(
                conn,
                novel_id,
                diff.changed_ids,
                now=now,
                static_base_url=self._static_base_url,
                released=diff.new,
            )
        return FinishReport(diff)

    def list(self) -> list[NovelView]:
        """Return all novels, newest first."""
        with db.read(self._engine) as conn:
            return _views(conn, True, now=self._clock(), static_base_url=self._static_base_url)
