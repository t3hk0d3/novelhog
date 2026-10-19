"""Row builders for integration tests that write straight to the schema."""

from datetime import datetime
from typing import Any

from sqlalchemy import Connection, insert

from novelhog.repo.schema import chapters, exports, hosts, novels

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
