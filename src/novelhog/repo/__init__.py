"""Database access for novelhog: schema, engine, migrations and the `Repo` facade."""

from collections.abc import Callable
from datetime import datetime

from novelhog.repo import db
from novelhog.repo.db import Busy, Revisions, SchemaOutdated
from novelhog.repo.events import Events
from novelhog.repo.hosts import Hosts
from novelhog.repo.novels import Novels
from novelhog.repo.settings import Settings
from novelhog.repo.types import utcnow

__all__ = ["Busy", "Repo", "Revisions", "SchemaOutdated", "utcnow"]


class Repo:
    """Entry point to the database; sub-repositories are attributes (later tasks add more)."""

    def __init__(
        self,
        url: str,
        *,
        clock: Callable[[], datetime] = utcnow,
        static_base_url: str = "/static",
        refresh_min_interval: int = 21600,
    ) -> None:
        self.engine = db.create_engine(url)
        self.clock = clock
        self.static_base_url = static_base_url
        self.events = Events(self.engine)
        self.hosts = Hosts(self.engine, clock)
        self.novels = Novels(
            self.engine,
            clock,
            static_base_url=static_base_url,
            refresh_min_interval=refresh_min_interval,
        )
        self.settings = Settings(self.engine, clock)

    def migrate(self) -> str:
        """Apply pending migrations and return the head revision."""
        return db.migrate(self.engine)

    def require_current(self) -> None:
        """Raise `SchemaOutdated` unless the schema is at this build's head revision."""
        db.require_current(self.engine)

    def revisions(self) -> Revisions:
        """Return the current and head revisions."""
        return db.revisions(self.engine)
