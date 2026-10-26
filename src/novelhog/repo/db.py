"""Engine, transaction helpers and migrations (spec section 4).

The `PRAGMA` and `BEGIN IMMEDIATE` statements in the SQLite connection events below and in
`optimize` are the only raw SQL in the code base.
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
# The `transaction_info` key of the task rows touched since the last `TaskQueue.stamp`.
TASKS_TOUCHED = "tasks_touched"


class Busy(Exception):  # noqa: N818  (public name fixed by the spec: `repo.Busy`)
    """A write found the database locked (SQLite), or hit a deadlock or lock timeout (Postgres)."""


class UnstampedTaskWriteError(RuntimeError):
    """A write transaction changed a task row and did not call `TaskQueue.stamp` after it.

    The rows would commit without their change cursor, so `since` polling would miss them.
    """


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
    """Create an engine for a SQLite or Postgres URL (Postgres uses the psycopg driver).

    Bound values are kept out of error messages (a logged traceback must not show the VAPID key).
    """
    parsed = make_url(url)
    if parsed.get_backend_name() == "sqlite":
        engine = _create_engine(parsed, hide_parameters=True)
        _configure_sqlite(engine)
        return engine
    if parsed.get_backend_name() in ("postgresql", "postgres"):
        parsed = parsed.set(drivername="postgresql+psycopg")
    return _create_engine(parsed, pool_pre_ping=True, hide_parameters=True)


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


_POSTGRES_BUSY_SQLSTATES = frozenset({"40P01", "55P03"})  # deadlock detected, lock not available


def _is_busy(error: OperationalError) -> bool:
    """Whether the error is SQLite's "database is locked" or a Postgres deadlock or lock timeout."""
    if "database is locked" in str(error.orig):
        return True
    return getattr(error.orig, "sqlstate", None) in _POSTGRES_BUSY_SQLSTATES


@contextmanager
def write(engine: Engine) -> Iterator[Connection]:
    """Run one write transaction: commit on success, roll back and re-raise on error.

    On SQLite the transaction starts with `BEGIN IMMEDIATE`; a lock that outlasts the busy
    timeout is raised as `Busy`. On Postgres a deadlock (40P01) or lock timeout (55P03) is
    raised as `Busy` too (spec section 4, "Lock order").

    A transaction that touched a task row after its last `TaskQueue.stamp` is rolled back with
    `UnstampedTaskWriteError` (tasks spec section 4, "Change cursor").
    """
    try:
        with engine.connect() as connection:
            connection.execution_options(**{_WRITE_OPTION: True})
            with connection.begin():
                yield connection
                if transaction_info(connection).get(TASKS_TOUCHED):
                    message = "a task write was not followed by TaskQueue.stamp"
                    raise UnstampedTaskWriteError(message)
    except OperationalError as error:
        if _is_busy(error):
            raise Busy(str(error.orig)) from error
        raise


def optimize(engine: Engine) -> None:
    """Run SQLite's `PRAGMA optimize` and truncate the WAL; a no-op on Postgres."""
    if engine.dialect.name != "sqlite":
        return
    # The checkpoint cannot run inside a transaction, so bypass the "begin" event's BEGIN by using
    # the driver connection, which is in autocommit mode (see `_configure_sqlite`).
    connection = engine.raw_connection()
    try:
        cursor = connection.cursor()
        cursor.execute("PRAGMA optimize")
        cursor.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        cursor.fetchall()
        cursor.close()
    finally:
        connection.close()


_TRANSACTION_INFO = "novelhog_transaction_info"


def transaction_info(conn: Connection) -> dict[str, Any]:
    """Return a scratch dict that lives as long as the connection's current transaction.

    `Connection.info` belongs to the pooled DBAPI connection and outlives the transaction, so the
    dict is tied to the transaction object and starts empty in every new transaction.
    """
    transaction = conn.get_transaction()
    held = conn.info.get(_TRANSACTION_INFO)
    if held is None or held[0] is not transaction:
        held = (transaction, {})
        conn.info[_TRANSACTION_INFO] = held
    info: dict[str, Any] = held[1]
    return info


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
        connection.execution_options(**{_WRITE_OPTION: True})  # SQLite: BEGIN IMMEDIATE
        config.attributes["connection"] = connection
        command.upgrade(config, "head")
    return ScriptDirectory.from_config(config).get_current_head() or ""


def revisions(engine: Engine, *, script_location: Path = MIGRATIONS_DIR) -> Revisions:
    """Return the database's current revision and this build's head revision."""
    head = ScriptDirectory.from_config(alembic_config(script_location)).get_current_head()
    with read(engine) as connection:
        current = MigrationContext.configure(connection).get_current_revision()
    return Revisions(current=current, head=head or "")


def require_current(engine: Engine, *, script_location: Path = MIGRATIONS_DIR) -> None:
    """Raise `SchemaOutdated` unless the database is at this build's head revision."""
    state = revisions(engine, script_location=script_location)
    if not state.is_current:
        raise SchemaOutdated(state.current, state.head)
