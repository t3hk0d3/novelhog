"""Migrations: initial schema, idempotence, working directory, SQLite batch rebuilds."""

import asyncio
import json
import re
import shutil
import textwrap
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import Engine, delete, event, insert, inspect, select, text, update
from sqlalchemy.exc import IntegrityError
from structlog.testing import capture_logs

from novelhog import config
from novelhog.api.routes_events import stream
from novelhog.domain.chapters import ChapterState
from novelhog.domain.payloads import format_time
from novelhog.repo import Repo, SchemaOutdated, db
from novelhog.repo.schema import chapters, event_clock, events, metadata, novels, tasks
from novelhog.repo.types import UTCDateTime
from novelhog.scraper_adapter.adapter import TocResult
from novelhog.storage import LocalStaticStorage
from novelhog.worker.loop import Worker
from tests.conftest import FakeClock
from tests.integration.helpers import NOW, chapter_row, insert_host, novel_row
from tests.integration.worker.conftest import FakeAdapter, FakeFetcher


@pytest.fixture
def empty_engine(empty_database_url: str) -> Iterator[Engine]:
    """An engine on an empty, unmigrated database."""
    engine = db.create_engine(empty_database_url)
    yield engine
    engine.dispose()


def _tables(engine: Engine) -> set[str]:
    return set(inspect(engine).get_table_names())


class TestMigrate__OnAnEmptyDatabase:
    def test_migrate__creates_every_table_and_seeds_event_clock(self, empty_engine: Engine) -> None:
        head = db.migrate(empty_engine)

        assert _tables(empty_engine) == {t.name for t in metadata.sorted_tables} | {
            "alembic_version"
        }
        with db.read(empty_engine) as connection:
            assert connection.execute(select(event_clock)).all() == [(1, 0)]
        assert db.revisions(empty_engine).current == head

    def test_migrate__leaves_no_difference_from_schema_metadata(self, empty_engine: Engine) -> None:
        db.migrate(empty_engine)

        with empty_engine.connect() as connection:
            context = MigrationContext.configure(
                connection,
                opts={"compare_type": True, "include_object": db.include_object},
            )
            assert compare_metadata(context, metadata) == []


class TestMigrate__OnAnUpToDateDatabase:
    def test_migrate__applies_nothing_and_returns_the_same_head(self, empty_engine: Engine) -> None:
        first = db.migrate(empty_engine)
        with db.write(empty_engine) as connection:
            connection.execute(text("UPDATE event_clock SET seq = 7"))

        second = db.migrate(empty_engine)

        assert second == first
        with db.read(empty_engine) as connection:
            assert connection.execute(select(event_clock.c.seq)).scalar() == 7


class TestMigrate__FromAnotherWorkingDirectory:
    def test_migrate__still_finds_the_migrations(
        self, empty_engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)

        head = db.migrate(empty_engine)

        assert head == db.revisions(empty_engine).current


_REBUILD = """
    from alembic import op

    revision = "0004"
    down_revision = "0003"
    branch_labels = None
    depends_on = None


    def upgrade() -> None:
        with op.batch_alter_table("novels", recreate="always") as batch:
            batch.alter_column("title", existing_type=None, nullable=True)
        {extra}


    def downgrade() -> None:
        pass
"""


def _script_location(tmp_path: Path, extra: str = "") -> Path:
    location = tmp_path / "migrations"
    shutil.copytree(db.MIGRATIONS_DIR, location, ignore=shutil.ignore_patterns("__pycache__"))
    body = textwrap.dedent(_REBUILD).format(extra=extra or "pass")
    (location / "versions" / "0004_rebuild_novels.py").write_text(body)
    return location


@pytest.fixture
def sqlite_with_data(empty_engine: Engine) -> Engine:
    """A migrated SQLite database holding one novel and two chapters."""
    if empty_engine.dialect.name != "sqlite":
        pytest.skip("SQLite batch rebuild")
    db.migrate(empty_engine)
    with db.write(empty_engine) as connection:
        insert_host(connection)
        connection.execute(insert(novels).values(**novel_row()))
        connection.execute(insert(chapters).values(**chapter_row()))
        connection.execute(
            insert(chapters).values(**chapter_row(id="CH000000000002", url="u2", position=2))
        )
    return empty_engine


class TestMigrate__WithABatchRebuildOnSqlite:
    def test_migrate__keeps_every_chapter_row(
        self, sqlite_with_data: Engine, tmp_path: Path
    ) -> None:
        head = db.migrate(sqlite_with_data, script_location=_script_location(tmp_path))

        assert head == "0004"
        with db.read(sqlite_with_data) as connection:
            assert len(connection.execute(select(chapters.c.id)).all()) == 2
            assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1

    def test_migrate__fails_when_foreign_key_check_reports_an_orphan(
        self, sqlite_with_data: Engine, tmp_path: Path
    ) -> None:
        location = _script_location(tmp_path, extra='op.execute("DELETE FROM novels")')

        with pytest.raises(RuntimeError, match="foreign_key_check"):
            db.migrate(sqlite_with_data, script_location=location)

        state = db.revisions(sqlite_with_data)
        assert state.current == "0003"
        with db.read(sqlite_with_data) as connection:
            assert len(connection.execute(select(novels.c.id)).all()) == 1
            assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1


def _downgrade(engine: Engine, revision: str) -> None:
    """Downgrade the database to `revision` through Alembic."""
    config = db.alembic_config()
    with engine.connect() as connection:
        connection.execution_options(**{"novelhog_write": True})
        config.attributes["connection"] = connection
        command.downgrade(config, revision)


class TestMigration0002__Tasks:
    def test_upgrade__from_0001_creates_tasks_and_leaves_autogenerate_clean(
        self, empty_engine: Engine
    ) -> None:
        db.migrate(empty_engine)
        _downgrade(empty_engine, "0001")
        assert "tasks" not in _tables(empty_engine)

        db.migrate(empty_engine)

        assert "tasks" in _tables(empty_engine)
        with empty_engine.connect() as connection:
            context = MigrationContext.configure(
                connection,
                opts={"compare_type": True, "include_object": db.include_object},
            )
            assert compare_metadata(context, metadata) == []

    def test_downgrade__to_0001_drops_tasks_and_restores_the_old_events_check(
        self, empty_engine: Engine
    ) -> None:
        db.migrate(empty_engine)

        _downgrade(empty_engine, "0001")

        assert "tasks" not in _tables(empty_engine)
        assert db.revisions(empty_engine).current == "0001"
        with pytest.raises(IntegrityError), db.write(empty_engine) as connection:
            connection.execute(
                insert(events).values(
                    seq=1, novel_id="NV000000000001", entity_id="TK000000000001", payload={}, at=NOW
                )
            )


def _columns(engine: Engine, table: str) -> set[str]:
    return {c["name"] for c in inspect(engine).get_columns(table)}


_REACH_COLUMNS = {"reach_after", "failures", "failing_since", "last_failure"}


class TestMigration0003__HostReach:
    def test_upgrade__adds_the_reach_columns_with_defaults(self, empty_engine: Engine) -> None:
        db.migrate(empty_engine)
        _downgrade(empty_engine, "0002")
        with db.write(empty_engine) as connection:
            insert_host(connection)

        db.migrate(empty_engine)

        assert _REACH_COLUMNS <= _columns(empty_engine, "hosts")
        assert "retries" in _columns(empty_engine, "tasks")
        with db.read(empty_engine) as connection:
            row = connection.execute(text("SELECT * FROM hosts")).one()
        assert (row.reach_after, row.failures, row.failing_since, row.last_failure) == (
            None,
            0,
            None,
            None,
        )

    def test_downgrade__to_0002_drops_the_reach_columns(self, empty_engine: Engine) -> None:
        db.migrate(empty_engine)

        _downgrade(empty_engine, "0002")

        assert not _REACH_COLUMNS & _columns(empty_engine, "hosts")
        assert "retries" not in _columns(empty_engine, "tasks")
        assert db.revisions(empty_engine).current == "0002"


_V1_NOVELS = sa.table(
    "novels",
    sa.column("id", sa.Text),
    sa.column("host", sa.Text),
    sa.column("toc_url", sa.Text),
    sa.column("title", sa.Text),
    sa.column("language", sa.Text),
    sa.column("refresh_interval", sa.Integer),
    sa.column("last_refreshed_at", UTCDateTime),
    sa.column("next_refresh_at", UTCDateTime),
    sa.column("sync_requested", sa.Boolean),
    sa.column("claimed_by", sa.Text),
    sa.column("claimed_until", UTCDateTime),
    sa.column("created_at", UTCDateTime),
    sa.column("updated_at", UTCDateTime),
)
LATER = NOW + timedelta(hours=6)
REQUESTED = NOW + timedelta(minutes=5)


def _v1_novel(n: int, next_refresh_at: datetime | None, **values: Any) -> dict[str, Any]:
    return {
        "id": f"NV{n:012d}",
        "host": "example.com",
        "toc_url": f"https://example.com/novel/{n}",
        "title": None,
        "language": None,
        "refresh_interval": 3600,
        "last_refreshed_at": None,
        "next_refresh_at": next_refresh_at,
        "sync_requested": False,
        "claimed_by": None,
        "claimed_until": None,
        "created_at": NOW,
        "updated_at": NOW,
    } | values


@pytest.fixture
def v1_engine(empty_engine: Engine) -> Engine:
    """A database at revision 0001 holding v1 novels: due later, requested, mid-sync."""
    db.migrate(empty_engine)
    _downgrade(empty_engine, "0001")
    with db.write(empty_engine) as connection:
        insert_host(connection)
        connection.execute(
            insert(_V1_NOVELS),
            [
                _v1_novel(1, LATER),
                _v1_novel(2, REQUESTED, sync_requested=True),
                _v1_novel(3, NOW, claimed_by="w", claimed_until=LATER),
            ],
        )
    return empty_engine


def _refresh_tasks(engine: Engine) -> dict[str, Any]:
    with db.read(engine) as connection:
        rows = connection.execute(select(tasks).where(tasks.c.kind == "novel_refresh")).all()
    return {row.entity_id: row for row in rows}


def _next_refresh_at(engine: Engine) -> dict[str, datetime]:
    """Each novel's API `next_refresh_at`, read through the repo."""
    repo = Repo(engine.url.render_as_string(hide_password=False))
    try:
        return {view.id: view.next_refresh_at for view in repo.novels.list()}
    finally:
        repo.engine.dispose()


class TestMigration0002__Novels:
    def test_upgrade__gives_each_novel_one_queued_refresh_at_its_next_refresh_at(
        self, v1_engine: Engine
    ) -> None:
        db.migrate(v1_engine)

        found = _refresh_tasks(v1_engine)
        assert {novel: (t.state, t.run_after) for novel, t in found.items()} == {
            "NV000000000001": ("queued", LATER),
            "NV000000000002": ("queued", REQUESTED),
            "NV000000000003": ("queued", NOW),
        }
        assert {(t.priority, t.emit_event, t.claimed_by, t.attempts) for t in found.values()} == {
            (300, False, None, 0)
        }

    def test_upgrade__stamps_every_task_with_one_new_clock_value(self, v1_engine: Engine) -> None:
        db.migrate(v1_engine)

        with db.read(v1_engine) as connection:
            clock = connection.execute(select(event_clock.c.seq)).scalar_one()
        assert {t.change_seq for t in _refresh_tasks(v1_engine).values()} == {clock}
        assert clock == 1

    def test_upgrade__keeps_novel_next_refresh_at(self, v1_engine: Engine) -> None:
        with db.read(v1_engine) as connection:
            before = dict(
                connection.execute(select(_V1_NOVELS.c.id, _V1_NOVELS.c.next_refresh_at)).all()
            )

        db.migrate(v1_engine)

        assert _next_refresh_at(v1_engine) == before

    def test_upgrade__drops_the_novel_queue_columns_and_index(self, v1_engine: Engine) -> None:
        db.migrate(v1_engine)

        columns = {c["name"] for c in inspect(v1_engine).get_columns("novels")}
        indexes = {i["name"] for i in inspect(v1_engine).get_indexes("novels")}
        assert columns.isdisjoint(
            {"next_refresh_at", "sync_requested", "claimed_by", "claimed_until"}
        )
        assert "novels_due_refresh" not in indexes

    def test_upgrade__on_an_empty_database_takes_no_clock_value(self, empty_engine: Engine) -> None:
        db.migrate(empty_engine)

        with db.read(empty_engine) as connection:
            assert connection.execute(select(event_clock.c.seq)).scalar_one() == 0


class TestMigration0002__NovelsDowngrade:
    @pytest.fixture
    def downgraded(self, v1_engine: Engine) -> tuple[Engine, datetime, datetime]:
        """Upgraded, then: novel 1 queued later, novel 2 running, novel 3 without live tasks."""
        db.migrate(v1_engine)
        with db.write(v1_engine) as connection:
            connection.execute(
                update(tasks)
                .where(tasks.c.entity_id == "NV000000000002")
                .values(state="running", claimed_by="w", claimed_until=LATER, started_at=NOW)
            )
            connection.execute(delete(tasks).where(tasks.c.entity_id == "NV000000000003"))
        start = datetime.now(UTC)
        _downgrade(v1_engine, "0001")
        return v1_engine, start, datetime.now(UTC)

    def test_downgrade__restores_next_refresh_at_from_queued_else_running_else_now(
        self, downgraded: tuple[Engine, datetime, datetime]
    ) -> None:
        engine, start, end = downgraded

        with db.read(engine) as connection:
            rows = dict(
                connection.execute(select(_V1_NOVELS.c.id, _V1_NOVELS.c.next_refresh_at)).all()
            )
        assert (rows["NV000000000001"], rows["NV000000000002"]) == (LATER, REQUESTED)
        assert start <= rows["NV000000000003"] <= end

    def test_downgrade__restores_no_requests_and_no_claims(
        self, downgraded: tuple[Engine, datetime, datetime]
    ) -> None:
        engine, _, _ = downgraded

        with db.read(engine) as connection:
            rows = connection.execute(
                select(
                    _V1_NOVELS.c.sync_requested,
                    _V1_NOVELS.c.claimed_by,
                    _V1_NOVELS.c.claimed_until,
                )
            ).all()
        assert set(rows) == {(False, None, None)}

    def test_downgrade__restores_the_v1_checks_and_index(
        self, downgraded: tuple[Engine, datetime, datetime]
    ) -> None:
        engine, _, _ = downgraded

        assert "novels_due_refresh" in {i["name"] for i in inspect(engine).get_indexes("novels")}
        with pytest.raises(IntegrityError), db.write(engine) as connection:
            connection.execute(insert(_V1_NOVELS).values(**_v1_novel(4, NOW, claimed_by="w")))
        with pytest.raises(IntegrityError), db.write(engine) as connection:
            connection.execute(insert(_V1_NOVELS).values(**_v1_novel(5, None)))


_V1_EXPORTS = sa.table(
    "exports",
    sa.column("id", sa.Text),
    sa.column("novel_id", sa.Text),
    sa.column("first_chapter_id", sa.Text),
    sa.column("last_chapter_id", sa.Text),
    sa.column("skip_missing", sa.Boolean),
    sa.column("mime", sa.Text),
    sa.column("filename", sa.Text),
    sa.column("state", sa.Text),
    sa.column("error", sa.Text),
    sa.column("claimed_by", sa.Text),
    sa.column("claimed_until", UTCDateTime),
    sa.column("storage_key", sa.Text),
    sa.column("size_bytes", sa.Integer),
    sa.column("ready_at", UTCDateTime),
    sa.column("created_at", UTCDateTime),
)
NOVEL_1 = "NV000000000001"


def _ch(position: int) -> str:
    return f"CH{position:012d}"


def _v1_export(n: int, first: int, last: int, **values: Any) -> dict[str, Any]:
    return {
        "id": f"EX{n:012d}",
        "novel_id": NOVEL_1,
        "first_chapter_id": _ch(first),
        "last_chapter_id": _ch(last),
        "skip_missing": False,
        "mime": "application/epub+zip",
        "filename": f"novel-{n}.epub",
        "state": "waiting",
        "error": None,
        "claimed_by": None,
        "claimed_until": None,
        "storage_key": None,
        "size_bytes": None,
        "ready_at": None,
        "created_at": NOW + timedelta(minutes=n),
    } | values


@pytest.fixture
def v1_exports_engine(v1_engine: Engine) -> Engine:
    """Revision 0001 with chapters 1-2 downloaded and 3 not requested, and exports of them.

    EX1 (1-2) is buildable, EX2 (1-3) waits for chapter 3, EX3 (2-3) is `skip_missing`, EX4 (2-2)
    is building under a claim, EX5 (1-1) is ready.
    """
    with db.write(v1_engine) as connection:
        for position, state in ((1, "downloaded"), (2, "downloaded"), (3, "not_requested")):
            connection.execute(
                insert(chapters).values(
                    **chapter_row(
                        id=_ch(position),
                        novel_id=NOVEL_1,
                        url=f"https://example.com/novel/1/{position}",
                        position=position,
                        state=state,
                    )
                )
            )
        connection.execute(
            insert(_V1_EXPORTS),
            [
                _v1_export(1, 1, 2),
                _v1_export(2, 1, 3),
                _v1_export(3, 2, 3, skip_missing=True),
                _v1_export(4, 2, 2, state="building", claimed_by="w", claimed_until=LATER),
                _v1_export(5, 1, 1, state="ready", storage_key="k", size_bytes=1, ready_at=NOW),
            ],
        )
    return v1_engine


def _build_tasks(engine: Engine) -> dict[str, Any]:
    with db.read(engine) as connection:
        rows = connection.execute(select(tasks).where(tasks.c.kind == "export_build")).all()
    return {row.entity_id: row for row in rows}


def _export_states(engine: Engine) -> dict[str, str]:
    with db.read(engine) as connection:
        return dict(connection.execute(select(_V1_EXPORTS.c.id, _V1_EXPORTS.c.state)).all())


class TestMigration0002__Exports:
    def test_upgrade__sets_building_exports_back_to_waiting(
        self, v1_exports_engine: Engine
    ) -> None:
        db.migrate(v1_exports_engine)

        states = _export_states(v1_exports_engine)
        assert states.pop("EX000000000005") == "ready"
        assert set(states.values()) == {"waiting"}

    def test_upgrade__gives_each_buildable_waiting_export_a_build_task_at_its_created_at(
        self, v1_exports_engine: Engine
    ) -> None:
        db.migrate(v1_exports_engine)

        found = _build_tasks(v1_exports_engine)
        assert sorted(found) == [f"EX{n:012d}" for n in (1, 3, 4)]
        for n in (1, 3, 4):
            task = found[f"EX{n:012d}"]
            at = NOW + timedelta(minutes=n)
            assert (task.state, task.run_after, task.created_at) == ("queued", at, at)
            assert (task.priority, task.emit_event, task.claimed_by) == (200, False, None)

    def test_upgrade__stamps_build_tasks_with_the_migration_clock_value(
        self, v1_exports_engine: Engine
    ) -> None:
        db.migrate(v1_exports_engine)

        with db.read(v1_exports_engine) as connection:
            clock = connection.execute(select(event_clock.c.seq)).scalar_one()
        assert {t.change_seq for t in _build_tasks(v1_exports_engine).values()} == {clock}

    def test_upgrade__drops_the_export_claim_columns(self, v1_exports_engine: Engine) -> None:
        db.migrate(v1_exports_engine)

        columns = {c["name"] for c in inspect(v1_exports_engine).get_columns("exports")}
        assert columns.isdisjoint({"claimed_by", "claimed_until"})


class TestMigration0002__ExportsDowngrade:
    @pytest.fixture
    def downgraded(self, v1_exports_engine: Engine) -> Engine:
        """Upgraded, then EX1's build running (export building), EX3's queued, then downgraded."""
        db.migrate(v1_exports_engine)
        with db.write(v1_exports_engine) as connection:
            connection.execute(
                update(tasks)
                .where(tasks.c.entity_id == "EX000000000001")
                .values(state="running", claimed_by="w", claimed_until=LATER, started_at=NOW)
            )
            connection.execute(
                update(_V1_EXPORTS)
                .where(_V1_EXPORTS.c.id == "EX000000000001")
                .values(state="building")
            )
        _downgrade(v1_exports_engine, "0001")
        return v1_exports_engine

    def test_downgrade__sets_building_exports_back_to_waiting_without_claims(
        self, downgraded: Engine
    ) -> None:
        with db.read(downgraded) as connection:
            rows = connection.execute(
                select(_V1_EXPORTS.c.state, _V1_EXPORTS.c.claimed_by, _V1_EXPORTS.c.claimed_until)
            ).all()
        assert set(rows) == {("waiting", None, None), ("ready", None, None)}

    def test_downgrade__restores_the_v1_export_checks(self, downgraded: Engine) -> None:
        with pytest.raises(IntegrityError), db.write(downgraded) as connection:
            connection.execute(insert(_V1_EXPORTS).values(**_v1_export(6, 3, 3, state="building")))
        with pytest.raises(IntegrityError), db.write(downgraded) as connection:
            connection.execute(insert(_V1_EXPORTS).values(**_v1_export(7, 3, 3, claimed_by="w")))


_V1_CHAPTERS = sa.table(
    "chapters",
    sa.column("id", sa.Text),
    sa.column("novel_id", sa.Text),
    sa.column("url", sa.Text),
    sa.column("position", sa.Integer),
    sa.column("removed_at", UTCDateTime),
    sa.column("title", sa.Text),
    sa.column("state", sa.Text),
    sa.column("priority", sa.Boolean),
    sa.column("released_at", UTCDateTime),
    sa.column("queued_at", UTCDateTime),
    sa.column("claimed_by", sa.Text),
    sa.column("claimed_until", UTCDateTime),
    sa.column("updated_at", UTCDateTime),
)
QUEUED_1 = NOW - timedelta(minutes=30)
QUEUED_2 = NOW - timedelta(minutes=20)


def _v1_chapter(n: int, position: int | None, state: str, **values: Any) -> dict[str, Any]:
    queued = state in ("pending", "downloading")
    return {
        "id": _ch(n),
        "novel_id": NOVEL_1,
        "url": f"https://example.com/novel/1/{n}",
        "position": position,
        "removed_at": None if position is not None else NOW,
        "title": None,
        "state": state,
        "priority": False,
        "released_at": None,
        "queued_at": NOW - timedelta(minutes=n) if queued else None,
        "claimed_by": "w" if state == "downloading" else None,
        "claimed_until": LATER if state == "downloading" else None,
        "updated_at": NOW,
    } | values


@pytest.fixture
def v1_chapters_engine(v1_engine: Engine) -> Engine:
    """Revision 0001 with chapters in every queue situation.

    CH1 pending (user-queued), CH2 downloading (an auto-queued release) under a claim, CH3 pending
    and CH4 downloading without a position (removed upstream), CH5 downloaded, CH6 pending under
    an unstarted claim.
    """
    with db.write(v1_engine) as connection:
        connection.execute(
            insert(_V1_CHAPTERS),
            [
                _v1_chapter(1, 1, "pending", queued_at=QUEUED_1),
                _v1_chapter(2, 2, "downloading", priority=True, queued_at=QUEUED_2),
                _v1_chapter(3, None, "pending"),
                _v1_chapter(4, None, "downloading"),
                _v1_chapter(5, 5, "downloaded"),
                _v1_chapter(6, 6, "pending", claimed_by="w", claimed_until=LATER),
            ],
        )
    return v1_engine


def _download_tasks(engine: Engine) -> dict[str, Any]:
    with db.read(engine) as connection:
        rows = connection.execute(select(tasks).where(tasks.c.kind == "chapter_download")).all()
    return {row.entity_id: row for row in rows}


def _chapter_states(engine: Engine) -> dict[str, str]:
    with db.read(engine) as connection:
        return dict(connection.execute(select(_V1_CHAPTERS.c.id, _V1_CHAPTERS.c.state)).all())


def _queued_at(engine: Engine, chapter_ids: list[str]) -> dict[str, datetime | None]:
    """Each chapter's API `queued_at`, read through the repo."""
    repo = Repo(engine.url.render_as_string(hide_password=False))
    try:
        return {chapter_id: repo.chapters.get(chapter_id).queued_at for chapter_id in chapter_ids}
    finally:
        repo.engine.dispose()


class TestMigration0002__Chapters:
    def test_upgrade__gives_positioned_pending_and_downloading_chapters_a_download_task(
        self, v1_chapters_engine: Engine
    ) -> None:
        db.migrate(v1_chapters_engine)

        found = _download_tasks(v1_chapters_engine)
        assert sorted(found) == [_ch(1), _ch(2), _ch(6)]
        for n, queued_at, priority in ((1, QUEUED_1, 100), (2, QUEUED_2, 110), (6, None, 100)):
            task = found[_ch(n)]
            at = queued_at or NOW - timedelta(minutes=n)
            assert (task.state, task.created_at, task.run_after) == ("queued", at, at)
            assert (task.priority, task.ordinal, task.emit_event, task.claimed_by) == (
                priority, n, False, None,
            )  # fmt: skip

    def test_upgrade__stamps_download_tasks_with_the_migration_clock_value(
        self, v1_chapters_engine: Engine
    ) -> None:
        db.migrate(v1_chapters_engine)

        with db.read(v1_chapters_engine) as connection:
            clock = connection.execute(select(event_clock.c.seq)).scalar_one()
        assert {t.change_seq for t in _download_tasks(v1_chapters_engine).values()} == {clock}

    def test_upgrade__brings_downloading_chapters_back_pending(
        self, v1_chapters_engine: Engine
    ) -> None:
        db.migrate(v1_chapters_engine)

        states = _chapter_states(v1_chapters_engine)
        assert (states[_ch(1)], states[_ch(2)], states[_ch(6)]) == ("pending",) * 3

    def test_upgrade__turns_unpositioned_pending_or_downloading_chapters_not_requested(
        self, v1_chapters_engine: Engine
    ) -> None:
        db.migrate(v1_chapters_engine)

        states = _chapter_states(v1_chapters_engine)
        assert (states[_ch(3)], states[_ch(4)], states[_ch(5)]) == (
            "not_requested", "not_requested", "downloaded",
        )  # fmt: skip

    def test_upgrade__keeps_chapter_queued_at(self, v1_chapters_engine: Engine) -> None:
        with db.read(v1_chapters_engine) as connection:
            before = dict(
                connection.execute(
                    select(_V1_CHAPTERS.c.id, _V1_CHAPTERS.c.queued_at).where(
                        _V1_CHAPTERS.c.position.is_not(None)
                    )
                ).all()
            )

        db.migrate(v1_chapters_engine)

        assert _queued_at(v1_chapters_engine, sorted(before)) == before

    def test_upgrade__drops_the_chapter_queue_columns_checks_and_index(
        self, v1_chapters_engine: Engine
    ) -> None:
        db.migrate(v1_chapters_engine)

        found = inspect(v1_chapters_engine)
        columns = {c["name"] for c in found.get_columns("chapters")}
        assert columns.isdisjoint({"queued_at", "priority", "claimed_by", "claimed_until"})
        assert "chapters_queue" not in {i["name"] for i in found.get_indexes("chapters")}
        checks = {c["name"] for c in found.get_check_constraints("chapters")}
        assert checks.isdisjoint(
            {
                "ck_chapters_queued",
                "ck_chapters_claim",
                "ck_chapters_claim_state",
                "ck_chapters_started",
            }
        )


class TestMigration0002__ChaptersDowngrade:
    @pytest.fixture
    def downgraded(self, v1_chapters_engine: Engine) -> Engine:
        """Upgraded, then CH2's download running (chapter downloading), CH6 without its task."""
        db.migrate(v1_chapters_engine)
        with db.write(v1_chapters_engine) as connection:
            connection.execute(
                update(tasks)
                .where(tasks.c.entity_id == _ch(2))
                .values(state="running", claimed_by="w", claimed_until=LATER, started_at=NOW)
            )
            connection.execute(
                update(_V1_CHAPTERS).where(_V1_CHAPTERS.c.id == _ch(2)).values(state="downloading")
            )
            connection.execute(delete(tasks).where(tasks.c.entity_id == _ch(6)))
        _downgrade(v1_chapters_engine, "0001")
        return v1_chapters_engine

    def _rows(self, engine: Engine) -> dict[str, Any]:
        with db.read(engine) as connection:
            return {row.id: row for row in connection.execute(select(_V1_CHAPTERS)).all()}

    def test_downgrade__resets_running_downloads_to_pending_without_claims(
        self, downgraded: Engine
    ) -> None:
        rows = self._rows(downgraded)
        assert (rows[_ch(2)].state, rows[_ch(2)].claimed_by, rows[_ch(2)].claimed_until) == (
            "pending", None, None,
        )  # fmt: skip

    def test_downgrade__restores_queued_at_and_priority_from_the_live_download_task(
        self, downgraded: Engine
    ) -> None:
        rows = self._rows(downgraded)
        assert (rows[_ch(1)].queued_at, rows[_ch(1)].priority) == (QUEUED_1, False)
        assert (rows[_ch(2)].queued_at, rows[_ch(2)].priority) == (QUEUED_2, True)

    def test_downgrade__a_pending_chapter_without_a_live_task_takes_updated_at_and_priority_0(
        self, downgraded: Engine
    ) -> None:
        row = self._rows(downgraded)[_ch(6)]
        assert (row.state, row.queued_at, row.priority) == ("pending", row.updated_at, False)

    def test_downgrade__leaves_resting_chapters_unqueued(self, downgraded: Engine) -> None:
        rows = self._rows(downgraded)
        for n in (3, 4, 5):
            assert (rows[_ch(n)].queued_at, rows[_ch(n)].priority) == (None, False)

    def test_downgrade__restores_the_v1_chapter_checks_and_index(self, downgraded: Engine) -> None:
        assert "chapters_queue" in {i["name"] for i in inspect(downgraded).get_indexes("chapters")}
        broken: list[tuple[str, dict[str, Any]]] = [
            ("pending", {"queued_at": None}),
            ("not_requested", {"claimed_by": "w", "claimed_until": LATER}),
            ("pending", {"claimed_by": "w", "claimed_until": None}),
            ("downloading", {"claimed_by": None, "claimed_until": None}),
        ]
        for state, values in broken:
            with pytest.raises(IntegrityError), db.write(downgraded) as connection:
                connection.execute(
                    insert(_V1_CHAPTERS).values(**_v1_chapter(9, 9, state, **values))
                )


# --- End to end: a populated v1 database (tasks spec section 9) ---------------------------------

NOVEL_2 = "NV000000000002"
NOVEL_3 = "NV000000000003"
DUE = NOW - timedelta(minutes=10)
REFRESHED = NOW - timedelta(hours=1)
ASKED = NOW - timedelta(minutes=2)
RANGE_CHANGED = "Range changed upstream"
V1_SEQ = 4
"""The clock value of `v1_populated_engine`: the last seq a v1 client saw."""
V1_VIEWS = Path(__file__).parents[1] / "fixtures" / "v1_populated_views.json"
"""The API objects v1's own code shows for `v1_populated_engine`, read at `NOW`.

Regenerate it whenever `populate_v1` changes: build a SQLite database with `populate_v1`, extract
v1's sources (`git archive b5e942a src`), put their `src` first on `sys.path` and write
`views_of(Repo(url, clock=lambda: NOW))` as sorted, indented JSON.
"""
LIVE_DOWNLOADS = {_ch(3), _ch(4), _ch(5), _ch(12)}
LIVE_BUILDS = {"EX000000000001", "EX000000000003", "EX000000000004"}
_CONTENTS = sa.table(
    "chapter_contents",
    sa.column("chapter_id", sa.Text),
    sa.column("markdown", sa.Text),
    sa.column("fetched_at", UTCDateTime),
)


def _v3_chapter(n: int, position: int, state: str, **values: Any) -> dict[str, Any]:
    url = f"https://example.com/novel/3/{position}"
    return _v1_chapter(n, position, state, novel_id=NOVEL_3, url=url, **values)


def _v1_event(seq: int, entity_id: str, novel_id: str, **payload: Any) -> dict[str, Any]:
    return {
        "seq": seq,
        "novel_id": novel_id,
        "entity_id": entity_id,
        "payload": {"id": entity_id} | payload,
        "at": NOW - timedelta(minutes=5 - seq),
    }


def populate_v1(engine: Engine) -> None:
    """Bring `engine` to revision 0001 and fill it with every v1 state, live claims included.

    Novels: NV1 due later, NV2 never imported with a requested sync, NV3 due and mid-sync under a
    live claim. NV1's chapters: CH1-2 downloaded, CH3 pending, CH4 an auto-queued release
    downloading under a claim, CH5 pending under an unstarted claim, CH6 error, CH7 not
    requested, CH8 pending, CH9 downloading and CH10 downloaded without a position. NV3's: CH11
    downloaded, CH12 pending. Exports of NV1: EX1 (1-2) buildable, EX2 (1-4) waiting for CH3-4,
    EX3 (2-7) `skip_missing`, EX4 (1-1) building under a claim, EX5 (2-2) ready, EX6 (1-1) failed,
    EX7 (6-7) waiting for an error and a not requested chapter; EX8 (NV3 1-2) waiting for CH12.
    The event log holds seqs 1-4, CH6's carrying its error.
    """
    db.migrate(engine)
    _downgrade(engine, "0001")
    claim = {"claimed_by": "w", "claimed_until": LATER}
    with db.write(engine) as connection:
        insert_host(connection)
        connection.execute(
            insert(_V1_NOVELS),
            [
                row | {"created_at": NOW - timedelta(days=2)}
                for row in (
                    _v1_novel(
                        1, LATER, title="Lantern", language="en", last_refreshed_at=REFRESHED
                    ),
                    _v1_novel(2, ASKED, sync_requested=True),
                    _v1_novel(3, DUE, title="Ember", last_refreshed_at=NOW - timedelta(days=1))
                    | claim,
                )
            ],
        )
        connection.execute(
            insert(_V1_CHAPTERS),
            [
                _v1_chapter(1, 1, "downloaded", title="One"),
                _v1_chapter(2, 2, "downloaded", title="Two"),
                _v1_chapter(3, 3, "pending"),
                _v1_chapter(4, 4, "downloading", priority=True, released_at=NOW),
                _v1_chapter(5, 5, "pending", **claim),
                _v1_chapter(6, 6, "error"),
                _v1_chapter(7, 7, "not_requested"),
                _v1_chapter(8, None, "pending"),
                _v1_chapter(9, None, "downloading"),
                _v1_chapter(10, None, "downloaded", title="Ten"),
                _v3_chapter(11, 1, "downloaded", title="Eleven"),
                _v3_chapter(12, 2, "pending"),
            ],
        )
        connection.execute(
            insert(_CONTENTS),
            [
                {"chapter_id": _ch(n), "markdown": f"Chapter {n}", "fetched_at": NOW}
                for n in (1, 2, 10, 11)
            ],
        )
        ready = {"storage_key": "exports/EX5.epub", "size_bytes": 1, "ready_at": NOW}
        exports = [
            _v1_export(1, 1, 2),
            _v1_export(2, 1, 4),
            _v1_export(3, 2, 7, skip_missing=True),
            _v1_export(4, 1, 1, state="building", **claim),
            _v1_export(5, 2, 2, state="ready", **ready),
            _v1_export(6, 1, 1, state="failed", error=RANGE_CHANGED),
            _v1_export(7, 6, 7),
            _v1_export(8, 11, 12, novel_id=NOVEL_3),
        ]
        connection.execute(
            insert(_V1_EXPORTS),
            [row | {"created_at": row["created_at"] - timedelta(hours=1)} for row in exports],
        )
        connection.execute(
            insert(events),
            [
                _v1_event(1, NOVEL_1, NOVEL_1, followed=True, released=[]),
                _v1_event(
                    2, _ch(6), NOVEL_1, state="error", error="HTTP 500", error_at=format_time(NOW)
                ),
                _v1_event(3, "EX000000000006", NOVEL_1, state="failed", error=RANGE_CHANGED),
                _v1_event(4, NOVEL_3, NOVEL_3, followed=True, released=[]),
            ],
        )
        connection.execute(update(event_clock).values(seq=V1_SEQ))


@pytest.fixture
def v1_populated_engine(empty_engine: Engine) -> Engine:
    """A database at revision 0001 holding a realistic mix of every v1 state (`populate_v1`)."""
    populate_v1(empty_engine)
    return empty_engine


def views_of(repo: Any) -> dict[str, Any]:
    """Every novel, positioned chapter and export as the API shows it, by id.

    Duck-typed on the repository so the same function reads v1's repository for `V1_VIEWS`.
    """
    found: dict[str, Any] = {}
    for novel in repo.novels.list():
        found[novel.id] = novel.to_json()
        for chapter in repo.chapters.list(novel.id)[0]:
            found[chapter.id] = chapter.to_json()
        for export in repo.exports.list(novel.id):
            found[export.id] = export.to_json()
    return found


def _repo_at_now(engine: Engine) -> Repo:
    return Repo(engine.url.render_as_string(hide_password=False), clock=FakeClock())


def _all_tasks(engine: Engine) -> list[Any]:
    with db.read(engine) as connection:
        return list(connection.execute(select(tasks).order_by(tasks.c.id)).all())


class TestMigration0002__FromAPopulatedV1Database:
    @pytest.fixture
    def upgraded(self, v1_populated_engine: Engine) -> tuple[Engine, datetime, datetime]:
        start = datetime.now(UTC)
        db.migrate(v1_populated_engine)
        return v1_populated_engine, start, datetime.now(UTC)

    def test_upgrade__gives_every_novel_exactly_one_queued_refresh_task(
        self, upgraded: tuple[Engine, datetime, datetime]
    ) -> None:
        engine, _, _ = upgraded

        refreshes = [t for t in _all_tasks(engine) if t.kind == "novel_refresh"]

        assert sorted((t.entity_id, t.state, t.run_after) for t in refreshes) == [
            (NOVEL_1, "queued", LATER), (NOVEL_2, "queued", ASKED), (NOVEL_3, "queued", DUE),
        ]  # fmt: skip

    def test_upgrade__gives_positioned_pending_and_downloading_chapters_one_queued_download(
        self, upgraded: tuple[Engine, datetime, datetime]
    ) -> None:
        engine, _, _ = upgraded

        downloads = [t for t in _all_tasks(engine) if t.kind == "chapter_download"]

        assert sorted(t.entity_id for t in downloads) == sorted(LIVE_DOWNLOADS)
        assert {t.state for t in downloads} == {"queued"}

    def test_upgrade__gives_buildable_waiting_exports_one_queued_build(
        self, upgraded: tuple[Engine, datetime, datetime]
    ) -> None:
        engine, _, _ = upgraded

        builds = [t for t in _all_tasks(engine) if t.kind == "export_build"]

        assert sorted(t.entity_id for t in builds) == sorted(LIVE_BUILDS)
        assert {t.state for t in builds} == {"queued"}

    def test_upgrade__leaves_every_migrated_task_silent_and_unclaimed(
        self, upgraded: tuple[Engine, datetime, datetime]
    ) -> None:
        engine, _, _ = upgraded

        found = {(t.emit_event, t.claimed_by, t.claimed_until) for t in _all_tasks(engine)}

        assert found == {(False, None, None)}

    def test_upgrade__stamps_every_migrated_task_with_the_one_migration_time(
        self, upgraded: tuple[Engine, datetime, datetime]
    ) -> None:
        engine, start, end = upgraded

        [updated_at] = {t.updated_at for t in _all_tasks(engine)}

        assert start <= updated_at <= end

    def test_upgrade__stamps_every_migrated_task_with_one_new_clock_value_and_no_event_row(
        self, upgraded: tuple[Engine, datetime, datetime]
    ) -> None:
        engine, _, _ = upgraded

        with db.read(engine) as connection:
            clock = connection.execute(select(event_clock.c.seq)).scalar_one()
            seqs = list(connection.execute(select(events.c.seq).order_by(events.c.seq)).scalars())
        assert {t.change_seq for t in _all_tasks(engine)} == {V1_SEQ + 1}
        assert (clock, seqs) == (V1_SEQ + 1, [1, 2, 3, 4])

    def test_upgrade__keeps_chapter_queued_at_and_novel_next_refresh_at(
        self, v1_populated_engine: Engine
    ) -> None:
        with db.read(v1_populated_engine) as connection:
            queued = dict(
                connection.execute(
                    select(_V1_CHAPTERS.c.id, _V1_CHAPTERS.c.queued_at).where(
                        _V1_CHAPTERS.c.position.is_not(None)
                    )
                ).all()
            )
            due = dict(
                connection.execute(select(_V1_NOVELS.c.id, _V1_NOVELS.c.next_refresh_at)).all()
            )

        db.migrate(v1_populated_engine)

        assert _queued_at(v1_populated_engine, sorted(queued)) == queued
        assert _next_refresh_at(v1_populated_engine) == due

    def test_upgrade__keeps_every_api_object_except_work_that_was_running(
        self, upgraded: tuple[Engine, datetime, datetime]
    ) -> None:
        engine, _, _ = upgraded
        expected = json.loads(V1_VIEWS.read_text())
        # Dropped claims: the running download, build and sync start over (tasks spec 9).
        expected[_ch(4)]["state"] = "pending"
        expected["EX000000000004"]["state"] = "waiting"
        expected[NOVEL_3]["syncing"] = False

        repo = _repo_at_now(engine)
        try:
            assert views_of(repo) == expected
        finally:
            repo.engine.dispose()


async def _replay(repo: Repo, last_event_id: str) -> list[str]:
    """The frames one SSE connection with `last_event_id` gets before its first poll wait."""
    stopped = False

    async def run(function: Any, *args: Any, **kwargs: Any) -> Any:
        return function(*args, **kwargs)

    async def sleep(_seconds: float) -> None:
        nonlocal stopped
        stopped = True

    frames = stream(
        repo,
        last_event_id=last_event_id,
        novel_id=None,
        poll_s=1.0,
        keepalive_s=60.0,
        should_stop=lambda: stopped,
        sleep=sleep,
        run=run,
    )
    return [frame async for frame in frames]


class TestMigration0002__AV1ClientResumingTheEventStream:
    def test_stream__replays_after_the_pre_migration_seq_without_resync(
        self, v1_populated_engine: Engine
    ) -> None:
        db.migrate(v1_populated_engine)
        repo = _repo_at_now(v1_populated_engine)
        try:
            repo.chapters.patch(NOVEL_1, ChapterState.PENDING, [7])
            after = [row.seq for row in repo.events.since(V1_SEQ)]

            frames = asyncio.run(_replay(repo, str(V1_SEQ)))
        finally:
            repo.engine.dispose()

        assert after
        assert all("event: resync" not in frame for frame in frames)
        assert [int(frame.split("\n")[0].removeprefix("id: ")) for frame in frames] == after


class TocsByNovel(FakeAdapter):
    """Serves each novel's TOC from its own `toc_url`."""

    def __init__(self, tocs: dict[str, list[str]]) -> None:
        super().__init__()
        self.tocs = tocs

    def sync_toc(self, novel_id: str, host: str, toc_url: str) -> TocResult:
        return TocResult("Lantern", "en", None, list(self.tocs[toc_url]), "<html></html>")


def _toc(novel: int, positions: range) -> list[str]:
    return [f"https://example.com/novel/{novel}/{p}" for p in positions]


class TestMigration0002__AWorkerStartedAfterTheUpgrade:
    @pytest.fixture
    def drained(
        self, v1_populated_engine: Engine, tmp_path: Path
    ) -> Iterator[tuple[Repo, list[Any]]]:
        """Upgraded, then one worker steps until idle at `NOW`, as v1's worker would drain it."""
        db.migrate(v1_populated_engine)
        repo = _repo_at_now(v1_populated_engine)
        adapter = TocsByNovel(
            {
                "https://example.com/novel/1": _toc(1, range(1, 8)),
                "https://example.com/novel/2": _toc(2, range(1, 2)),
                "https://example.com/novel/3": _toc(3, range(1, 3)),
            }
        )
        settings = config.load(
            {"NOVELHOG_DATA_DIR": str(tmp_path), "NOVELHOG_WORKER_ID": "worker-a"}
            | {"NOVELHOG_REQUEST_DELAY": "0"}
        )
        storage = LocalStaticStorage(tmp_path / "static", "/static")
        worker = Worker(repo, adapter, FakeFetcher(), storage, {}, settings, stop=threading.Event())
        with capture_logs() as logs:
            for _ in range(100):
                if worker.step() == "idle":
                    break
            else:
                pytest.fail("the worker never went idle")
        try:
            yield repo, logs
        finally:
            repo.engine.dispose()

    def test_worker__downloads_every_queued_chapter(self, drained: tuple[Repo, list[Any]]) -> None:
        repo, _ = drained

        states = {
            chapter.id: chapter.state.value
            for novel in (NOVEL_1, NOVEL_3)
            for chapter in repo.chapters.list(novel)[0]
        }

        assert states == {
            _ch(1): "downloaded", _ch(2): "downloaded", _ch(3): "downloaded",
            _ch(4): "downloaded", _ch(5): "downloaded", _ch(6): "error",
            _ch(7): "not_requested", _ch(11): "downloaded", _ch(12): "downloaded",
        }  # fmt: skip

    def test_worker__builds_every_export_whose_range_is_complete(
        self, drained: tuple[Repo, list[Any]]
    ) -> None:
        repo, _ = drained

        states = {
            export.id: export.state.value
            for novel in (NOVEL_1, NOVEL_3)
            for export in repo.exports.list(novel)
        }

        assert states == {
            f"EX{n:012d}": state
            for n, state in enumerate(["ready"] * 5 + ["failed", "waiting", "ready"], start=1)
        }

    def test_worker__refreshes_the_novels_that_are_due(
        self, drained: tuple[Repo, list[Any]]
    ) -> None:
        repo, _ = drained

        refreshed = {novel.id: novel.last_refreshed_at for novel in repo.novels.list()}

        assert refreshed == {NOVEL_1: REFRESHED, NOVEL_2: NOW, NOVEL_3: NOW}

    def test_worker__leaves_only_future_refreshes_queued(
        self, drained: tuple[Repo, list[Any]]
    ) -> None:
        repo, _ = drained

        live = [t for t in _all_tasks(repo.engine) if t.state in ("queued", "running")]

        assert {(t.kind, t.state) for t in live} == {("novel_refresh", "queued")}
        assert all(t.run_after > NOW for t in live)

    def test_worker__finds_nothing_to_repair(self, drained: tuple[Repo, list[Any]]) -> None:
        _, logs = drained

        assert [line for line in logs if line["event"] == "task_repaired"] == []
        assert [line for line in logs if line.get("log_level") == "error"] == []


_CHAPTER_CHECKS = {
    "ck_chapters_queued": "(state IN ('pending', 'downloading')) = (queued_at IS NOT NULL)",
    "ck_chapters_claim": "(claimed_by IS NULL) = (claimed_until IS NULL)",
    "ck_chapters_claim_state": "claimed_by IS NULL OR state IN ('pending', 'downloading')",
    "ck_chapters_started": "state <> 'downloading' OR claimed_by IS NOT NULL",
}


def _v1_violations(engine: Engine) -> dict[str, int]:
    """Rows breaking each check v1 had on the queue and claim columns, by check name."""
    checks = {
        **_CHAPTER_CHECKS,
        "ck_novels_claim": "(claimed_by IS NULL) = (claimed_until IS NULL)",
        "ck_exports_claim": "(claimed_by IS NULL) = (claimed_until IS NULL)",
        "ck_exports_building": "(state = 'building') = (claimed_by IS NOT NULL)",
    }
    found = {}
    with db.read(engine) as connection:
        for name, condition in checks.items():
            table = name.split("_")[1]
            found[name] = connection.execute(
                text(f"SELECT count(*) FROM {table} WHERE NOT ({condition})")  # noqa: S608
            ).scalar_one()
    return found


def _counts(engine: Engine) -> dict[str, int]:
    with db.read(engine) as connection:
        return {
            table: connection.execute(
                select(sa.func.count()).select_from(sa.table(table))
            ).scalar_one()
            for table in sorted(_tables(engine) - {"alembic_version"})
        }


def _constraints(engine: Engine) -> dict[str, list[str]]:
    """Each table's checks, unique constraints, foreign keys and indexes, in a stable order.

    A SQLite batch rebuild may write them in another order; the order is not a difference.
    """
    found = inspect(engine)
    return {
        table: sorted(
            [
                *(f"check {c['name']}: {c['sqltext']}" for c in found.get_check_constraints(table)),
                *(
                    f"unique {sorted(u['column_names'])}"
                    for u in found.get_unique_constraints(table)
                ),
                *(
                    f"fk {f['constrained_columns']} -> {f['referred_table']}"
                    for f in found.get_foreign_keys(table)
                ),
                *(
                    f"index {i['name']} {i['column_names']} {i.get('unique')} "
                    f"{ {k: str(v) for k, v in i.get('dialect_options', {}).items()} }"
                    for i in found.get_indexes(table)
                ),
            ]
        )
        for table in sorted(_tables(engine))
    }


class TestMigration0002__DowngradeFromAPopulatedDatabase:
    @pytest.fixture
    def downgraded(self, v1_populated_engine: Engine) -> Engine:
        """Upgraded; then CH7 queued by a user, NV1's sync requested (a TK event), NV2's sync,
        CH3's download and EX1's build running, CH5's task gone; then downgraded."""
        db.migrate(v1_populated_engine)
        repo = _repo_at_now(v1_populated_engine)
        try:
            repo.chapters.patch(NOVEL_1, ChapterState.PENDING, [7])
            repo.novels.request_sync(NOVEL_1)
        finally:
            repo.engine.dispose()
        running = {"state": "running", "claimed_by": "w", "claimed_until": LATER, "started_at": NOW}
        with db.write(v1_populated_engine) as connection:
            assert connection.execute(
                select(sa.func.count()).where(sa.func.substr(events.c.entity_id, 1, 2) == "TK")
            ).scalar_one()
            for entity in (NOVEL_2, _ch(3), "EX000000000001"):
                connection.execute(update(tasks).where(tasks.c.entity_id == entity).values(running))
            connection.execute(
                update(chapters).where(chapters.c.id == _ch(3)).values(state="downloading")
            )
            connection.execute(
                update(_V1_EXPORTS)
                .where(_V1_EXPORTS.c.id == "EX000000000001")
                .values(state="building")
            )
            connection.execute(delete(tasks).where(tasks.c.entity_id == _ch(5)))
        _downgrade(v1_populated_engine, "0001")
        return v1_populated_engine

    def test_downgrade__restores_next_refresh_at_and_no_sync_requests(
        self, downgraded: Engine
    ) -> None:
        with db.read(downgraded) as connection:
            rows = connection.execute(
                select(_V1_NOVELS.c.id, _V1_NOVELS.c.next_refresh_at, _V1_NOVELS.c.sync_requested)
            ).all()

        assert sorted(rows) == [
            (NOVEL_1, NOW, False),
            (NOVEL_2, ASKED, False),
            (NOVEL_3, DUE, False),
        ]

    def test_downgrade__restores_queued_at_and_priority_of_pending_chapters(
        self, downgraded: Engine
    ) -> None:
        with db.read(downgraded) as connection:
            rows = connection.execute(
                select(
                    _V1_CHAPTERS.c.id,
                    _V1_CHAPTERS.c.state,
                    _V1_CHAPTERS.c.queued_at,
                    _V1_CHAPTERS.c.priority,
                ).where(_V1_CHAPTERS.c.state == "pending")
            ).all()

        assert sorted(rows) == [
            (_ch(3), "pending", NOW - timedelta(minutes=3), False),
            (_ch(4), "pending", NOW - timedelta(minutes=4), True),
            (_ch(5), "pending", NOW, False),  # no live task: its updated_at
            (_ch(7), "pending", NOW, False),
            (_ch(12), "pending", NOW - timedelta(minutes=12), False),
        ]

    def test_downgrade__resets_running_work_and_restores_no_claims(
        self, downgraded: Engine
    ) -> None:
        with db.read(downgraded) as connection:
            claims = {
                table.name: set(
                    connection.execute(select(table.c.claimed_by, table.c.claimed_until)).all()
                )
                for table in (_V1_NOVELS, _V1_CHAPTERS, _V1_EXPORTS)
            }
            states = {
                *connection.execute(select(_V1_CHAPTERS.c.state)).scalars(),
                *connection.execute(select(_V1_EXPORTS.c.state)).scalars(),
            }

        assert claims == dict.fromkeys(("novels", "chapters", "exports"), {(None, None)})
        assert states.isdisjoint({"downloading", "building"})

    def test_downgrade__deletes_the_task_events(self, downgraded: Engine) -> None:
        with db.read(downgraded) as connection:
            prefixes = set(
                connection.execute(select(sa.func.substr(events.c.entity_id, 1, 2))).scalars()
            )

        assert "TK" not in prefixes

    def test_downgrade__passes_v1s_checks(self, downgraded: Engine) -> None:
        assert set(_v1_violations(downgraded).values()) == {0}

    def test_downgrade__leaves_autogenerate_clean_against_0001(
        self, downgraded: Engine, reference_database_url: str
    ) -> None:
        reference = db.create_engine(reference_database_url)
        try:
            db.migrate(reference)
            _downgrade(reference, "0001")
            v1_metadata = sa.MetaData()
            v1_metadata.reflect(reference)
            v1_metadata.remove(v1_metadata.tables["alembic_version"])

            with downgraded.connect() as connection:
                context = MigrationContext.configure(connection, opts={"compare_type": True})
                assert compare_metadata(context, v1_metadata) == []
            assert _constraints(downgraded) == _constraints(reference)
        finally:
            reference.dispose()


class TestMigration0002__UpgradeDowngradeUpgrade:
    def test_upgrade__again_after_a_downgrade_gives_the_same_row_counts(
        self, v1_populated_engine: Engine
    ) -> None:
        db.migrate(v1_populated_engine)
        first = _counts(v1_populated_engine)
        _downgrade(v1_populated_engine, "0001")

        db.migrate(v1_populated_engine)

        assert _counts(v1_populated_engine) == first
        assert first["tasks"] == 3 + len(LIVE_DOWNLOADS) + len(LIVE_BUILDS)


class TestRequireCurrent:
    def test_require_current__returns_on_a_database_at_head(self, empty_engine: Engine) -> None:
        db.migrate(empty_engine)

        state = db.revisions(empty_engine)
        db.require_current(empty_engine)

        assert state.is_current

    def test_require_current__raises_on_an_empty_database(self, empty_engine: Engine) -> None:
        with pytest.raises(SchemaOutdated) as raised:
            db.require_current(empty_engine)

        head = db.revisions(empty_engine).head
        assert (raised.value.current, raised.value.head) == (None, head)
        assert str(raised.value) == (
            f"Database schema is at none, this build needs {head}; run `novelhog db migrate`"
        )

    def test_require_current__raises_on_a_database_newer_than_the_build(
        self, empty_engine: Engine
    ) -> None:
        db.migrate(empty_engine)
        with db.write(empty_engine) as connection:
            connection.execute(text("UPDATE alembic_version SET version_num = '9999'"))

        with pytest.raises(SchemaOutdated, match="at 9999, this build needs 0003"):
            db.require_current(empty_engine)

    def test_revisions__reports_an_older_database_against_a_newer_build(
        self, empty_engine: Engine, tmp_path: Path
    ) -> None:
        db.migrate(empty_engine)

        state = db.revisions(empty_engine, script_location=_script_location(tmp_path))

        assert (state.current, state.head, state.is_current) == ("0003", "0004", False)

    def test_require_current__raises_on_an_older_database(
        self, empty_engine: Engine, tmp_path: Path
    ) -> None:
        db.migrate(empty_engine)

        with pytest.raises(SchemaOutdated, match="at 0003, this build needs 0004"):
            db.require_current(empty_engine, script_location=_script_location(tmp_path))


class TestMigrate__OnSqlite:
    def test_migrate__starts_its_transaction_with_begin_immediate(
        self, empty_engine: Engine
    ) -> None:
        if empty_engine.dialect.name != "sqlite":
            pytest.skip("SQLite transaction mode")
        statements: list[str] = []

        @event.listens_for(empty_engine, "before_cursor_execute")
        def record(_c: Any, _cur: Any, statement: str, *_rest: Any) -> None:
            statements.append(statement)

        db.migrate(empty_engine)

        assert "BEGIN IMMEDIATE" in statements
        assert "BEGIN" not in statements


def _definitions(engine: Engine) -> dict[str, str]:
    """Constraint and index definitions as the database itself reports them."""
    with db.read(engine) as connection:
        if engine.dialect.name == "sqlite":
            rows = connection.execute(
                text("SELECT name, sql FROM sqlite_master WHERE name NOT LIKE '%alembic%'")
            ).all()
        else:
            rows = connection.execute(
                text(
                    "SELECT conrelid::regclass::text || '.' || conname, pg_get_constraintdef(oid) "
                    "FROM pg_constraint WHERE connamespace = 'public'::regnamespace "
                    "AND conrelid::regclass::text <> 'alembic_version' "
                    "UNION ALL SELECT indexname, indexdef FROM pg_indexes "
                    "WHERE schemaname = 'public' AND tablename <> 'alembic_version'"
                )
            ).all()
    # SQLite quotes a table name after a batch rebuild; the quoting is not a difference.
    return {name: re.sub(r"\s+", " ", (sql or "").replace('"', "")) for name, sql in rows}


class TestMigrate__AgainstCreateAll:
    def test_migrate__matches_create_all_checks_and_index_definitions(
        self, empty_engine: Engine, reference_database_url: str
    ) -> None:
        db.migrate(empty_engine)
        reference = db.create_engine(reference_database_url)
        metadata.create_all(reference)

        try:
            migrated = _definitions(empty_engine)
            assert migrated == _definitions(reference)
            assert any("WHERE" in sql for sql in migrated.values())
            assert any("CHECK" in sql.upper() for sql in migrated.values())
        finally:
            reference.dispose()
