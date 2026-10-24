"""Migrations: initial schema, idempotence, working directory, SQLite batch rebuilds."""

import re
import shutil
import textwrap
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

from novelhog.repo import Repo, SchemaOutdated, db
from novelhog.repo.schema import chapters, event_clock, events, metadata, novels, tasks
from novelhog.repo.types import UTCDateTime
from tests.integration.helpers import NOW, chapter_row, insert_host, novel_row


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

    revision = "0003"
    down_revision = "0002"
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
    (location / "versions" / "0003_rebuild_novels.py").write_text(body)
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

        assert head == "0003"
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
        assert state.current == "0002"
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


_V1_NOVELS = sa.table(
    "novels",
    sa.column("id", sa.Text),
    sa.column("host", sa.Text),
    sa.column("toc_url", sa.Text),
    sa.column("refresh_interval", sa.Integer),
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
        "refresh_interval": 3600,
        "next_refresh_at": next_refresh_at,
        "sync_requested": False,
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
    sa.column("state", sa.Text),
    sa.column("priority", sa.Boolean),
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
        "state": state,
        "priority": False,
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

        with pytest.raises(SchemaOutdated, match="at 9999, this build needs 0002"):
            db.require_current(empty_engine)

    def test_revisions__reports_an_older_database_against_a_newer_build(
        self, empty_engine: Engine, tmp_path: Path
    ) -> None:
        db.migrate(empty_engine)

        state = db.revisions(empty_engine, script_location=_script_location(tmp_path))

        assert (state.current, state.head, state.is_current) == ("0002", "0003", False)

    def test_require_current__raises_on_an_older_database(
        self, empty_engine: Engine, tmp_path: Path
    ) -> None:
        db.migrate(empty_engine)

        with pytest.raises(SchemaOutdated, match="at 0002, this build needs 0003"):
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
