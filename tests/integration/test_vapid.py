"""Unit tests for novelhog.vapid."""

import base64
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from novelhog import vapid
from novelhog.repo import Repo


def _decode(value: str) -> bytes:
    assert "=" not in value
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


class TestEnsureKeys__OnEmptySettings:
    def test_ensure_keys__creates_pair_with_expected_encodings(self, repo: Repo) -> None:
        keys = vapid.ensure_keys(repo)
        public = _decode(keys.public_key)
        assert len(public) == 65
        assert public[0] == 0x04
        assert len(_decode(keys.private_key)) == 32

    def test_ensure_keys__stores_pair_under_vapid(self, repo: Repo) -> None:
        keys = vapid.ensure_keys(repo)
        assert repo.settings.get("vapid") == {
            "public_key": keys.public_key,
            "private_key": keys.private_key,
        }


class TestEnsureKeys__WhenKeysExist:
    def test_ensure_keys__returns_them_unchanged(self, repo: Repo) -> None:
        first = vapid.ensure_keys(repo)
        assert vapid.ensure_keys(repo) == first


class TestEnsureKeys__WithTwoInstancesStartingTogether:
    def test_ensure_keys__both_end_with_the_same_pair(
        self, repo: Repo, migrated_database: str
    ) -> None:
        other = Repo(migrated_database)
        barrier = Barrier(2)

        def start(target: Repo) -> vapid.VapidKeys:
            barrier.wait()
            return vapid.ensure_keys(target)

        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(start, [repo, other]))
        finally:
            other.engine.dispose()
        assert results[0] == results[1]
        assert vapid.load(repo) == results[0]


class TestLoad:
    def test_load__returns_none_when_there_is_none(self, repo: Repo) -> None:
        assert vapid.load(repo) is None

    def test_load__returns_the_stored_pair(self, repo: Repo) -> None:
        keys = vapid.ensure_keys(repo)
        assert vapid.load(repo) == keys
