"""Novels: create, read, update, sync requests and delete (spec sections 4 and 5.3)."""

from datetime import datetime
from typing import Any

from sqlalchemy import Connection, and_, delete, func, insert, or_, select, update
from sqlalchemy.exc import IntegrityError

from novelhog import ids
from novelhog.domain import payloads
from novelhog.domain.errors import Conflict, NotFound, ServiceUnavailable, ValidationFailed
from novelhog.domain.payloads import NovelView
from novelhog.domain.text import strip_xml_invalid
from novelhog.domain.urls import host_of, normalize_toc_url, static_url
from novelhog.repo import db, events, hosts
from novelhog.repo.context import RepoContext
from novelhog.repo.schema import (
    chapter_contents,
    chapters,
    exports,
    storage_trash,
)
from novelhog.repo.schema import novels as novels_table
from novelhog.repo.sql import dialect_insert

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
        if not ids.is_valid(novel_id, "NV"):
            raise NotFound("novel", novel_id)
        found = conn.execute(select(novels_table.c.id).where(novels_table.c.id == novel_id))
        if found.first() is None:
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
        if not ids.is_valid(novel_id, "NV"):
            raise NotFound("novel", novel_id)
        with db.write(self._engine) as conn:
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
            if keys:
                conn.execute(
                    dialect_insert(self._engine, storage_trash).on_conflict_do_nothing(
                        index_elements=["key"]
                    ),
                    [{"key": k, "at": now} for k in keys],
                )
            events.append(conn, novel_id, novel_id, payloads.deleted_novel(novel_id), now=now)

    def list(self) -> list[NovelView]:
        """Return all novels, newest first."""
        with db.read(self._engine) as conn:
            return _views(conn, True, now=self._clock(), static_base_url=self._static_base_url)
