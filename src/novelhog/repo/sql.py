"""Dialect helpers shared by the repositories."""

from sqlalchemy import Engine, Table
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.dialects.postgresql import Insert as PostgresInsert
from sqlalchemy.dialects.sqlite import Insert as SqliteInsert


def dialect_insert(engine: Engine, table: Table) -> PostgresInsert | SqliteInsert:
    """Return an `INSERT` that supports `on_conflict_do_nothing` and `on_conflict_do_update`."""
    if engine.dialect.name == "postgresql":
        return postgresql.insert(table)
    return sqlite.insert(table)
