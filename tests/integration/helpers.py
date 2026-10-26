"""Row builders for integration tests that write straight to the schema."""

from datetime import datetime
from typing import Any

from sqlalchemy import Connection, Engine, insert, select

from novelhog.repo.schema import chapters, events, exports, hosts, novels, storage_trash

NOW = datetime.fromisoformat("2026-10-19T12:00:00+00:00")
NOVEL_ID = "NV000000000001"


def insert_host(connection: Connection, host: str = "example.com") -> None:
    """Insert a supported host."""
    connection.execute(insert(hosts).values(host=host, supported=True, updated_at=NOW))


def novel_row(**overrides: Any) -> dict[str, Any]:
    """Return a valid novel row with `overrides` applied."""
    row: dict[str, Any] = {
        "id": NOVEL_ID,
        "host": "example.com",
        "toc_url": "https://example.com/novel",
        "refresh_interval": 3600,
        "next_refresh_at": NOW,
        "created_at": NOW,
        "updated_at": NOW,
    }
    return row | overrides


def chapter_row(**overrides: Any) -> dict[str, Any]:
    """Return a valid `not_requested` chapter row with `overrides` applied."""
    row: dict[str, Any] = {
        "id": "CH000000000001",
        "novel_id": NOVEL_ID,
        "url": "https://example.com/novel/1",
        "position": 1,
        "updated_at": NOW,
    }
    return row | overrides


def export_row(**overrides: Any) -> dict[str, Any]:
    """Return a valid `waiting` export row with `overrides` applied."""
    row: dict[str, Any] = {
        "id": "EX000000000001",
        "novel_id": NOVEL_ID,
        "first_chapter_id": "CH000000000001",
        "last_chapter_id": "CH000000000001",
        "mime": "application/epub+zip",
        "filename": "novel.epub",
        "created_at": NOW,
    }
    return row | overrides


def insert_novel_with_chapter(connection: Connection) -> None:
    """Insert a host, a novel and one chapter."""
    insert_host(connection)
    connection.execute(insert(novels).values(**novel_row()))
    connection.execute(insert(chapters).values(**chapter_row()))


def insert_export(connection: Connection, **overrides: Any) -> None:
    """Insert a host, a novel, one chapter and an export row."""
    insert_novel_with_chapter(connection)
    connection.execute(insert(exports).values(**export_row(**overrides)))


def assert_no_events_after_delete(engine: Engine, novel_id: str) -> None:
    """Assert that the novel's `deleted` NV event is its last event (no ghost CH/NV/EX after it)."""
    with engine.connect() as connection:
        rows = connection.execute(
            select(events.c.seq, events.c.payload)
            .where(events.c.novel_id == novel_id)
            .order_by(events.c.seq)
        ).all()
    deleted = [row.seq for row in rows if row.payload.get("deleted")]
    assert len(deleted) == 1
    assert rows[-1].seq == deleted[0]


def trash_keys(engine: Engine) -> list[str]:
    """Return every key in the storage trash, ignoring the grace age of `Trash.take`."""
    with engine.connect() as connection:
        return list(
            connection.execute(
                select(storage_trash.c.key).order_by(storage_trash.c.at, storage_trash.c.key)
            ).scalars()
        )
