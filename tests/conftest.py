"""Shared pytest fixtures."""

import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import structlog
from sqlalchemy import create_engine as sa_create_engine
from sqlalchemy import delete, insert, make_url, text

from novelhog.repo import Repo, db
from novelhog.repo.schema import event_clock, metadata


@pytest.fixture(autouse=True)
def _reset_structlog() -> Iterator[None]:
    """Restore structlog's defaults and context variables after every test."""
    yield
    structlog.reset_defaults()
    structlog.contextvars.clear_contextvars()


class FakeClock:
    """A controllable clock: call it for the current time, `advance` to move it forward."""

    def __init__(self) -> None:
        self.current = datetime(2026, 10, 19, 12, 0, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.current

    def advance(self, **delta: float) -> datetime:
        """Move the clock forward by a `timedelta(**delta)` and return the new time."""
        self.current += timedelta(**delta)
        return self.current


@pytest.fixture(scope="session")
def database_url(tmp_path_factory: pytest.TempPathFactory) -> str:
    """The test database URL: `NOVELHOG_TEST_DATABASE_URL`, else a temporary SQLite file."""
    configured = os.environ.get("NOVELHOG_TEST_DATABASE_URL")
    if configured:
        # A leaked lock fails a test after 10 s instead of hanging the suite.
        url = make_url(configured)
        if url.get_backend_name() in ("postgresql", "postgres"):
            url = url.update_query_dict({"options": "-c lock_timeout=10000"})
        return url.render_as_string(hide_password=False)
    return f"sqlite:///{tmp_path_factory.mktemp('db') / 'test.db'}"


@pytest.fixture(scope="session")
def migrated_database(database_url: str) -> str:
    """The test database with every migration applied (once per session)."""
    engine = db.create_engine(database_url)
    db.migrate(engine)
    engine.dispose()
    return database_url


@contextmanager
def _scratch_database(database_url: str, directory: Path, label: str) -> Iterator[str]:
    """Create a fresh, unmigrated database next to `database_url` and drop it afterwards."""
    parsed = make_url(database_url)
    if parsed.get_backend_name() == "sqlite":
        yield f"sqlite:///{directory / (label + '.db')}"
        return
    name = f"novelhog_test_{uuid.uuid4().hex[:12]}"
    admin = sa_create_engine(
        parsed.set(drivername="postgresql+psycopg"), isolation_level="AUTOCOMMIT"
    )
    with admin.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{name}"'))
    try:
        yield parsed.set(database=name).render_as_string(hide_password=False)
    finally:
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


@pytest.fixture
def empty_database_url(database_url: str, tmp_path: Path) -> Iterator[str]:
    """A fresh, unmigrated database on the same kind of server as `database_url`."""
    with _scratch_database(database_url, tmp_path, "empty") as url:
        yield url


@pytest.fixture
def reference_database_url(database_url: str, tmp_path: Path) -> Iterator[str]:
    """A second fresh, unmigrated database, for comparisons against `empty_database_url`."""
    with _scratch_database(database_url, tmp_path, "reference") as url:
        yield url


@pytest.fixture
def clock() -> FakeClock:
    """A controllable clock."""
    return FakeClock()


@pytest.fixture
def repo(migrated_database: str, clock: FakeClock) -> Iterator[Repo]:
    """A `Repo` on the migrated test database, emptied before the test, using the fake clock."""
    repository = Repo(migrated_database, clock=clock)
    with db.write(repository.engine) as connection:
        for table in reversed(metadata.sorted_tables):
            connection.execute(delete(table))
        connection.execute(insert(event_clock).values(id=1, seq=0))
    yield repository
    repository.engine.dispose()
