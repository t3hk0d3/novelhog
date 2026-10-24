"""Novels: create, read, update, sync requests and delete (spec sections 4 and 5.3).

A novel's refresh schedule lives on its `novel_refresh` tasks (tasks spec section 3): creating a
novel queues its first refresh, a sync request merges into the queued one, and deleting a novel
deletes its tasks. `next_refresh_at` and `syncing` are derived from the live refresh task.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    ColumnElement,
    Connection,
    FromClause,
    and_,
    delete,
    func,
    insert,
    or_,
    select,
    update,
)
from sqlalchemy.exc import IntegrityError

from novelhog import ids
from novelhog.domain import payloads
from novelhog.domain.errors import Conflict, NotFound, ServiceUnavailable, ValidationFailed
from novelhog.domain.payloads import NovelView
from novelhog.domain.tasks import PRIORITY_REFRESH, TaskKind, TaskState
from novelhog.domain.text import strip_xml_invalid
from novelhog.domain.urls import host_of, normalize_toc_url, static_url
from novelhog.repo import db, events, hosts, trash
from novelhog.repo.context import RepoContext
from novelhog.repo.schema import chapter_contents, chapters, exports, tasks
from novelhog.repo.schema import novels as novels_table
from novelhog.repo.tasks import TaskQueue

_KIND = TaskKind.NOVEL_REFRESH

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


_queued_refresh = tasks.alias("queued_refresh")
_running_refresh = tasks.alias("running_refresh")


def _refresh_task(alias: FromClause, state: TaskState) -> ColumnElement[bool]:
    """Join condition: the novel's refresh task in `state` (at most one, tasks spec 3)."""
    return and_(
        alias.c.kind == _KIND.value,
        alias.c.entity_id == novels_table.c.id,
        alias.c.state == state.value,
    )


def _views(conn: Connection, where: Any, *, now: datetime, static_base_url: str) -> list[NovelView]:
    """Build views (spec section 4, "Derived values") for the novels matching `where`.

    `next_refresh_at` and `syncing` come from the novel's live refresh task (tasks spec 3): the
    queued task's `run_after`, else the running one's; `syncing` while a running task's claim
    holds. A novel without a live refresh task (a repair case, tasks spec 8) is shown due now.
    """
    statement = (
        select(
            novels_table,
            _counters.c.total,
            _counters.c.downloaded,
            _counters.c.pending,
            _counters.c.error,
            _counters.c.new,
            func.coalesce(_queued_refresh.c.run_after, _running_refresh.c.run_after).label(
                "next_refresh_at"
            ),
            _running_refresh.c.claimed_until.label("sync_claimed_until"),
        )
        .outerjoin(_counters, _counters.c.novel_id == novels_table.c.id)
        .outerjoin(_queued_refresh, _refresh_task(_queued_refresh, TaskState.QUEUED))
        .outerjoin(_running_refresh, _refresh_task(_running_refresh, TaskState.RUNNING))
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
                syncing=row.sync_claimed_until is not None and row.sync_claimed_until >= now,
                refresh_interval=row.refresh_interval,
                next_refresh_at=row.next_refresh_at or now,
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
        self._queue = TaskQueue(context)

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
                        created_at=now,
                        updated_at=now,
                    )
                )
                self._enqueue_refresh(conn, novel_id, now)
                view = self._emit(conn, novel_id, now)
                self._queue.stamp(conn)
                return view
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

    def _enqueue_refresh(self, conn: Connection, novel_id: str, now: datetime) -> None:
        """Queue a refresh due now with `emit_event`, or merge into the queued one (tasks 5.2)."""
        self._queue.enqueue(
            conn,
            _KIND,
            novel_id,
            novel_id,
            priority=PRIORITY_REFRESH,
            run_after=now,
            emit_event=True,
        )

    def request_sync(self, novel_id: str) -> NovelView:
        """Ask for an immediate sync: a refresh task due now (tasks spec 5.2, section 1.1).

        Merges into the queued refresh; during a sync that queues the successor, so exactly one
        more sync runs after the current one.
        """
        now = self._clock()
        with db.write(self._engine) as conn:
            self._require(conn, novel_id)
            self._enqueue_refresh(conn, novel_id, now)
            conn.execute(
                update(novels_table).where(novels_table.c.id == novel_id).values(updated_at=now)
            )
            view = self._emit(conn, novel_id, now)
            self._queue.stamp(conn)
            return view

    def delete(self, novel_id: str) -> None:
        """Delete the novel with its chapters, contents, exports and tasks; trash its static files.

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
            self._queue.delete_for_novel(conn, novel_id)
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

    def list(self) -> list[NovelView]:
        """Return all novels, newest first."""
        with db.read(self._engine) as conn:
            return _views(conn, True, now=self._clock(), static_base_url=self._static_base_url)
