"""Tasks table and the `TK` entity prefix in events (tasks spec sections 3 and 9).

Novels: each novel gets one queued `novel_refresh` task at its `next_refresh_at` (v1 already set
it to the request time for a requested sync), then the novels' queue and claim columns go.
Downgrade restores them from the refresh tasks.

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
_NOVELS_DUE = "novels_due_refresh"
_NOVELS_CLAIM = "ck_novels_claim"

_event_clock = sa.table("event_clock", sa.column("id", sa.Integer), sa.column("seq", _BIG))
_novels = sa.table(
    "novels",
    sa.column("id", sa.Text),
    sa.column("next_refresh_at", UTCDateTime),
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
    _drop_novel_queue_columns()


def downgrade() -> None:
    _restore_novel_queue_columns(datetime.now(UTC))
    op.execute("DELETE FROM events WHERE substr(entity_id, 1, 2) = 'TK'")
    _widen_events_check(_OLD_PREFIXES)
    op.drop_table("tasks")
