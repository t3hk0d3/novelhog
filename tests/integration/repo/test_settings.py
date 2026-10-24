"""The settings repository (spec section 4)."""

import threading

from novelhog.repo import Repo


class TestGet:
    def test_get__returns_the_stored_dict_or_none(self, repo: Repo) -> None:
        assert repo.settings.get("vapid") is None
        repo.settings.put_if_absent("vapid", {"public_key": "p"})
        assert repo.settings.get("vapid") == {"public_key": "p"}


class TestPutIfAbsent:
    def test_put_if_absent__stores_and_returns_true_when_missing(self, repo: Repo) -> None:
        assert repo.settings.put_if_absent("k", {"a": 1}) is True
        assert repo.settings.get("k") == {"a": 1}

    def test_put_if_absent__keeps_the_value_and_returns_false_when_present(
        self, repo: Repo
    ) -> None:
        repo.settings.put_if_absent("k", {"a": 1})
        assert repo.settings.put_if_absent("k", {"a": 2}) is False
        assert repo.settings.get("k") == {"a": 1}

    def test_put_if_absent__exactly_one_of_two_racing_connections_wins(self, repo: Repo) -> None:
        barrier = threading.Barrier(2)
        results: list[bool] = []

        def racer(n: int) -> None:
            barrier.wait(timeout=30)
            results.append(repo.settings.put_if_absent("k", {"n": n}))

        threads = [threading.Thread(target=racer, args=(n,)) for n in (1, 2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        assert sorted(results) == [False, True]
