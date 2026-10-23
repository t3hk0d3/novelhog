"""UTCDateTime, engine creation and the write/read transaction helpers."""

import sqlite3
import threading
import time
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.exc import DBAPIError, OperationalError

from novelhog.repo import Busy, Repo, db
from novelhog.repo.schema import hosts
from novelhog.repo.types import UTCDateTime

INSTANT = datetime(2026, 10, 19, 7, 41, 10, 123456, tzinfo=UTC)


def _insert_host(repo: Repo, name: str, at: datetime) -> None:
    with db.write(repo.engine) as connection:
        connection.execute(hosts.insert().values(host=name, supported=True, updated_at=at))


def _stored_at(repo: Repo, name: str) -> datetime:
    with db.read(repo.engine) as connection:
        value = connection.execute(select(hosts.c.updated_at).where(hosts.c.host == name)).scalar()
    assert isinstance(value, datetime)
    return value


class TestUTCDateTime:
    def test_utc_datetime__round_trips_an_aware_utc_value_unchanged(self, repo: Repo) -> None:
        _insert_host(repo, "a.com", INSTANT)

        assert _stored_at(repo, "a.com") == INSTANT
        assert _stored_at(repo, "a.com").utcoffset() == timedelta(0)

    def test_utc_datetime__stores_other_zone_as_same_instant_returned_in_utc(
        self, repo: Repo
    ) -> None:
        plus_three = INSTANT.astimezone(timezone(timedelta(hours=3)))

        _insert_host(repo, "a.com", plus_three)

        stored = _stored_at(repo, "a.com")
        assert stored == INSTANT
        assert stored.utcoffset() == timedelta(0)

    @pytest.mark.parametrize("dialect_module", [sqlite, postgresql])
    def test_utc_datetime__rejects_naive_datetime(self, dialect_module: Any) -> None:
        processor = UTCDateTime().bind_processor(dialect_module.dialect())
        assert processor is not None

        with pytest.raises(ValueError, match="naive"):
            processor(datetime(2026, 10, 19, 7, 41, 10))  # noqa: DTZ001

    def test_utc_datetime__sqlite_string_order_equals_time_order(self, repo: Repo) -> None:
        if repo.engine.dialect.name != "sqlite":
            pytest.skip("SQLite storage format")
        moments = [
            INSTANT,
            INSTANT + timedelta(microseconds=1),
            INSTANT.replace(microsecond=0),
            INSTANT - timedelta(days=400),
            INSTANT + timedelta(hours=5, milliseconds=3),
        ]
        for index, moment in enumerate(moments):
            _insert_host(repo, f"h{index}.com", moment)

        with db.read(repo.engine) as connection:
            rows = connection.execute(text("SELECT host, updated_at FROM hosts")).all()

        by_string = [host for host, _ in sorted(rows, key=lambda row: row[1])]
        by_time = [f"h{i}.com" for i, _ in sorted(enumerate(moments), key=lambda pair: pair[1])]
        assert by_string == by_time


class TestCreateEngine__WithASqliteUrl:
    def test_create_engine__sets_pragmas_on_every_connection(self, tmp_path: Path) -> None:
        engine = db.create_engine(f"sqlite:///{tmp_path / 'p.db'}")
        connections = [engine.connect(), engine.connect()]
        try:
            for connection in connections:
                pragmas = {
                    name: connection.exec_driver_sql(f"PRAGMA {name}").scalar()
                    for name in ("journal_mode", "foreign_keys", "busy_timeout")
                }
                assert pragmas == {"journal_mode": "wal", "foreign_keys": 1, "busy_timeout": 15000}
        finally:
            for connection in connections:
                connection.close()
            engine.dispose()


BOUND_VALUE = "bound-value-4f1c"


class TestCreateEngine__OnAFailingStatement:
    def test_create_engine__keeps_bound_values_out_of_the_error(self, repo: Repo) -> None:
        engine = db.create_engine(repo.engine.url.render_as_string(hide_password=False))
        try:
            with engine.connect() as connection, pytest.raises(DBAPIError) as raised:
                statement = text("SELECT * FROM no_such_table WHERE x = :v")
                connection.execute(statement, {"v": BOUND_VALUE})
            assert BOUND_VALUE not in str(raised.value)
            assert engine.hide_parameters is True
        finally:
            engine.dispose()


class TestCreateEngine__WithAPostgresUrl:
    @pytest.mark.parametrize(
        "url",
        [
            "postgresql://u:p@localhost/d",
            "postgres://u:p@localhost/d",
            "postgresql+psycopg://u@h/d",
            "postgresql+psycopg2://u@h/d",
        ],
    )
    def test_create_engine__uses_the_psycopg_driver(self, url: str) -> None:
        engine = db.create_engine(url)
        try:
            assert engine.dialect.driver == "psycopg"
        finally:
            engine.dispose()

    def test_create_engine__discards_a_connection_closed_by_the_server(self, repo: Repo) -> None:
        if repo.engine.dialect.name != "postgresql":
            pytest.skip("pre-ping is a Postgres pool setting")
        with db.read(repo.engine) as connection:
            pid = connection.execute(text("SELECT pg_backend_pid()")).scalar()
        other = db.create_engine(repo.engine.url.render_as_string(hide_password=False))
        try:
            with db.write(other) as connection:
                connection.execute(text("SELECT pg_terminate_backend(:pid)"), {"pid": pid})
        finally:
            other.dispose()

        with db.read(repo.engine) as connection:
            assert connection.execute(text("SELECT 1")).scalar() == 1


class TestWrite__OnSuccess:
    def test_write__commits_the_transaction(self, repo: Repo) -> None:
        _insert_host(repo, "a.com", INSTANT)

        with db.read(repo.engine) as connection:
            assert connection.execute(select(hosts.c.host)).scalars().all() == ["a.com"]


class TestWrite__OnException:
    def test_write__rolls_back_and_reraises(self, repo: Repo) -> None:
        with pytest.raises(RuntimeError, match="boom"), db.write(repo.engine) as connection:
            connection.execute(
                hosts.insert().values(host="a.com", supported=True, updated_at=INSTANT)
            )
            msg = "boom"
            raise RuntimeError(msg)

        with db.read(repo.engine) as connection:
            assert connection.execute(select(hosts.c.host)).all() == []


def _hold_write_lock(path: str, seconds: float, locked: threading.Event) -> threading.Thread:
    def hold() -> None:
        other = sqlite3.connect(path, isolation_level=None)
        other.execute("BEGIN IMMEDIATE")
        locked.set()
        time.sleep(seconds)
        other.execute("ROLLBACK")
        other.close()

    thread = threading.Thread(target=hold)
    thread.start()
    assert locked.wait(5)
    return thread


class TestWrite__WithAnotherWriterHoldingTheLock:
    def test_write__waits_and_succeeds_instead_of_raising_locked(self, tmp_path: Path) -> None:
        path = tmp_path / "w.db"
        engine = db.create_engine(f"sqlite:///{path}")
        repo = Repo(f"sqlite:///{path}")
        repo.migrate()
        thread = _hold_write_lock(str(path), 1.0, threading.Event())

        started = time.monotonic()
        with db.write(engine) as connection:
            connection.execute(select(hosts.c.host)).all()  # read first, then write
            connection.execute(
                hosts.insert().values(host="a.com", supported=True, updated_at=INSTANT)
            )
        waited = time.monotonic() - started
        thread.join()

        assert waited >= 0.5
        with db.read(engine) as connection:
            assert connection.execute(select(hosts.c.host)).scalars().all() == ["a.com"]
        engine.dispose()
        repo.engine.dispose()
        engine.dispose()
        repo.engine.dispose()

    def test_write__raises_busy_when_the_lock_outlasts_the_busy_timeout(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = tmp_path / "b.db"
        Repo(f"sqlite:///{path}").migrate()
        monkeypatch.setattr(db, "SQLITE_BUSY_TIMEOUT_MS", 100)
        engine = db.create_engine(f"sqlite:///{path}")
        thread = _hold_write_lock(str(path), 1.0, threading.Event())

        try:
            with pytest.raises(Busy), db.write(engine):
                pass
        finally:
            thread.join()
            engine.dispose()


@pytest.fixture
def postgres_repo(repo: Repo) -> Repo:
    if repo.engine.dialect.name != "postgresql":
        pytest.skip("deadlocks and lock timeouts are Postgres behaviour")
    for name in ("a.com", "b.com"):
        _insert_host(repo, name, INSTANT)
    return repo


def _update_host(repo: Repo, name: str) -> Any:
    return hosts.update().where(hosts.c.host == name).values(updated_at=INSTANT)


class TestWrite__OnPostgresLockConflicts:
    def test_write__reports_a_deadlock_as_busy(self, postgres_repo: Repo) -> None:
        barrier = threading.Barrier(2, timeout=30)
        outcomes: list[str] = []

        def crossing(first: str, second: str) -> None:
            try:
                with db.write(postgres_repo.engine) as connection:
                    connection.execute(_update_host(postgres_repo, first))
                    barrier.wait()
                    connection.execute(_update_host(postgres_repo, second))
                outcomes.append("ok")
            except Busy:
                outcomes.append("busy")

        threads = [
            threading.Thread(target=crossing, args=pair, daemon=True)
            for pair in (("a.com", "b.com"), ("b.com", "a.com"))
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
            assert not thread.is_alive()
        assert sorted(outcomes) == ["busy", "ok"]

    def test_write__reports_a_lock_timeout_as_busy(self, postgres_repo: Repo) -> None:
        holding, release = threading.Event(), threading.Event()

        def hold() -> None:
            with db.write(postgres_repo.engine) as connection:
                connection.execute(_update_host(postgres_repo, "a.com"))
                holding.set()
                release.wait(timeout=30)

        holder = threading.Thread(target=hold, daemon=True)
        holder.start()
        assert holding.wait(timeout=30)
        try:
            with pytest.raises(Busy), db.write(postgres_repo.engine) as connection:
                connection.execute(text("SET LOCAL lock_timeout = '200ms'"))
                connection.execute(_update_host(postgres_repo, "a.com"))
        finally:
            release.set()
            holder.join(timeout=30)

    def test_write__other_operational_errors_are_not_busy(self, postgres_repo: Repo) -> None:
        with pytest.raises(OperationalError) as caught, db.write(postgres_repo.engine) as conn:
            conn.execute(text("SET LOCAL statement_timeout = '50ms'"))
            conn.execute(text("SELECT pg_sleep(2)"))
        assert caught.value.orig.sqlstate == "57014"  # type: ignore[union-attr]
        assert not isinstance(caught.value, Busy)
