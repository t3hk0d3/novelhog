"""The subscriptions repository (spec sections 4, 5.6 and 6.6)."""

import threading
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from novelhog import ids
from novelhog.domain.errors import NotFound
from novelhog.repo import Repo, db
from novelhog.repo.schema import events, subscriptions
from tests.conftest import FakeClock

ADDRESS: dict[str, object] = {"endpoint": "https://push.example/a", "keys": {"auth": "x"}}


def _count(repo: Repo, table: object = subscriptions) -> int:
    with db.read(repo.engine) as conn:
        return conn.execute(select(func.count()).select_from(table)).scalar_one()  # type: ignore[arg-type]


class TestUpsert__WithANewKey:
    def test_upsert__inserts_with_an_sb_id_and_returns_created(
        self, repo: Repo, clock: FakeClock
    ) -> None:
        view, created = repo.subscriptions.upsert("webpush", ADDRESS, "k1", "Firefox")
        assert created is True
        assert ids.is_valid(view.id, "SB")
        assert (view.channel, view.label, view.created_at) == ("webpush", "Firefox", clock.current)
        assert _count(repo) == 1

    def test_upsert__appends_no_event(self, repo: Repo) -> None:
        repo.subscriptions.upsert("webpush", ADDRESS, "k1", None)
        assert _count(repo, events) == 0


class TestUpsert__WithAnExistingKey:
    def test_upsert__replaces_address_and_label_keeps_id_and_returns_not_created(
        self, repo: Repo, clock: FakeClock
    ) -> None:
        first, _ = repo.subscriptions.upsert("webpush", ADDRESS, "k1", "old")
        clock.current += timedelta(hours=1)
        new_address: dict[str, object] = {
            "endpoint": "https://push.example/a",
            "keys": {"auth": "y"},
        }
        second, created = repo.subscriptions.upsert("webpush", new_address, "k1", "new")
        assert created is False
        assert second.id == first.id
        assert second.label == "new"
        assert second.created_at == first.created_at
        (stored,) = repo.subscriptions.all_with_address()
        assert (stored.address, stored.label) == (new_address, "new")

    def test_upsert__same_key_on_another_channel_is_a_new_row(self, repo: Repo) -> None:
        repo.subscriptions.upsert("webpush", ADDRESS, "k1", None)
        _, created = repo.subscriptions.upsert("telegram", {"chat": 1}, "k1", None)
        assert created is True
        assert _count(repo) == 2

    def test_upsert__concurrent_calls_on_two_repos_end_in_one_row_and_one_created(
        self, repo: Repo, migrated_database: str, clock: FakeClock
    ) -> None:
        other = Repo(migrated_database, clock=clock)
        try:
            barrier = threading.Barrier(2)
            results: list[tuple[str, bool]] = []
            errors: list[BaseException] = []

            def worker(target: Repo) -> None:
                try:
                    barrier.wait(timeout=30)
                    view, created = target.subscriptions.upsert("webpush", ADDRESS, "k1", None)
                    results.append((view.id, created))
                except BaseException as error:  # noqa: BLE001  (checked in the main thread)
                    errors.append(error)

            threads = [threading.Thread(target=worker, args=(r,)) for r in (repo, other)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=60)
            assert errors == []
            assert _count(repo) == 1
            assert sorted(created for _, created in results) == [False, True]
            assert len({sub_id for sub_id, _ in results}) == 1
        finally:
            other.engine.dispose()


class TestList:
    def test_list__returns_views_by_created_at_without_addresses(
        self, repo: Repo, clock: FakeClock
    ) -> None:
        later, _ = repo.subscriptions.upsert("webpush", ADDRESS, "late", "b")
        clock.current -= timedelta(hours=1)
        earlier, _ = repo.subscriptions.upsert("telegram", {"chat": 1}, "early", None)
        listed = repo.subscriptions.list()
        assert listed == [earlier, later]
        assert not any(hasattr(item, "address") for item in listed)
        assert set(listed[0].to_json()) == {"id", "channel", "label", "created_at"}


class TestAllWithAddress:
    def test_all_with_address__returns_every_subscription_with_its_address(
        self, repo: Repo, clock: FakeClock
    ) -> None:
        a, _ = repo.subscriptions.upsert("webpush", ADDRESS, "k1", "x")
        clock.current += timedelta(seconds=1)
        b, _ = repo.subscriptions.upsert("telegram", {"chat": 7}, "k2", None)
        found = repo.subscriptions.all_with_address()
        assert [(s.id, s.channel, s.address, s.address_key, s.label) for s in found] == [
            (a.id, "webpush", ADDRESS, "k1", "x"),
            (b.id, "telegram", {"chat": 7}, "k2", None),
        ]


class TestDelete:
    def test_delete__removes_the_row(self, repo: Repo) -> None:
        view, _ = repo.subscriptions.upsert("webpush", ADDRESS, "k1", None)
        repo.subscriptions.delete(view.id)
        assert _count(repo) == 0

    @pytest.mark.parametrize("bad", ["SBabcdefghij12", "nope", "NVabcdefghij12", "", "SB"])
    def test_delete__unknown_or_malformed_id_raises_not_found(self, repo: Repo, bad: str) -> None:
        with pytest.raises(NotFound):
            repo.subscriptions.delete(bad)
