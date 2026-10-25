"""Tasks table and the `TK` entity prefix in events (tasks spec sections 3 and 9).

Novels: each novel gets one queued `novel_refresh` task at its `next_refresh_at` (v1 already set
it to the request time for a requested sync), then the novels' queue and claim columns go.
Downgrade restores them from the refresh tasks.

Chapters: each `pending` or `downloading` chapter with a position gets one queued
`chapter_download` task (`created_at = run_after = queued_at`, priority from v1's `priority`) and
comes back `pending`; one without a position becomes `not_requested`. Then the chapters' queue and
claim columns, checks and index go. Downgrade resets running downloads and restores the columns
from the download tasks.

Exports: `building` exports go back to `waiting`, each buildable waiting export gets one queued
`export_build` task at its `created_at`, then the exports' claim columns and checks go. Downgrade
sets `building` exports back to `waiting` and restores the columns and checks.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-24
"""

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

from novelhog.ids import new_id
from novelhog.repo.types import UTCDateTime

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JSON = sa.JSON().with_variant(JSONB, "postgresql")
_BIG = sa.BigInteger().with_variant(sa.Integer, "sqlite")

_EVENTS_CHECK = "ck_events_entity"
_OLD_PREFIXES = "substr(entity_id, 1, 2) IN ('NV', 'CH', 'EX')"
_NEW_PREFIXES = "substr(entity_id, 1, 2) IN ('NV', 'CH', 'EX', 'TK')"


_REFRESH = "novel_refresh"
_PRIORITY_REFRESH = 300
_BUILD = "export_build"
_PRIORITY_BUILD = 200
_EXPORTS_CLAIM = "ck_exports_claim"
_EXPORTS_BUILDING = "ck_exports_building"
_EXPORTS_BUILDING_CHECK = "(state = 'building') = (claimed_by IS NOT NULL)"
_CLAIM_CHECK = "(claimed_by IS NULL) = (claimed_until IS NULL)"
_NOVELS_DUE = "novels_due_refresh"
_NOVELS_CLAIM = "ck_novels_claim"
_DOWNLOAD = "chapter_download"
_PRIORITY_RELEASE = 110
_PRIORITY_USER = 100
_CHAPTERS_QUEUE = "chapters_queue"
_CHAPTER_CHECKS = {
    "ck_chapters_queued": "(state IN ('pending', 'downloading')) = (queued_at IS NOT NULL)",
    "ck_chapters_claim": _CLAIM_CHECK,
    "ck_chapters_claim_state": "claimed_by IS NULL OR state IN ('pending', 'downloading')",
    "ck_chapters_started": "state <> 'downloading' OR claimed_by IS NOT NULL",
}
_QUEUED_STATES = ("pending", "downloading")

_event_clock = sa.table("event_clock", sa.column("id", sa.Integer), sa.column("seq", _BIG))
_novels = sa.table(
    "novels",
    sa.column("id", sa.Text),
    sa.column("next_refresh_at", UTCDateTime),
)
_chapters = sa.table(
    "chapters",
    sa.column("id", sa.Text),
    sa.column("novel_id", sa.Text),
    sa.column("position", sa.Integer),
    sa.column("state", sa.Text),
)
_v1_chapter_queue = sa.table(
    "chapters",
    sa.column("id", sa.Text),
    sa.column("novel_id", sa.Text),
    sa.column("position", sa.Integer),
    sa.column("state", sa.Text),
    sa.column("priority", sa.Boolean),
    sa.column("queued_at", UTCDateTime),
    sa.column("claimed_by", sa.Text),
    sa.column("claimed_until", UTCDateTime),
    sa.column("updated_at", UTCDateTime),
)
_exports = sa.table(
    "exports",
    sa.column("id", sa.Text),
    sa.column("novel_id", sa.Text),
    sa.column("first_chapter_id", sa.Text),
    sa.column("last_chapter_id", sa.Text),
    sa.column("skip_missing", sa.Boolean),
    sa.column("state", sa.Text),
    sa.column("created_at", UTCDateTime),
)
_tasks = sa.table(
    "tasks",
    sa.column("id", sa.Text),
    sa.column("kind", sa.Text),
    sa.column("entity_id", sa.Text),
    sa.column("novel_id", sa.Text),
    sa.column("state", sa.Text),
    sa.column("priority", sa.Integer),
    sa.column("ordinal", sa.Integer),
    sa.column("run_after", UTCDateTime),
    sa.column("payload", _JSON),
    sa.column("emit_event", sa.Boolean),
    sa.column("attempts", sa.Integer),
    sa.column("change_seq", _BIG),
    sa.column("created_at", UTCDateTime),
    sa.column("updated_at", UTCDateTime),
)


class _Migration:
    """The migration time and the one `event_clock` value every migrated task shares."""

    def __init__(self) -> None:
        self.now = datetime.now(UTC)
        self._seq: int | None = None

    def change_seq(self) -> int:
        """Take one clock value for the whole migration, the first time a task needs it."""
        if self._seq is None:
            statement = (
                sa.update(_event_clock)
                .where(_event_clock.c.id == 1)
                .values(seq=_event_clock.c.seq + 1)
                .returning(_event_clock.c.seq)
            )
            self._seq = int(op.get_bind().execute(statement).scalar_one())
        return self._seq


def _fill_refresh_tasks(migration: _Migration) -> None:
    """One queued refresh task per novel at its `next_refresh_at`, claims dropped (spec 9)."""
    conn = op.get_bind()
    novels = conn.execute(
        sa.select(_novels.c.id, _novels.c.next_refresh_at).order_by(_novels.c.id)
    ).all()
    if not novels:
        return
    seq = migration.change_seq()
    conn.execute(
        sa.insert(_tasks),
        [
            {
                "id": new_id("TK"),
                "kind": _REFRESH,
                "entity_id": novel.id,
                "novel_id": novel.id,
                "state": "queued",
                "priority": _PRIORITY_REFRESH,
                "ordinal": 0,
                "run_after": novel.next_refresh_at,
                "payload": {},
                "emit_event": False,
                "attempts": 0,
                "change_seq": seq,
                "created_at": migration.now,
                "updated_at": migration.now,
            }
            for novel in novels
        ],
    )


def _v1_novel_columns() -> sa.Table:
    """`novels` as revision 0001 created it, without the constraints that outlive this revision.

    The SQLite batch rebuild copies this instead of reflecting the table; `_kept_novel_constraints`
    adds the rest in a fixed order, the order `metadata.create_all` uses. Reflection would copy
    them in set order.
    """
    return sa.Table(
        "novels",
        sa.MetaData(),
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("title", sa.Text),
        sa.Column("host", sa.Text, nullable=False),
        sa.Column("toc_url", sa.Text, nullable=False),
        sa.Column("language", sa.Text),
        sa.Column("import_error", sa.Text),
        sa.Column("cover_key", sa.Text),
        sa.Column("followed", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column(
            "refresh_interval",
            sa.Integer,
            sa.CheckConstraint("refresh_interval > 0", name="ck_novels_refresh_interval"),
            nullable=False,
        ),
        sa.Column("next_refresh_at", UTCDateTime, nullable=False),
        sa.Column("sync_requested", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("last_refreshed_at", UTCDateTime),
        sa.Column("seen_at", UTCDateTime),
        sa.Column("claimed_by", sa.Text),
        sa.Column("claimed_until", UTCDateTime),
        sa.Column("created_at", UTCDateTime, nullable=False),
        sa.Column("updated_at", UTCDateTime, nullable=False),
        sa.CheckConstraint("(claimed_by IS NULL) = (claimed_until IS NULL)", name=_NOVELS_CLAIM),
        sa.Index(_NOVELS_DUE, "next_refresh_at"),
    )


def _hosts_key() -> sa.Column[str]:
    return sa.Table("hosts", sa.MetaData(), sa.Column("host", sa.Text, primary_key=True)).c.host


def _kept_novel_constraints() -> tuple[sa.Constraint, ...]:
    return (
        sa.CheckConstraint("length(id) = 14 AND substr(id, 1, 2) = 'NV'", name="ck_novels_id"),
        # A column object, not "hosts.host": the batch's new table has no `hosts` to look up.
        sa.ForeignKeyConstraint(["host"], [_hosts_key()]),
        sa.UniqueConstraint("toc_url"),
    )


def _drop_novel_queue_columns() -> None:
    sqlite = op.get_bind().dialect.name == "sqlite"
    with op.batch_alter_table(
        "novels",
        copy_from=_v1_novel_columns() if sqlite else None,
        table_args=_kept_novel_constraints() if sqlite else (),
    ) as batch:
        batch.drop_index(_NOVELS_DUE)
        batch.drop_constraint(_NOVELS_CLAIM, type_="check")
        batch.drop_column("next_refresh_at")
        batch.drop_column("sync_requested")
        batch.drop_column("claimed_by")
        batch.drop_column("claimed_until")


def _restore_novel_queue_columns(now: datetime) -> None:
    """Restore v1's novel columns: `next_refresh_at` from the refresh tasks, no requests or claims.

    `next_refresh_at` is the queued refresh task's `run_after`, else the running one's, else now.
    """
    with op.batch_alter_table("novels") as batch:
        batch.add_column(sa.Column("next_refresh_at", UTCDateTime, nullable=True))
        batch.add_column(
            sa.Column("sync_requested", sa.Boolean, nullable=False, server_default=sa.false())
        )
        batch.add_column(sa.Column("claimed_by", sa.Text))
        batch.add_column(sa.Column("claimed_until", UTCDateTime))

    def run_after(state: str) -> sa.ScalarSelect[datetime]:
        return (
            sa.select(_tasks.c.run_after)
            .where(
                _tasks.c.kind == _REFRESH,
                _tasks.c.entity_id == _novels.c.id,
                _tasks.c.state == state,
            )
            .scalar_subquery()
        )

    op.get_bind().execute(
        sa.update(_novels).values(
            next_refresh_at=sa.func.coalesce(
                run_after("queued"), run_after("running"), sa.literal(now, UTCDateTime)
            )
        )
    )
    with op.batch_alter_table("novels") as batch:
        batch.alter_column("next_refresh_at", existing_type=UTCDateTime, nullable=False)
        batch.create_check_constraint(
            _NOVELS_CLAIM, "(claimed_by IS NULL) = (claimed_until IS NULL)"
        )
        batch.create_index(_NOVELS_DUE, ["next_refresh_at"])


def _fill_download_tasks(migration: _Migration) -> None:
    """One queued download task per positioned queued chapter; the rest leave the queue (spec 9).

    A positioned `pending` or `downloading` chapter gets a task with `created_at = run_after =
    queued_at`, priority 110 for an auto-queued release (v1 `priority`) else 100, and `ordinal` =
    its position, and is `pending` without a claim. One without a position becomes
    `not_requested` (v1 `_back_in_line`). Runs while v1's chapter checks still hold.
    """
    conn = op.get_bind()
    chapters = _v1_chapter_queue
    queued = chapters.c.state.in_(_QUEUED_STATES)
    positioned = chapters.c.position.is_not(None)
    rows = conn.execute(
        sa.select(
            chapters.c.id,
            chapters.c.novel_id,
            chapters.c.position,
            chapters.c.priority,
            chapters.c.queued_at,
        )
        .where(queued, positioned)
        .order_by(chapters.c.id)
    ).all()
    if rows:
        seq = migration.change_seq()
        conn.execute(
            sa.insert(_tasks),
            [
                {
                    "id": new_id("TK"),
                    "kind": _DOWNLOAD,
                    "entity_id": row.id,
                    "novel_id": row.novel_id,
                    "state": "queued",
                    "priority": _PRIORITY_RELEASE if row.priority else _PRIORITY_USER,
                    "ordinal": row.position,
                    "run_after": row.queued_at,
                    "payload": {},
                    "emit_event": False,
                    "attempts": 0,
                    "change_seq": seq,
                    "created_at": row.queued_at,
                    "updated_at": migration.now,
                }
                for row in rows
            ],
        )
    conn.execute(
        sa.update(chapters)
        .where(queued, positioned)
        .values(state="pending", claimed_by=None, claimed_until=None)
    )
    conn.execute(
        sa.update(chapters)
        .where(queued, chapters.c.position.is_(None))
        .values(
            state="not_requested",
            priority=False,
            queued_at=None,
            claimed_by=None,
            claimed_until=None,
        )
    )


def _v1_chapter_columns() -> sa.Table:
    """`chapters` as revision 0001 created it, without the constraints that outlive this revision.

    Like `_v1_novel_columns`: the SQLite batch rebuild copies this, and
    `_kept_chapter_constraints` adds the rest in the order `metadata.create_all` uses.
    """
    pending = sa.text("state = 'pending'")
    released = sa.text("released_at IS NOT NULL")
    return sa.Table(
        "chapters",
        sa.MetaData(),
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("novel_id", sa.Text, nullable=False),
        sa.Column("url", sa.Text, nullable=False),
        sa.Column(
            "position",
            sa.Integer,
            sa.CheckConstraint('"position" >= 1', name="ck_chapters_position"),
        ),
        sa.Column("removed_at", UTCDateTime),
        sa.Column("title", sa.Text),
        sa.Column("state", sa.Text, nullable=False, server_default="not_requested"),
        sa.Column("priority", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("has_images", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("released_at", UTCDateTime),
        sa.Column("queued_at", UTCDateTime),
        sa.Column("claimed_by", sa.Text),
        sa.Column("claimed_until", UTCDateTime),
        sa.Column("updated_at", UTCDateTime, nullable=False),
        *(sa.CheckConstraint(sql, name=name) for name, sql in _CHAPTER_CHECKS.items()),
        sa.Index(
            _CHAPTERS_QUEUE,
            sa.text("priority DESC"),
            "queued_at",
            "position",
            sqlite_where=pending,
        ),
        sa.Index("chapters_order", "novel_id", "position"),
        sa.Index("chapters_state", "novel_id", "state"),
        sa.Index("chapters_released", "novel_id", "released_at", sqlite_where=released),
    )


def _kept_chapter_constraints() -> tuple[sa.Constraint, ...]:
    return (
        sa.UniqueConstraint("novel_id", "url", name="uq_chapters_novel_url"),
        sa.CheckConstraint("length(id) = 14 AND substr(id, 1, 2) = 'CH'", name="ck_chapters_id"),
        sa.CheckConstraint(
            "state IN ('not_requested', 'pending', 'downloading', 'downloaded', 'error')",
            name="ck_chapters_state",
        ),
        sa.CheckConstraint(
            '("position" IS NULL) = (removed_at IS NOT NULL)', name="ck_chapters_removed"
        ),
        sa.ForeignKeyConstraint(["novel_id"], [_key_of("novels", "id")]),
    )


def _drop_chapter_queue_columns() -> None:
    sqlite = op.get_bind().dialect.name == "sqlite"
    with op.batch_alter_table(
        "chapters",
        copy_from=_v1_chapter_columns() if sqlite else None,
        table_args=_kept_chapter_constraints() if sqlite else (),
    ) as batch:
        batch.drop_index(_CHAPTERS_QUEUE)
        for name in _CHAPTER_CHECKS:
            batch.drop_constraint(name, type_="check")
        batch.drop_column("priority")
        batch.drop_column("queued_at")
        batch.drop_column("claimed_by")
        batch.drop_column("claimed_until")


def _restore_chapter_queue_columns() -> None:
    """Restore v1's chapter queue columns from the download tasks (tasks spec 9, downgrade).

    Running downloads are reset first: `downloading` → `pending`, or `not_requested` without a
    position. A `pending` chapter takes `queued_at` from its live download task's `created_at`
    and `priority` from its priority (110 → 1, 100 → 0); without a live task, `queued_at =
    updated_at` and `priority = 0`. No claims.
    """
    conn = op.get_bind()
    chapters = _v1_chapter_queue
    conn.execute(
        sa.update(_chapters)
        .where(_chapters.c.state == "downloading")
        .values(
            state=sa.case((_chapters.c.position.is_not(None), "pending"), else_="not_requested")
        )
    )
    with op.batch_alter_table("chapters") as batch:
        batch.add_column(
            sa.Column("priority", sa.Boolean, nullable=False, server_default=sa.false())
        )
        batch.add_column(sa.Column("queued_at", UTCDateTime))
        batch.add_column(sa.Column("claimed_by", sa.Text))
        batch.add_column(sa.Column("claimed_until", UTCDateTime))

    def live(state: str) -> sa.ColumnElement[bool]:
        return sa.and_(
            _tasks.c.kind == _DOWNLOAD,
            _tasks.c.entity_id == chapters.c.id,
            _tasks.c.state == state,
        )

    def created_at(state: str) -> sa.ScalarSelect[datetime]:
        return sa.select(_tasks.c.created_at).where(live(state)).scalar_subquery()

    release = sa.exists().where(
        _tasks.c.kind == _DOWNLOAD,
        _tasks.c.entity_id == chapters.c.id,
        _tasks.c.state.in_(("queued", "running")),
        _tasks.c.priority == _PRIORITY_RELEASE,
    )
    conn.execute(
        sa.update(chapters)
        .where(chapters.c.state == "pending")
        .values(
            queued_at=sa.func.coalesce(
                created_at("queued"), created_at("running"), chapters.c.updated_at
            ),
            priority=release,
        )
    )
    with op.batch_alter_table("chapters") as batch:
        for name, sql in _CHAPTER_CHECKS.items():
            batch.create_check_constraint(name, sql)
    pending = sa.text("state = 'pending'")
    op.create_index(
        _CHAPTERS_QUEUE,
        "chapters",
        [sa.text("priority DESC"), "queued_at", "position"],
        sqlite_where=pending,
        postgresql_where=pending,
    )


def _reset_building_exports(*, clear_claims: bool) -> None:
    """Exports `building` go back to `waiting`: their build restarts (tasks spec 9).

    With `clear_claims` (upgrade, while v1's claim columns and `ck_exports_building` still exist)
    the claims are cleared in the same statement.
    """
    table = _exports
    values: dict[str, object] = {"state": "waiting"}
    if clear_claims:
        table = sa.table(
            "exports",
            sa.column("state", sa.Text),
            sa.column("claimed_by", sa.Text),
            sa.column("claimed_until", UTCDateTime),
        )
        values |= {"claimed_by": None, "claimed_until": None}
    op.get_bind().execute(sa.update(table).where(table.c.state == "building").values(values))


def _buildable() -> sa.ColumnElement[bool]:
    """A waiting export whose range may be built (v1 spec 6.5), as this revision defines it.

    A frozen copy of `repo.exports.buildable` over this revision's tables: skip_missing, an end
    chapter without a position, or every positioned chapter between the ends downloaded.
    """

    def end(chapter_id: sa.ColumnElement[str]) -> sa.ScalarSelect[int]:
        position = sa.select(_chapters.c.position).where(_chapters.c.id == chapter_id)
        return position.correlate(_exports).scalar_subquery()

    first, last = end(_exports.c.first_chapter_id), end(_exports.c.last_chapter_id)
    other = _chapters.alias("other")
    incomplete = sa.exists().where(
        other.c.novel_id == _exports.c.novel_id,
        other.c.position.is_not(None),
        other.c.state != "downloaded",
        sa.or_(
            sa.and_(other.c.position >= first, other.c.position <= last),
            sa.and_(other.c.position >= last, other.c.position <= first),
        ),
    )
    return sa.and_(
        _exports.c.state == "waiting",
        sa.or_(_exports.c.skip_missing, first.is_(None), last.is_(None), ~incomplete),
    )


def _fill_build_tasks(migration: _Migration) -> None:
    """One queued build task per buildable waiting export at its `created_at` (tasks spec 9)."""
    conn = op.get_bind()
    buildable = conn.execute(
        sa.select(_exports.c.id, _exports.c.novel_id, _exports.c.created_at)
        .where(_buildable())
        .order_by(_exports.c.id)
    ).all()
    if not buildable:
        return
    seq = migration.change_seq()
    conn.execute(
        sa.insert(_tasks),
        [
            {
                "id": new_id("TK"),
                "kind": _BUILD,
                "entity_id": export.id,
                "novel_id": export.novel_id,
                "state": "queued",
                "priority": _PRIORITY_BUILD,
                "ordinal": 0,
                "run_after": export.created_at,
                "payload": {},
                "emit_event": False,
                "attempts": 0,
                "change_seq": seq,
                "created_at": export.created_at,
                "updated_at": migration.now,
            }
            for export in buildable
        ],
    )


def _key_of(table: str, column: str) -> sa.Column[str]:
    """A column object to reference from a batch table that has no metadata of its own."""
    return sa.Table(table, sa.MetaData(), sa.Column(column, sa.Text, primary_key=True)).c[column]


def _v1_export_columns() -> sa.Table:
    """`exports` as revision 0001 created it, without the constraints that outlive this revision.

    Like `_v1_novel_columns`: the SQLite batch rebuild copies this, and `_kept_export_constraints`
    adds the rest in the order `metadata.create_all` uses.
    """
    waiting = sa.text("state = 'waiting'")
    active = sa.text("state IN ('waiting', 'building')")
    return sa.Table(
        "exports",
        sa.MetaData(),
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("novel_id", sa.Text, nullable=False),
        sa.Column("first_chapter_id", sa.Text, nullable=False),
        sa.Column("last_chapter_id", sa.Text, nullable=False),
        sa.Column("skip_missing", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("mime", sa.Text, nullable=False),
        sa.Column("filename", sa.Text, nullable=False),
        sa.Column("state", sa.Text, nullable=False, server_default="waiting"),
        sa.Column("error", sa.Text),
        sa.Column("storage_key", sa.Text),
        sa.Column(
            "size_bytes", _BIG, sa.CheckConstraint("size_bytes >= 0", name="ck_exports_size")
        ),
        sa.Column("waited", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("claimed_by", sa.Text),
        sa.Column("claimed_until", UTCDateTime),
        sa.Column("created_at", UTCDateTime, nullable=False),
        sa.Column("ready_at", UTCDateTime),
        sa.CheckConstraint(_CLAIM_CHECK, name=_EXPORTS_CLAIM),
        sa.CheckConstraint(_EXPORTS_BUILDING_CHECK, name=_EXPORTS_BUILDING),
        sa.Index("exports_by_novel", "novel_id", "created_at"),
        sa.Index("exports_waiting", "created_at", sqlite_where=waiting),
        sa.Index(
            "exports_active",
            "novel_id",
            "first_chapter_id",
            "last_chapter_id",
            "mime",
            "skip_missing",
            unique=True,
            sqlite_where=active,
        ),
    )


def _kept_export_constraints() -> tuple[sa.Constraint, ...]:
    return (
        sa.CheckConstraint("length(id) = 14 AND substr(id, 1, 2) = 'EX'", name="ck_exports_id"),
        sa.CheckConstraint(
            "state IN ('waiting', 'building', 'ready', 'failed')", name="ck_exports_state"
        ),
        sa.CheckConstraint(
            "(state = 'ready') = (storage_key IS NOT NULL AND size_bytes IS NOT NULL)",
            name="ck_exports_ready",
        ),
        sa.CheckConstraint("(state = 'failed') = (error IS NOT NULL)", name="ck_exports_failed"),
        sa.ForeignKeyConstraint(["novel_id"], [_key_of("novels", "id")]),
        sa.ForeignKeyConstraint(["first_chapter_id"], [_key_of("chapters", "id")]),
        sa.ForeignKeyConstraint(["last_chapter_id"], [_key_of("chapters", "id")]),
        sa.UniqueConstraint("storage_key"),
    )


def _drop_export_claim_columns() -> None:
    sqlite = op.get_bind().dialect.name == "sqlite"
    with op.batch_alter_table(
        "exports",
        copy_from=_v1_export_columns() if sqlite else None,
        table_args=_kept_export_constraints() if sqlite else (),
    ) as batch:
        batch.drop_constraint(_EXPORTS_BUILDING, type_="check")
        batch.drop_constraint(_EXPORTS_CLAIM, type_="check")
        batch.drop_column("claimed_by")
        batch.drop_column("claimed_until")


def _restore_export_claim_columns() -> None:
    """Restore v1's export claim columns, all empty, and their checks (no export is building)."""
    _reset_building_exports(clear_claims=False)
    with op.batch_alter_table("exports") as batch:
        batch.add_column(sa.Column("claimed_by", sa.Text))
        batch.add_column(sa.Column("claimed_until", UTCDateTime))
    with op.batch_alter_table("exports") as batch:
        batch.create_check_constraint(_EXPORTS_CLAIM, _CLAIM_CHECK)
        batch.create_check_constraint(_EXPORTS_BUILDING, _EXPORTS_BUILDING_CHECK)


def _widen_events_check(condition: str) -> None:
    with op.batch_alter_table("events") as batch:
        batch.drop_constraint(_EVENTS_CHECK, type_="check")
        batch.create_check_constraint(_EVENTS_CHECK, condition)


def upgrade() -> None:
    op.create_table(
        "tasks",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("entity_id", sa.Text, nullable=False),
        sa.Column("novel_id", sa.Text, sa.ForeignKey("novels.id"), nullable=False),
        sa.Column("state", sa.Text, nullable=False, server_default="queued"),
        sa.Column("priority", sa.Integer, nullable=False),
        sa.Column("ordinal", sa.Integer, nullable=False, server_default="0"),
        sa.Column("run_after", UTCDateTime, nullable=False),
        sa.Column("payload", _JSON, nullable=False, server_default="{}"),
        sa.Column("emit_event", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("error", sa.Text),
        sa.Column("change_seq", _BIG, nullable=False),
        sa.Column("claimed_by", sa.Text),
        sa.Column("claimed_until", UTCDateTime),
        sa.Column("created_at", UTCDateTime, nullable=False),
        sa.Column("updated_at", UTCDateTime, nullable=False),
        sa.Column("started_at", UTCDateTime),
        sa.Column("finished_at", UTCDateTime),
        sa.Column("duration_ms", sa.Integer),
        sa.CheckConstraint("length(id) = 14 AND substr(id, 1, 2) = 'TK'", name="ck_tasks_id"),
        sa.CheckConstraint("(claimed_by IS NULL) = (claimed_until IS NULL)", name="ck_tasks_claim"),
        sa.CheckConstraint(
            "state IN ('queued', 'running', 'done', 'failed', 'skipped', 'cancelled')",
            name="ck_tasks_state",
        ),
        sa.CheckConstraint(
            "(kind = 'novel_refresh' AND substr(entity_id, 1, 2) = 'NV') "
            "OR (kind = 'chapter_download' AND substr(entity_id, 1, 2) = 'CH') "
            "OR (kind = 'export_build' AND substr(entity_id, 1, 2) = 'EX')",
            name="ck_tasks_kind",
        ),
        sa.CheckConstraint(
            "state <> 'running' OR (claimed_by IS NOT NULL AND started_at IS NOT NULL)",
            name="ck_tasks_started",
        ),
        sa.CheckConstraint(
            "claimed_by IS NULL OR state IN ('queued', 'running')", name="ck_tasks_claim_state"
        ),
        sa.CheckConstraint(
            "(state IN ('done', 'failed', 'skipped', 'cancelled')) = (finished_at IS NOT NULL)",
            name="ck_tasks_finished",
        ),
        sa.CheckConstraint("state <> 'failed' OR error IS NOT NULL", name="ck_tasks_failed"),
    )
    queued = sa.text("state = 'queued'")
    running = sa.text("state = 'running'")
    finished = sa.text("finished_at IS NOT NULL")
    op.create_index(
        "tasks_one_queued",
        "tasks",
        ["kind", "entity_id"],
        unique=True,
        sqlite_where=queued,
        postgresql_where=queued,
    )
    op.create_index(
        "tasks_one_running",
        "tasks",
        ["kind", "entity_id"],
        unique=True,
        sqlite_where=running,
        postgresql_where=running,
    )
    op.create_index(
        "tasks_queue",
        "tasks",
        [sa.text("priority DESC"), "run_after", "ordinal", "id"],
        sqlite_where=queued,
        postgresql_where=queued,
    )
    op.create_index("tasks_by_entity", "tasks", ["entity_id", "created_at"])
    op.create_index("tasks_by_novel", "tasks", ["novel_id"])
    op.create_index(
        "tasks_finished",
        "tasks",
        ["finished_at"],
        sqlite_where=finished,
        postgresql_where=finished,
    )
    op.create_index("tasks_changes", "tasks", ["change_seq"])

    _widen_events_check(_NEW_PREFIXES)

    migration = _Migration()
    _fill_refresh_tasks(migration)
    _reset_building_exports(clear_claims=True)
    _fill_download_tasks(migration)
    _fill_build_tasks(migration)
    _drop_novel_queue_columns()
    _drop_chapter_queue_columns()
    _drop_export_claim_columns()


def downgrade() -> None:
    _restore_export_claim_columns()
    _restore_chapter_queue_columns()
    _restore_novel_queue_columns(datetime.now(UTC))
    op.execute("DELETE FROM events WHERE substr(entity_id, 1, 2) = 'TK'")
    _widen_events_check(_OLD_PREFIXES)
    op.drop_table("tasks")
