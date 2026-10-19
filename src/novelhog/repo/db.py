"""Engine, transaction helpers and migrations (spec section 4).

The `PRAGMA` and `BEGIN IMMEDIATE` statements in the SQLite connection events below are the only
raw SQL in the code base.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Connection, Engine, event
from sqlalchemy import create_engine as _create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError
from sqlalchemy.schema import SchemaItem

SQLITE_BUSY_TIMEOUT_MS = 15000
MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
_WRITE_OPTION = "novelhog_write"


class Busy(Exception):  # noqa: N818  (public name fixed by the spec: `repo.Busy`)
    """A write found the database locked after the busy timeout."""


class SchemaOutdated(Exception):  # noqa: N818  (public name fixed by the spec)
    """The database revision differs from the head revision of this build."""

    def __init__(self, current: str | None, head: str) -> None:
        self.current = current
        self.head = head
        super().__init__(
            f"Database schema is at {current or 'none'}, this build needs {head}; "
            "run `novelhog db migrate`"
        )


@dataclass(frozen=True)
class Revisions:
    """The database's current revision (None if unmigrated) and the build's head revision."""

    current: str | None
    head: str

    @property
    def is_current(self) -> bool:
        """Whether the database is at the head revision."""
        return self.current == self.head


def create_engine(url: str) -> Engine:
    """Create an engine for a SQLite or Postgres URL (Postgres uses the psycopg driver)."""
    parsed = make_url(url)
    if parsed.get_backend_name() == "sqlite":
        engine = _create_engine(parsed)
        _configure_sqlite(engine)
        return engine
    if parsed.drivername in ("postgresql", "postgres"):
        parsed = parsed.set(drivername="postgresql+psycopg")
    return _create_engine(parsed, pool_pre_ping=True)


def _configure_sqlite(engine: Engine) -> None:
    @event.listens_for(engine, "connect")
    def on_connect(dbapi_connection: Any, _record: object) -> None:
        # Transactions are started by the "begin" event below, not by the driver.
        dbapi_connection.isolation_level = None
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode = WAL")
        cursor.execute("PRAGMA foreign_keys = ON")
        cursor.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")
        cursor.close()

    @event.listens_for(engine, "begin")
    def on_begin(connection: Connection) -> None:
        if connection.get_execution_options().get(_WRITE_OPTION):
            connection.exec_driver_sql("BEGIN IMMEDIATE")
        else:
            connection.exec_driver_sql("BEGIN")


def _is_locked(error: OperationalError) -> bool:
    return "database is locked" in str(error.orig)


@contextmanager
def write(engine: Engine) -> Iterator[Connection]:
    """Run one write transaction: commit on success, roll back and re-raise on error.

    On SQLite the transaction starts with `BEGIN IMMEDIATE`; a lock that outlasts the busy
    timeout is raised as `Busy`.
    """
    try:
        with engine.connect() as connection:
            connection.execution_options(**{_WRITE_OPTION: True})
            with connection.begin():
                yield connection
    except OperationalError as error:
        if _is_locked(error):
            raise Busy(str(error.orig)) from error
        raise


@contextmanager
def read(engine: Engine) -> Iterator[Connection]:
    """Run a read-only transaction (one consistent snapshot) and end it without committing."""
    with engine.connect() as connection, connection.begin():
        yield connection


def include_object(
    obj: SchemaItem, name: str | None, type_: str, reflected: bool, compare_to: object
) -> bool:
    """Alembic filter: leave Alembic's own version table out of comparisons."""
    return not (type_ == "table" and name == "alembic_version")


def alembic_config(script_location: Path = MIGRATIONS_DIR) -> Config:
    """Build the Alembic configuration in code; no `alembic.ini` is needed."""
    config = Config()
    config.set_main_option("script_location", str(script_location))
    return config


def migrate(engine: Engine, *, script_location: Path = MIGRATIONS_DIR) -> str:
    """Apply every pending migration (`upgrade head`) and return the head revision."""
    config = alembic_config(script_location)
    with engine.connect() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "head")
    return ScriptDirectory.from_config(config).get_current_head() or ""


def revisions(engine: Engine, *, script_location: Path = MIGRATIONS_DIR) -> Revisions:
    """Return the database's current revision and this build's head revision."""
    head = ScriptDirectory.from_config(alembic_config(script_location)).get_current_head()
    with read(engine) as connection:
        current = MigrationContext.configure(connection).get_current_revision()
    return Revisions(current=current, head=head or "")


def require_current(engine: Engine) -> None:
    """Raise `SchemaOutdated` unless the database is at this build's head revision."""
    state = revisions(engine)
    if not state.is_current:
        raise SchemaOutdated(state.current, state.head)
