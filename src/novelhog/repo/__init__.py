"""Database access for novelhog: schema, engine, migrations and the `Repo` facade."""

from collections.abc import Callable
from datetime import datetime

from novelhog.repo import db
from novelhog.repo.db import Busy, Revisions, SchemaOutdated
from novelhog.repo.types import utcnow

__all__ = ["Busy", "Repo", "Revisions", "SchemaOutdated", "utcnow"]


class Repo:
    """Entry point to the database; sub-repositories are attached by later tasks."""

    def __init__(
        self,
        url: str,
        *,
        clock: Callable[[], datetime] = utcnow,
        static_base_url: str = "/static",
    ) -> None:
        self.engine = db.create_engine(url)
        self.clock = clock
        self.static_base_url = static_base_url

    def migrate(self) -> str:
        """Apply pending migrations and return the head revision."""
        return db.migrate(self.engine)

    def require_current(self) -> None:
        """Raise `SchemaOutdated` unless the schema is at this build's head revision."""
        db.require_current(self.engine)

    def revisions(self) -> Revisions:
        """Return the current and head revisions."""
        return db.revisions(self.engine)
