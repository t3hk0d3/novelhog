"""Worker trash emptying and periodic maintenance (spec sections 4 and 6.1)."""

import os
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select, update
from structlog.testing import capture_logs

from novelhog.config import Settings
from novelhog.repo import Repo, db
from novelhog.repo import events as event_log
from novelhog.repo import trash as trash_table
from novelhog.repo.schema import consumers
from novelhog.repo.schema import events as events_table
from novelhog.repo.trash import TRASH_GRACE_S
from novelhog.storage import LocalStaticStorage
from novelhog.worker import maintenance
from novelhog.worker.loop import MAINTENANCE_INTERVAL, Worker
from tests.conftest import FakeClock
from tests.integration.worker.conftest import FakeAdapter, FakeFetcher, RecordingStop

NOVEL = "NV000000000001"
DAY = 86400


@pytest.fixture
def storage(tmp_path: Path) -> LocalStaticStorage:
    return LocalStaticStorage(tmp_path / "static", "/static")


def _trash(repo: Repo, keys: list[str]) -> None:
    """Trash the keys as if they had been discarded `TRASH_GRACE_S` ago (so `take` returns them)."""
    with db.write(repo.engine) as conn:
        trash_table.add(conn, keys, repo.clock() - timedelta(seconds=TRASH_GRACE_S))


def _put(storage: LocalStaticStorage, key: str) -> None:
    storage.put(key, b"x", "image/jpeg")


def _exists(storage: LocalStaticStorage, key: str) -> bool:
    return (storage._path(key)).exists()  # noqa: SLF001 - checking the file on disk


class TestEmptyTrash:
    def test_empty_trash__deletes_the_files_and_their_rows(
        self, repo: Repo, storage: LocalStaticStorage
    ) -> None:
        _put(storage, "covers/a.jpg")
        _put(storage, "covers/b.jpg")
        _trash(repo, ["covers/a.jpg", "covers/b.jpg"])

        assert maintenance.empty_trash(repo, storage) == 2
        assert not _exists(storage, "covers/a.jpg")
        assert not _exists(storage, "covers/b.jpg")
        assert repo.trash.take() == []

    def test_empty_trash__handles_at_most_limit_keys_per_call(
        self, repo: Repo, storage: LocalStaticStorage
    ) -> None:
        keys = [f"covers/{n}.jpg" for n in range(3)]
        for key in keys:
            _put(storage, key)
        _trash(repo, keys)

        assert maintenance.empty_trash(repo, storage, limit=2) == 2
        assert len(repo.trash.take()) == 1
        assert sum(_exists(storage, key) for key in keys) == 1

    def test_empty_trash__removes_the_row_of_a_key_whose_file_is_missing(
        self, repo: Repo, storage: LocalStaticStorage
    ) -> None:
        _trash(repo, ["covers/gone.jpg"])

        assert maintenance.empty_trash(repo, storage) == 1
        assert repo.trash.take() == []

    def test_empty_trash__forgets_a_key_the_storage_rejects(
        self, repo: Repo, storage: LocalStaticStorage
    ) -> None:
        _trash(repo, ["../escape"])

        with capture_logs() as logs:
            assert maintenance.empty_trash(repo, storage) == 1
        assert repo.trash.take() == []
        assert [entry["event"] for entry in logs] == ["trash_key_invalid"]


class TestMaintain:
    def _add(self, repo: Repo, entity_id: str, payload: dict[str, object] | None = None) -> None:
        with db.write(repo.engine) as conn:
            event_log.append(conn, NOVEL, entity_id, payload or {}, now=repo.clock())

    def test_maintain__prunes_the_event_log_and_reports_it(
        self, repo: Repo, clock: FakeClock
    ) -> None:
        for entity in (NOVEL, NOVEL):
            self._add(repo, entity)
        clock.advance(seconds=DAY + 1)
        self._add(repo, "CH000000000001")

        report = maintenance.maintain(repo, now=clock())

        assert report.deleted == 1
        assert report.consumers_moved == []
        assert report.at == clock()
        with db.read(repo.engine) as conn:
            assert list(conn.execute(select(events_table.c.seq).order_by("seq")).scalars()) == [
                2,
                3,
            ]

    def test_maintain__warns_for_every_consumer_moved_with_the_skipped_range(
        self, repo: Repo, clock: FakeClock
    ) -> None:
        self._add(repo, NOVEL, {"released": ["c1"], "followed": True})  # seq 1
        self._add(repo, "CH000000000001")  # 2
        clock.advance(seconds=10 * DAY)
        self._add(repo, "CH000000000002")  # 3
        repo.consumers.claim("dead", "w", 10)
        with db.write(repo.engine) as conn:
            conn.execute(update(consumers).values(last_seq=0))

        with capture_logs() as logs:
            report = maintenance.maintain(repo, now=clock())

        assert report.consumers_moved == [("dead", 0, 2, 1)]
        warnings = [entry for entry in logs if entry["log_level"] == "warning"]
        assert len(warnings) == 1
        assert warnings[0]["consumer"] == "dead"
        assert (warnings[0]["skipped_from_seq"], warnings[0]["skipped_to_seq"]) == (1, 2)
        assert warnings[0]["skipped_releases"] == 1

    def test_maintain__on_sqlite_truncates_the_wal_file(self, repo: Repo, clock: FakeClock) -> None:
        if repo.engine.dialect.name != "sqlite":
            pytest.skip("SQLite only")
        for _ in range(20):
            self._add(repo, NOVEL)
        wal = Path(f"{repo.engine.url.database}-wal")
        assert wal.exists()
        assert wal.stat().st_size > 0

        maintenance.maintain(repo, now=clock())

        assert os.path.getsize(wal) == 0

    def test_optimize__runs_on_either_database(self, repo: Repo) -> None:
        repo.optimize()  # must not raise on either database


class TestStep__Housekeeping:
    @pytest.fixture
    def worker(
        self,
        repo: Repo,
        adapter: FakeAdapter,
        fetcher: FakeFetcher,
        settings: Settings,
        storage: LocalStaticStorage,
        clock: FakeClock,
    ) -> Worker:
        return Worker(
            repo, adapter, fetcher, storage, {}, settings, stop=RecordingStop(), clock=clock
        )

    def test_step__empties_the_trash_every_iteration(
        self, repo: Repo, worker: Worker, storage: LocalStaticStorage
    ) -> None:
        _put(storage, "covers/a.jpg")
        _trash(repo, ["covers/a.jpg"])

        worker.step()

        assert not _exists(storage, "covers/a.jpg")
        assert repo.trash.take() == []

    def test_step__keeps_going_when_emptying_the_trash_fails(
        self, repo: Repo, worker: Worker, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fail(*_args: object, **_kwargs: object) -> int:
            raise RuntimeError  # noqa: TRY003

        monkeypatch.setattr(maintenance, "empty_trash", fail)
        with capture_logs() as logs:
            assert worker.step() == "idle"
        assert "empty_trash_failed" in [entry["event"] for entry in logs]

    def test_step__runs_maintain_at_most_once_per_hour_on_idle_passes(
        self, worker: Worker, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[object] = []
        monkeypatch.setattr(maintenance, "maintain", lambda _repo, *, now: calls.append(now))

        assert worker.step() == "idle"
        assert len(calls) == 1
        clock.advance(seconds=MAINTENANCE_INTERVAL.total_seconds() - 1)
        worker.step()
        assert len(calls) == 1
        clock.advance(seconds=1)
        worker.step()
        assert len(calls) == 2

    def test_step__does_not_maintain_when_work_was_done(
        self, repo: Repo, worker: Worker, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[object] = []
        monkeypatch.setattr(maintenance, "maintain", lambda _repo, *, now: calls.append(now))
        monkeypatch.setattr(worker._runner, "step", lambda: True)  # noqa: SLF001

        assert worker.step() == "task"
        assert calls == []

    def test_step__logs_a_maintenance_failure_and_waits_an_hour_to_retry(
        self, worker: Worker, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        attempts: list[int] = []

        def fail(_repo: Repo, *, now: object) -> None:
            attempts.append(1)
            raise RuntimeError  # noqa: TRY003

        monkeypatch.setattr(maintenance, "maintain", fail)
        with capture_logs() as logs:
            assert worker.step() == "idle"
            assert worker.step() == "idle"
        assert len(attempts) == 1
        assert "maintenance_failed" in [entry["event"] for entry in logs]
        clock.advance(seconds=timedelta(hours=1).total_seconds())
        worker.step()
        assert len(attempts) == 2
