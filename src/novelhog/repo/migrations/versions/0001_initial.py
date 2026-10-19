"""Initial schema (spec section 4); seeds event_clock with (1, 0).

Revision ID: 0001
Revises:
Create Date: 2026-10-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

from novelhog.repo.types import UTCDateTime

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JSON = sa.JSON().with_variant(JSONB, "postgresql")
_BIG = sa.BigInteger().with_variant(sa.Integer, "sqlite")


def _id_check(table: str, prefix: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(
        f"length(id) = 14 AND substr(id, 1, 2) = '{prefix}'", name=f"ck_{table}_id"
    )


def _claim_check(table: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(
        "(claimed_by IS NULL) = (claimed_until IS NULL)", name=f"ck_{table}_claim"
    )


def upgrade() -> None:
    op.create_table(
        "hosts",
        sa.Column("host", sa.Text, primary_key=True),
        sa.Column("supported", sa.Boolean, nullable=False),
        sa.Column("updated_at", UTCDateTime, nullable=False),
    )

    op.create_table(
        "novels",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("title", sa.Text),
        sa.Column("host", sa.Text, sa.ForeignKey("hosts.host"), nullable=False),
        sa.Column("toc_url", sa.Text, nullable=False, unique=True),
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
        _id_check("novels", "NV"),
        _claim_check("novels"),
    )
    op.create_index("novels_due_refresh", "novels", ["next_refresh_at"])

    op.create_table(
        "chapters",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("novel_id", sa.Text, sa.ForeignKey("novels.id"), nullable=False),
        sa.Column("url", sa.Text, nullable=False),
        sa.Column(
            "position",
            sa.Integer,
            sa.CheckConstraint('"position" >= 1', name="ck_chapters_position"),
        ),
        sa.Column("removed_at", UTCDateTime),
        sa.Column("title", sa.Text),
        sa.Column("state", sa.Text, nullable=False, server_default="not_requested"),
        sa.Column("priority", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("has_images", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("released_at", UTCDateTime),
        sa.Column("queued_at", UTCDateTime),
        sa.Column("claimed_by", sa.Text),
        sa.Column("claimed_until", UTCDateTime),
        sa.Column("updated_at", UTCDateTime, nullable=False),
        sa.UniqueConstraint("novel_id", "url", name="uq_chapters_novel_url"),
        _id_check("chapters", "CH"),
        sa.CheckConstraint(
            "state IN ('not_requested', 'pending', 'downloading', 'downloaded', 'error')",
            name="ck_chapters_state",
        ),
        sa.CheckConstraint("priority IN (0, 1)", name="ck_chapters_priority"),
        sa.CheckConstraint(
            '("position" IS NULL) = (removed_at IS NOT NULL)', name="ck_chapters_removed"
        ),
        sa.CheckConstraint(
            "(state IN ('pending', 'downloading')) = (queued_at IS NOT NULL)",
            name="ck_chapters_queued",
        ),
        _claim_check("chapters"),
        sa.CheckConstraint(
            "claimed_by IS NULL OR state IN ('pending', 'downloading')",
            name="ck_chapters_claim_state",
        ),
        sa.CheckConstraint(
            "state <> 'downloading' OR claimed_by IS NOT NULL", name="ck_chapters_started"
        ),
    )
    op.create_index(
        "chapters_queue",
        "chapters",
        [sa.text("priority DESC"), "queued_at", "position"],
        sqlite_where=sa.text("state = 'pending'"),
        postgresql_where=sa.text("state = 'pending'"),
    )
    op.create_index("chapters_order", "chapters", ["novel_id", "position"])
    op.create_index("chapters_state", "chapters", ["novel_id", "state"])
    op.create_index(
        "chapters_released",
        "chapters",
        ["novel_id", "released_at"],
        sqlite_where=sa.text("released_at IS NOT NULL"),
        postgresql_where=sa.text("released_at IS NOT NULL"),
    )

    op.create_table(
        "chapter_contents",
        sa.Column("chapter_id", sa.Text, sa.ForeignKey("chapters.id"), primary_key=True),
        sa.Column("markdown", sa.Text, nullable=False),
        sa.Column("fetched_at", UTCDateTime, nullable=False),
    )

    op.create_table(
        "exports",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("novel_id", sa.Text, sa.ForeignKey("novels.id"), nullable=False),
        sa.Column("first_chapter_id", sa.Text, sa.ForeignKey("chapters.id"), nullable=False),
        sa.Column("last_chapter_id", sa.Text, sa.ForeignKey("chapters.id"), nullable=False),
        sa.Column("skip_missing", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("mime", sa.Text, nullable=False),
        sa.Column("filename", sa.Text, nullable=False),
        sa.Column("state", sa.Text, nullable=False, server_default="waiting"),
        sa.Column("error", sa.Text),
        sa.Column("storage_key", sa.Text, unique=True),
        sa.Column(
            "size_bytes", _BIG, sa.CheckConstraint("size_bytes >= 0", name="ck_exports_size")
        ),
        sa.Column("waited", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("claimed_by", sa.Text),
        sa.Column("claimed_until", UTCDateTime),
        sa.Column("created_at", UTCDateTime, nullable=False),
        sa.Column("ready_at", UTCDateTime),
        _id_check("exports", "EX"),
        sa.CheckConstraint(
            "state IN ('waiting', 'building', 'ready', 'failed')", name="ck_exports_state"
        ),
        sa.CheckConstraint(
            "(state = 'ready') = (storage_key IS NOT NULL AND size_bytes IS NOT NULL)",
            name="ck_exports_ready",
        ),
        sa.CheckConstraint("(state = 'failed') = (error IS NOT NULL)", name="ck_exports_failed"),
        _claim_check("exports"),
        sa.CheckConstraint(
            "(state = 'building') = (claimed_by IS NOT NULL)", name="ck_exports_building"
        ),
    )
    op.create_index("exports_by_novel", "exports", ["novel_id", "created_at"])
    op.create_index(
        "exports_waiting",
        "exports",
        ["created_at"],
        sqlite_where=sa.text("state = 'waiting'"),
        postgresql_where=sa.text("state = 'waiting'"),
    )
    op.create_index(
        "exports_active",
        "exports",
        ["novel_id", "first_chapter_id", "last_chapter_id", "mime", "skip_missing"],
        unique=True,
        sqlite_where=sa.text("state IN ('waiting', 'building')"),
        postgresql_where=sa.text("state IN ('waiting', 'building')"),
    )

    op.create_table(
        "settings",
        sa.Column("key", sa.Text, primary_key=True),
        sa.Column("value", _JSON, nullable=False),
        sa.Column("updated_at", UTCDateTime, nullable=False),
    )

    op.create_table(
        "subscriptions",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("channel", sa.Text, nullable=False),
        sa.Column("address", _JSON, nullable=False),
        sa.Column("address_key", sa.Text, nullable=False),
        sa.Column("label", sa.Text),
        sa.Column("created_at", UTCDateTime, nullable=False),
        sa.UniqueConstraint("channel", "address_key", name="uq_subscriptions_channel_address_key"),
        _id_check("subscriptions", "SB"),
    )

    event_clock = op.create_table(
        "event_clock",
        sa.Column(
            "id",
            sa.Integer,
            sa.CheckConstraint("id = 1", name="ck_event_clock_id"),
            primary_key=True,
        ),
        sa.Column("seq", _BIG, nullable=False),
    )
    op.bulk_insert(event_clock, [{"id": 1, "seq": 0}])

    op.create_table(
        "events",
        sa.Column("seq", _BIG, primary_key=True, autoincrement=False),
        sa.Column("novel_id", sa.Text, nullable=False),
        sa.Column(
            "entity_id",
            sa.Text,
            sa.CheckConstraint(
                "substr(entity_id, 1, 2) IN ('NV', 'CH', 'EX')", name="ck_events_entity"
            ),
            nullable=False,
        ),
        sa.Column("payload", _JSON, nullable=False),
        sa.Column("at", UTCDateTime, nullable=False),
    )
    op.create_index("events_by_novel", "events", ["novel_id", "seq"])
    op.create_index("events_by_entity", "events", ["entity_id", "seq"])

    op.create_table(
        "consumers",
        sa.Column("name", sa.Text, primary_key=True),
        sa.Column("last_seq", _BIG, nullable=False),
        sa.Column("claimed_by", sa.Text),
        sa.Column("claimed_until", UTCDateTime),
        sa.Column("updated_at", UTCDateTime, nullable=False),
        _claim_check("consumers"),
    )

    op.create_table(
        "storage_trash",
        sa.Column("key", sa.Text, primary_key=True),
        sa.Column("at", UTCDateTime, nullable=False),
    )


def downgrade() -> None:
    for table in (
        "storage_trash",
        "consumers",
        "events",
        "event_clock",
        "subscriptions",
        "settings",
        "exports",
        "chapter_contents",
        "chapters",
        "novels",
        "hosts",
    ):
        op.drop_table(table)
