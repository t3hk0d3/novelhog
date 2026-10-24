"""The database schema as SQLAlchemy Core tables (spec section 4); this module is the contract."""

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Table,
    Text,
    UniqueConstraint,
    false,
    text,
    true,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.types import JSON

from novelhog.repo.types import UTCDateTime

metadata = MetaData()

_JSON = JSON().with_variant(JSONB, "postgresql")
_BIG = BigInteger().with_variant(Integer, "sqlite")


def _id_check(name: str, prefix: str) -> CheckConstraint:
    return CheckConstraint(f"length(id) = 14 AND substr(id, 1, 2) = '{prefix}'", name=name)


def _claim_check(name: str) -> CheckConstraint:
    return CheckConstraint("(claimed_by IS NULL) = (claimed_until IS NULL)", name=name)


hosts = Table(
    "hosts",
    metadata,
    Column("host", Text, primary_key=True),
    Column("supported", Boolean, nullable=False),
    Column("updated_at", UTCDateTime, nullable=False),
)

novels = Table(
    "novels",
    metadata,
    Column("id", Text, primary_key=True),
    Column("title", Text),
    Column("host", Text, ForeignKey("hosts.host"), nullable=False),
    Column("toc_url", Text, nullable=False, unique=True),
    Column("language", Text),
    Column("import_error", Text),
    Column("cover_key", Text),
    Column("followed", Boolean, nullable=False, server_default=true()),
    Column(
        "refresh_interval",
        Integer,
        CheckConstraint("refresh_interval > 0", name="ck_novels_refresh_interval"),
        nullable=False,
    ),
    Column("last_refreshed_at", UTCDateTime),
    Column("seen_at", UTCDateTime),
    Column("created_at", UTCDateTime, nullable=False),
    Column("updated_at", UTCDateTime, nullable=False),
    _id_check("ck_novels_id", "NV"),
)

chapters = Table(
    "chapters",
    metadata,
    Column("id", Text, primary_key=True),
    Column("novel_id", Text, ForeignKey("novels.id"), nullable=False),
    Column("url", Text, nullable=False),
    Column("position", Integer, CheckConstraint('"position" >= 1', name="ck_chapters_position")),
    Column("removed_at", UTCDateTime),
    Column("title", Text),
    Column("state", Text, nullable=False, server_default="not_requested"),
    Column("priority", Boolean, nullable=False, server_default=false()),
    Column("has_images", Boolean, nullable=False, server_default=false()),
    Column("released_at", UTCDateTime),
    Column("queued_at", UTCDateTime),
    Column("claimed_by", Text),
    Column("claimed_until", UTCDateTime),
    Column("updated_at", UTCDateTime, nullable=False),
    UniqueConstraint("novel_id", "url", name="uq_chapters_novel_url"),
    _id_check("ck_chapters_id", "CH"),
    CheckConstraint(
        "state IN ('not_requested', 'pending', 'downloading', 'downloaded', 'error')",
        name="ck_chapters_state",
    ),
    CheckConstraint('("position" IS NULL) = (removed_at IS NOT NULL)', name="ck_chapters_removed"),
    CheckConstraint(
        "(state IN ('pending', 'downloading')) = (queued_at IS NOT NULL)",
        name="ck_chapters_queued",
    ),
    _claim_check("ck_chapters_claim"),
    CheckConstraint(
        "claimed_by IS NULL OR state IN ('pending', 'downloading')",
        name="ck_chapters_claim_state",
    ),
    CheckConstraint("state <> 'downloading' OR claimed_by IS NOT NULL", name="ck_chapters_started"),
    Index("chapters_order", "novel_id", "position"),
    Index("chapters_state", "novel_id", "state"),
    Index(
        "chapters_released",
        "novel_id",
        "released_at",
        sqlite_where=text("released_at IS NOT NULL"),
        postgresql_where=text("released_at IS NOT NULL"),
    ),
)

Index(
    "chapters_queue",
    chapters.c.priority.desc(),
    chapters.c.queued_at,
    chapters.c.position,
    sqlite_where=text("state = 'pending'"),
    postgresql_where=text("state = 'pending'"),
)

chapter_contents = Table(
    "chapter_contents",
    metadata,
    Column("chapter_id", Text, ForeignKey("chapters.id"), primary_key=True),
    Column("markdown", Text, nullable=False),
    Column("fetched_at", UTCDateTime, nullable=False),
)

exports = Table(
    "exports",
    metadata,
    Column("id", Text, primary_key=True),
    Column("novel_id", Text, ForeignKey("novels.id"), nullable=False),
    Column("first_chapter_id", Text, ForeignKey("chapters.id"), nullable=False),
    Column("last_chapter_id", Text, ForeignKey("chapters.id"), nullable=False),
    Column("skip_missing", Boolean, nullable=False, server_default=false()),
    Column("mime", Text, nullable=False),
    Column("filename", Text, nullable=False),
    Column("state", Text, nullable=False, server_default="waiting"),
    Column("error", Text),
    Column("storage_key", Text, unique=True),
    Column(
        "size_bytes",
        _BIG,
        CheckConstraint("size_bytes >= 0", name="ck_exports_size"),
    ),
    Column("waited", Boolean, nullable=False, server_default=false()),
    Column("claimed_by", Text),
    Column("claimed_until", UTCDateTime),
    Column("created_at", UTCDateTime, nullable=False),
    Column("ready_at", UTCDateTime),
    _id_check("ck_exports_id", "EX"),
    CheckConstraint("state IN ('waiting', 'building', 'ready', 'failed')", name="ck_exports_state"),
    CheckConstraint(
        "(state = 'ready') = (storage_key IS NOT NULL AND size_bytes IS NOT NULL)",
        name="ck_exports_ready",
    ),
    CheckConstraint("(state = 'failed') = (error IS NOT NULL)", name="ck_exports_failed"),
    _claim_check("ck_exports_claim"),
    CheckConstraint("(state = 'building') = (claimed_by IS NOT NULL)", name="ck_exports_building"),
    Index("exports_by_novel", "novel_id", "created_at"),
    Index(
        "exports_waiting",
        "created_at",
        sqlite_where=text("state = 'waiting'"),
        postgresql_where=text("state = 'waiting'"),
    ),
    Index(
        "exports_active",
        "novel_id",
        "first_chapter_id",
        "last_chapter_id",
        "mime",
        "skip_missing",
        unique=True,
        sqlite_where=text("state IN ('waiting', 'building')"),
        postgresql_where=text("state IN ('waiting', 'building')"),
    ),
)

settings = Table(
    "settings",
    metadata,
    Column("key", Text, primary_key=True),
    Column("value", _JSON, nullable=False),
    Column("updated_at", UTCDateTime, nullable=False),
)

subscriptions = Table(
    "subscriptions",
    metadata,
    Column("id", Text, primary_key=True),
    Column("channel", Text, nullable=False),
    Column("address", _JSON, nullable=False),
    Column("address_key", Text, nullable=False),
    Column("label", Text),
    Column("created_at", UTCDateTime, nullable=False),
    UniqueConstraint("channel", "address_key", name="uq_subscriptions_channel_address_key"),
    _id_check("ck_subscriptions_id", "SB"),
)

tasks = Table(
    "tasks",
    metadata,
    Column("id", Text, primary_key=True),
    Column("kind", Text, nullable=False),
    Column("entity_id", Text, nullable=False),
    Column("novel_id", Text, ForeignKey("novels.id"), nullable=False),
    Column("state", Text, nullable=False, server_default="queued"),
    Column("priority", Integer, nullable=False),
    Column("ordinal", Integer, nullable=False, server_default="0"),
    Column("run_after", UTCDateTime, nullable=False),
    Column("payload", _JSON, nullable=False, server_default="{}"),
    Column("emit_event", Boolean, nullable=False, server_default=false()),
    Column("attempts", Integer, nullable=False, server_default="0"),
    Column("error", Text),
    Column("change_seq", _BIG, nullable=False),
    Column("claimed_by", Text),
    Column("claimed_until", UTCDateTime),
    Column("created_at", UTCDateTime, nullable=False),
    Column("updated_at", UTCDateTime, nullable=False),
    Column("started_at", UTCDateTime),
    Column("finished_at", UTCDateTime),
    Column("duration_ms", Integer),
    _id_check("ck_tasks_id", "TK"),
    _claim_check("ck_tasks_claim"),
    CheckConstraint(
        "state IN ('queued', 'running', 'done', 'failed', 'skipped', 'cancelled')",
        name="ck_tasks_state",
    ),
    CheckConstraint(
        "(kind = 'novel_refresh' AND substr(entity_id, 1, 2) = 'NV') "
        "OR (kind = 'chapter_download' AND substr(entity_id, 1, 2) = 'CH') "
        "OR (kind = 'export_build' AND substr(entity_id, 1, 2) = 'EX')",
        name="ck_tasks_kind",
    ),
    CheckConstraint(
        "state <> 'running' OR (claimed_by IS NOT NULL AND started_at IS NOT NULL)",
        name="ck_tasks_started",
    ),
    CheckConstraint(
        "claimed_by IS NULL OR state IN ('queued', 'running')", name="ck_tasks_claim_state"
    ),
    CheckConstraint(
        "(state IN ('done', 'failed', 'skipped', 'cancelled')) = (finished_at IS NOT NULL)",
        name="ck_tasks_finished",
    ),
    CheckConstraint("state <> 'failed' OR error IS NOT NULL", name="ck_tasks_failed"),
    Index(
        "tasks_one_queued",
        "kind",
        "entity_id",
        unique=True,
        sqlite_where=text("state = 'queued'"),
        postgresql_where=text("state = 'queued'"),
    ),
    Index(
        "tasks_one_running",
        "kind",
        "entity_id",
        unique=True,
        sqlite_where=text("state = 'running'"),
        postgresql_where=text("state = 'running'"),
    ),
    Index("tasks_by_entity", "entity_id", "created_at"),
    Index("tasks_by_novel", "novel_id"),
    Index(
        "tasks_finished",
        "finished_at",
        sqlite_where=text("finished_at IS NOT NULL"),
        postgresql_where=text("finished_at IS NOT NULL"),
    ),
    Index("tasks_changes", "change_seq"),
)

Index(
    "tasks_queue",
    tasks.c.priority.desc(),
    tasks.c.run_after,
    tasks.c.ordinal,
    tasks.c.id,
    sqlite_where=text("state = 'queued'"),
    postgresql_where=text("state = 'queued'"),
)

event_clock = Table(
    "event_clock",
    metadata,
    Column("id", Integer, CheckConstraint("id = 1", name="ck_event_clock_id"), primary_key=True),
    Column("seq", _BIG, nullable=False),
)

events = Table(
    "events",
    metadata,
    Column("seq", _BIG, primary_key=True, autoincrement=False),
    Column("novel_id", Text, nullable=False),
    Column("entity_id", Text, nullable=False),
    Column("payload", _JSON, nullable=False),
    Column("at", UTCDateTime, nullable=False),
    CheckConstraint("substr(entity_id, 1, 2) IN ('NV', 'CH', 'EX', 'TK')", name="ck_events_entity"),
    Index("events_by_novel", "novel_id", "seq"),
    Index("events_by_entity", "entity_id", "seq"),
)

consumers = Table(
    "consumers",
    metadata,
    Column("name", Text, primary_key=True),
    Column("last_seq", _BIG, nullable=False),
    Column("claimed_by", Text),
    Column("claimed_until", UTCDateTime),
    Column("updated_at", UTCDateTime, nullable=False),
    _claim_check("ck_consumers_claim"),
)

storage_trash = Table(
    "storage_trash",
    metadata,
    Column("key", Text, primary_key=True),
    Column("at", UTCDateTime, nullable=False),
)
