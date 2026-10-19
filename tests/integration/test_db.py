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


class TestCreateEngine__WithAPostgresUrl:
    @pytest.mark.parametrize("url", ["postgresql://u:p@localhost/d", "postgresql+psycopg://u@h/d"])
    def test_create_engine__uses_the_psycopg_driver(self, url: str) -> None:
        engine = db.create_engine(url)

        assert engine.dialect.driver == "psycopg"
        assert engine.pool._pre_ping is True  # noqa: SLF001


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
            connection.execute(
                hosts.insert().values(host="a.com", supported=True, updated_at=INSTANT)
            )
        waited = time.monotonic() - started
        thread.join()

        assert waited >= 0.5
        with db.read(engine) as connection:
            assert connection.execute(select(hosts.c.host)).scalars().all() == ["a.com"]

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
