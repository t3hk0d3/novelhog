"""The storage trash repository (spec section 4)."""

import threading
from datetime import timedelta

from sqlalchemy import delete, select, update

from novelhog.repo import Repo, db, trash
from novelhog.repo.novels import lock_novel
from novelhog.repo.schema import exports, novels, storage_trash
from novelhog.repo.trash import TRASH_GRACE_S
from tests.conftest import FakeClock
from tests.integration.helpers import (
    NOVEL_ID,
    NOW,
    insert_export,
    insert_novel_with_chapter,
    trash_keys,
)

LATER = timedelta(days=1)


def put(repo: Repo, keys: list[str]) -> None:
    with db.write(repo.engine) as conn:
        trash.add(conn, keys, repo.clock())


class TestTrash:
    def test_take__returns_at_most_limit_keys_oldest_first(
        self, repo: Repo, clock: FakeClock
    ) -> None:
        put(repo, ["b"])
        clock.advance(seconds=1)
        put(repo, ["a"])
        clock.advance(seconds=1)
        put(repo, ["c"])
        clock.advance(seconds=TRASH_GRACE_S)
        assert repo.trash.take(2) == ["b", "a"]
        assert repo.trash.take() == ["b", "a", "c"]

    def test_remove__deletes_the_keys(self, repo: Repo) -> None:
        put(repo, ["a", "b", "c"])
        repo.trash.remove(["a", "c", "missing"])
        assert trash_keys(repo.engine) == ["b"]

    def test_add__existing_key_is_a_no_op(self, repo: Repo, clock: FakeClock) -> None:
        put(repo, ["a"])
        clock.advance(seconds=5)
        put(repo, ["b", "a", "b"])
        assert trash_keys(repo.engine) == ["a", "b"]


class TestTake__GraceAge:
    def test_take__a_fresh_row_is_not_returned(self, repo: Repo, clock: FakeClock) -> None:
        put(repo, ["a"])
        clock.advance(seconds=TRASH_GRACE_S - 1)
        assert repo.trash.take() == []
        assert trash_keys(repo.engine) == ["a"]

    def test_take__a_row_older_than_the_grace_is_returned(
        self, repo: Repo, clock: FakeClock
    ) -> None:
        put(repo, ["a"])
        clock.advance(seconds=TRASH_GRACE_S)
        put(repo, ["b"])
        assert repo.trash.take() == ["a"]
        clock.advance(seconds=TRASH_GRACE_S)
        assert repo.trash.take() == ["a", "b"]


class TestDiscard:
    def test_discard__trashes_the_keys_in_its_own_transaction(
        self, repo: Repo, clock: FakeClock
    ) -> None:
        repo.trash.discard(["b", "a", "b"])
        assert trash_keys(repo.engine) == ["a", "b"]

    def test_discard__existing_key_and_empty_list_are_no_ops(self, repo: Repo) -> None:
        put(repo, ["a"])
        repo.trash.discard(["a"])
        repo.trash.discard([])
        assert trash_keys(repo.engine) == ["a"]


class TestDiscard__ReferencedKeys:
    def test_discard__skips_a_key_a_ready_export_references(self, repo: Repo) -> None:
        with db.write(repo.engine) as conn:
            insert_export(conn, state="ready", storage_key="exports/a", size_bytes=1, ready_at=NOW)

        repo.trash.discard(["exports/a", "other"])

        assert trash_keys(repo.engine) == ["other"]

    def test_discard__skips_a_key_a_novel_cover_references(self, repo: Repo) -> None:
        with db.write(repo.engine) as conn:
            insert_novel_with_chapter(conn)
            conn.execute(update(novels).values(cover_key="covers/a.png"))

        repo.trash.discard(["covers/a.png", "other"])

        assert trash_keys(repo.engine) == ["other"]


class TestTake__ReferencedKeys:
    def test_take__skips_and_cleans_up_a_key_a_ready_export_references(
        self, repo: Repo, clock: FakeClock
    ) -> None:
        put(repo, ["exports/a", "other"])  # the race already left a row for a live key
        with db.write(repo.engine) as conn:
            insert_export(conn, state="ready", storage_key="exports/a", size_bytes=1, ready_at=NOW)

        clock.advance(seconds=TRASH_GRACE_S)
        assert repo.trash.take() == ["other"]
        with db.read(repo.engine) as conn:
            remaining = conn.execute(select(storage_trash.c.key)).scalars().all()
        assert sorted(remaining) == ["other"]

    def test_take__skips_and_cleans_up_a_key_a_novel_cover_references(
        self, repo: Repo, clock: FakeClock
    ) -> None:
        put(repo, ["covers/a.png", "other"])
        with db.write(repo.engine) as conn:
            insert_novel_with_chapter(conn)
            conn.execute(update(novels).values(cover_key="covers/a.png"))

        clock.advance(seconds=TRASH_GRACE_S)
        assert repo.trash.take() == ["other"]
        assert trash_keys(repo.engine) == ["other"]


class TestDiscard__RacingFinish:
    def test_discard__waits_for_a_finish_in_flight_and_then_skips_the_key(self, repo: Repo) -> None:
        key = f"exports/{NOVEL_ID}/EX000000000001/n.epub"
        with db.write(repo.engine) as conn:
            insert_export(conn, state="building", claimed_by="b", claimed_until=NOW + LATER)
        started = threading.Event()
        errors: list[BaseException] = []

        def old_owner() -> None:
            started.set()
            try:
                repo.trash.discard([key])
            except BaseException as error:  # noqa: BLE001 - surfaced by the assertion below
                errors.append(error)

        thread = threading.Thread(target=old_owner, daemon=True)
        # The new owner's finish: novel lock, export ready, trash row removed - uncommitted.
        with db.write(repo.engine) as conn:
            assert lock_novel(conn, NOVEL_ID)
            conn.execute(
                update(exports).values(
                    state="ready",
                    storage_key=key,
                    size_bytes=1,
                    ready_at=NOW,
                    claimed_by=None,
                    claimed_until=None,
                )
            )
            conn.execute(delete(storage_trash).where(storage_trash.c.key == key))
            thread.start()
            started.wait(5)
            thread.join(0.3)  # without the novel lock the discard would insert its row now
        thread.join(10)

        assert errors == []
        with db.read(repo.engine) as conn:  # the table itself, not `take`, which self-heals
            assert conn.execute(select(storage_trash.c.key)).first() is None

    def test_discard__trashes_keys_of_other_shapes_and_of_deleted_novels(self, repo: Repo) -> None:
        repo.trash.discard(["covers/x/a.png", "exports/NVgone00000000/EX000000000001/n.epub"])
        assert len(trash_keys(repo.engine)) == 2
