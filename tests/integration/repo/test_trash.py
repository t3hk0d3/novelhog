"""The storage trash repository (spec section 4)."""

from novelhog.repo import Repo, db, trash
from tests.conftest import FakeClock
from tests.integration.helpers import NOW, insert_export


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
        assert repo.trash.take(2) == ["b", "a"]
        assert repo.trash.take() == ["b", "a", "c"]

    def test_remove__deletes_the_keys(self, repo: Repo) -> None:
        put(repo, ["a", "b", "c"])
        repo.trash.remove(["a", "c", "missing"])
        assert repo.trash.take() == ["b"]

    def test_add__existing_key_is_a_no_op(self, repo: Repo, clock: FakeClock) -> None:
        put(repo, ["a"])
        clock.advance(seconds=5)
        put(repo, ["b", "a", "b"])
        assert repo.trash.take() == ["a", "b"]


class TestDiscard:
    def test_discard__trashes_the_keys_in_its_own_transaction(
        self, repo: Repo, clock: FakeClock
    ) -> None:
        repo.trash.discard(["b", "a", "b"])
        assert repo.trash.take() == ["a", "b"]

    def test_discard__existing_key_and_empty_list_are_no_ops(self, repo: Repo) -> None:
        put(repo, ["a"])
        repo.trash.discard(["a"])
        repo.trash.discard([])
        assert repo.trash.take() == ["a"]


class TestDiscard__ReferencedKeys:
    def test_discard__skips_a_key_a_ready_export_references(self, repo: Repo) -> None:
        with db.write(repo.engine) as conn:
            insert_export(conn, state="ready", storage_key="exports/a", size_bytes=1, ready_at=NOW)

        repo.trash.discard(["exports/a", "other"])

        assert repo.trash.take() == ["other"]
