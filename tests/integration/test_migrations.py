"""Migrations: initial schema, idempotence, working directory, SQLite batch rebuilds."""

import shutil
import textwrap
from pathlib import Path

import pytest
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import Engine, insert, inspect, select, text

from novelhog.repo import SchemaOutdated, db
from novelhog.repo.schema import chapters, event_clock, metadata, novels
from tests.integration.helpers import chapter_row, insert_host, novel_row


@pytest.fixture
def empty_engine(empty_database_url: str) -> Engine:
    """An engine on an empty, unmigrated database."""
    return db.create_engine(empty_database_url)


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

    revision = "0002"
    down_revision = "0001"
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
    (location / "versions" / "0002_rebuild_novels.py").write_text(body)
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

        assert head == "0002"
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
        assert state.current == "0001"
        with db.read(sqlite_with_data) as connection:
            assert len(connection.execute(select(novels.c.id)).all()) == 1
            assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1


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

        with pytest.raises(SchemaOutdated, match="at 9999, this build needs 0001"):
            db.require_current(empty_engine)
