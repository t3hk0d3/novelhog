"""Spot checks of the spec section 4 constraints, on SQLite and Postgres."""

from typing import Any

import pytest
from sqlalchemy import insert, select
from sqlalchemy.exc import IntegrityError

from novelhog.repo import Repo, db
from novelhog.repo.schema import chapters, consumers, exports, novels, settings
from tests.integration.helpers import (
    NOVEL_ID,
    NOW,
    chapter_row,
    export_row,
    insert_export,
    insert_host,
    insert_novel_with_chapter,
    novel_row,
)


def _expect_rejected(repo: Repo, setup: Any, *statements: Any) -> None:
    """Run `setup(connection)` and `statements`; the last statement must violate a constraint."""
    with pytest.raises(IntegrityError), db.write(repo.engine) as connection:
        if setup is not None:
            setup(connection)
        for statement in statements:
            connection.execute(statement)


def _insert_host_and_novel(connection: Any) -> None:
    insert_host(connection)
    connection.execute(insert(novels).values(**novel_row()))


class TestClaimColumns:
    @pytest.mark.parametrize(
        ("table", "row", "claim"),
        [
            (novels, novel_row(), {"claimed_by": "w1"}),
            (novels, novel_row(), {"claimed_until": NOW}),
            (chapters, chapter_row(state="pending", queued_at=NOW), {"claimed_by": "w1"}),
            (chapters, chapter_row(state="pending", queued_at=NOW), {"claimed_until": NOW}),
            (exports, export_row(), {"claimed_by": "w1", "state": "building"}),
            (exports, export_row(), {"claimed_until": NOW}),
            (
                consumers,
                {"name": "notifier", "last_seq": 0, "updated_at": NOW},
                {"claimed_by": "w"},
            ),
            (
                consumers,
                {"name": "notifier", "last_seq": 0, "updated_at": NOW},
                {"claimed_until": NOW},
            ),
        ],
    )
    def test_claim_columns__reject_a_row_with_only_one_set(
        self, repo: Repo, table: Any, row: dict[str, Any], claim: dict[str, Any]
    ) -> None:
        setups = {
            novels: insert_host,
            chapters: _insert_host_and_novel,
            exports: insert_novel_with_chapter,
            consumers: None,
        }
        _expect_rejected(repo, setups[table], insert(table).values(**(row | claim)))


class TestSchemaConstraints:
    def test_schema__novel_id_without_nv_prefix_is_rejected(self, repo: Repo) -> None:
        _expect_rejected(repo, insert_host, insert(novels).values(**novel_row(id="XX000000000001")))

    def test_schema__chapter_id_with_wrong_length_is_rejected(self, repo: Repo) -> None:
        def setup(connection: Any) -> None:
            insert_host(connection)
            connection.execute(insert(novels).values(**novel_row()))

        _expect_rejected(repo, setup, insert(chapters).values(**chapter_row(id="CH1")))

    @pytest.mark.parametrize("state", ["pending", "downloading"])
    def test_schema__queued_state_without_queued_at_is_rejected(
        self, repo: Repo, state: str
    ) -> None:
        _expect_rejected(
            repo,
            insert_novel_with_chapter,
            insert(chapters).values(
                **chapter_row(
                    id="CH000000000002",
                    url="u2",
                    position=2,
                    state=state,
                    claimed_by="w" if state == "downloading" else None,
                    claimed_until=NOW if state == "downloading" else None,
                )
            ),
        )

    def test_schema__downloading_chapter_without_claim_is_rejected(self, repo: Repo) -> None:
        _expect_rejected(
            repo,
            insert_novel_with_chapter,
            insert(chapters).values(
                **chapter_row(id="CH000000000002", url="u2", state="downloading", queued_at=NOW)
            ),
        )

    @pytest.mark.parametrize("state", ["not_requested", "downloaded", "error"])
    def test_schema__claimed_chapter_in_a_resting_state_is_rejected(
        self, repo: Repo, state: str
    ) -> None:
        _expect_rejected(
            repo,
            insert_novel_with_chapter,
            insert(chapters).values(
                **chapter_row(
                    id="CH000000000002",
                    url="u2",
                    state=state,
                    claimed_by="w",
                    claimed_until=NOW,
                )
            ),
        )

    @pytest.mark.parametrize(
        "fields", [{"position": 2, "removed_at": NOW}, {"position": None, "removed_at": None}]
    )
    def test_schema__chapter_needs_exactly_one_of_position_and_removed_at(
        self, repo: Repo, fields: dict[str, Any]
    ) -> None:
        _expect_rejected(
            repo,
            insert_novel_with_chapter,
            insert(chapters).values(**chapter_row(id="CH000000000002", url="u2", **fields)),
        )

    def test_schema__chapter_position_below_one_is_rejected(self, repo: Repo) -> None:
        _expect_rejected(
            repo,
            insert_novel_with_chapter,
            insert(chapters).values(**chapter_row(id="CH000000000002", url="u2", position=0)),
        )

    def test_schema__two_chapters_of_a_novel_with_the_same_url_are_rejected(
        self, repo: Repo
    ) -> None:
        _expect_rejected(
            repo,
            insert_novel_with_chapter,
            insert(chapters).values(**chapter_row(id="CH000000000002", position=2)),
        )

    def test_schema__ready_export_without_storage_key_is_rejected(self, repo: Repo) -> None:
        with pytest.raises(IntegrityError), db.write(repo.engine) as connection:
            insert_export(connection, state="ready", size_bytes=10, ready_at=NOW)

    def test_schema__second_waiting_export_with_same_key_is_rejected(self, repo: Repo) -> None:
        def setup(connection: Any) -> None:
            insert_export(connection)

        _expect_rejected(repo, setup, insert(exports).values(**export_row(id="EX000000000002")))

    def test_schema__export_with_same_key_is_allowed_once_the_first_is_ready(
        self, repo: Repo
    ) -> None:
        with db.write(repo.engine) as connection:
            insert_export(connection, state="ready", storage_key="k", size_bytes=10, ready_at=NOW)
            connection.execute(insert(exports).values(**export_row(id="EX000000000002")))

        with db.read(repo.engine) as connection:
            assert len(connection.execute(select(exports.c.id)).all()) == 2

    def test_schema__deleting_a_novel_with_chapters_is_rejected_without_cascade(
        self, repo: Repo
    ) -> None:
        with db.write(repo.engine) as connection:
            insert_novel_with_chapter(connection)

        with pytest.raises(IntegrityError), db.write(repo.engine) as connection:
            connection.execute(novels.delete().where(novels.c.id == NOVEL_ID))

    def test_schema__json_columns_round_trip_dicts_unchanged(self, repo: Repo) -> None:
        value = {"public_key": "abc", "nested": {"n": [1, 2, {"x": None}]}, "t": True}
        with db.write(repo.engine) as connection:
            connection.execute(insert(settings).values(key="vapid", value=value, updated_at=NOW))

        with db.read(repo.engine) as connection:
            assert connection.execute(select(settings.c.value)).scalar() == value
