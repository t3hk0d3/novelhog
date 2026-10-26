"""Small key/value store (spec section 4, `settings`)."""

from typing import Any

from sqlalchemy import select

from novelhog.repo import db
from novelhog.repo.context import RepoContext
from novelhog.repo.schema import settings as settings_table
from novelhog.repo.sql import dialect_insert


class Settings:
    """Reads and writes settings rows."""

    def __init__(self, context: RepoContext) -> None:
        self._engine = context.engine
        self._clock = context.clock

    def get(self, key: str) -> dict[str, Any] | None:
        """Return the stored value, or None if the key is missing."""
        with db.read(self._engine) as conn:
            statement = select(settings_table.c.value).where(settings_table.c.key == key)
            return conn.execute(statement).scalar_one_or_none()

    def put_if_absent(self, key: str, value: dict[str, Any]) -> bool:
        """Store `value` unless the key exists; return whether this call stored it.

        The insert ignores a conflict, so concurrent callers cannot both succeed.
        """
        statement = (
            dialect_insert(self._engine, settings_table)
            .values(key=key, value=value, updated_at=self._clock())
            .on_conflict_do_nothing(index_elements=["key"])
            .returning(settings_table.c.key)
        )
        with db.write(self._engine) as conn:
            return conn.execute(statement).first() is not None
