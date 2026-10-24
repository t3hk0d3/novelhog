"""Tasks table and the `TK` entity prefix in events (tasks spec sections 3 and 9).

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

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


def downgrade() -> None:
    op.execute("DELETE FROM events WHERE substr(entity_id, 1, 2) = 'TK'")
    _widen_events_check(_OLD_PREFIXES)
    op.drop_table("tasks")
