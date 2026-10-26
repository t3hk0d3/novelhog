"""The consumers repository: claim, advance, release, renew and recovery."""

from datetime import timedelta

import pytest
from sqlalchemy import select

from novelhog.repo import Repo, db
from novelhog.repo import events as event_log
from novelhog.repo.schema import consumers
from tests.conftest import FakeClock

NOVEL = "NV000000000001"


def _row(repo: Repo, name: str = "notifier"):  # type: ignore[no-untyped-def]
    with db.read(repo.engine) as conn:
        return conn.execute(select(consumers).where(consumers.c.name == name)).one()


def _events(repo: Repo, count: int) -> None:
    for _ in range(count):
        with db.write(repo.engine) as conn:
            event_log.append(conn, NOVEL, NOVEL, {}, now=repo.clock())


class TestClaim:
    def test_claim__creates_a_missing_row_at_the_current_clock(
        self, repo: Repo, clock: FakeClock
    ) -> None:
        _events(repo, 3)
        assert repo.consumers.claim("notifier", "w1", 60) == 3
        row = _row(repo)
        assert (row.last_seq, row.claimed_by, row.claimed_until) == (
            3,
            "w1",
            clock.current + timedelta(seconds=60),
        )

    def test_claim__takes_a_free_row_and_returns_its_last_seq(self, repo: Repo) -> None:
        _events(repo, 2)
        repo.consumers.claim("notifier", "w1", 60)
        _events(repo, 2)
        assert repo.consumers.advance("notifier", "w1", 3)
        assert repo.consumers.claim("notifier", "w2", 60) == 3
        assert _row(repo).claimed_by == "w2"

    def test_claim__returns_none_while_another_owner_holds_it(self, repo: Repo) -> None:
        assert repo.consumers.claim("notifier", "w1", 60) == 0
        assert repo.consumers.claim("notifier", "w2", 60) is None
        assert _row(repo).claimed_by == "w1"

    def test_claim__takes_over_an_expired_claim(self, repo: Repo, clock: FakeClock) -> None:
        repo.consumers.claim("notifier", "w1", 60)
        clock.advance(seconds=61)
        assert repo.consumers.claim("notifier", "w2", 60) == 0
        assert _row(repo).claimed_by == "w2"


class TestAdvance:
    def test_advance__sets_last_seq_and_clears_the_claim(self, repo: Repo) -> None:
        _events(repo, 5)
        repo.consumers.claim("notifier", "w1", 60)
        assert repo.consumers.advance("notifier", "w1", 5)
        row = _row(repo)
        assert (row.last_seq, row.claimed_by, row.claimed_until) == (5, None, None)

    def test_advance__never_moves_backwards(self, repo: Repo) -> None:
        _events(repo, 5)
        repo.consumers.claim("notifier", "w1", 60)
        with pytest.raises(ValueError, match="back"):
            repo.consumers.advance("notifier", "w1", 4)
        row = _row(repo)
        assert (row.last_seq, row.claimed_by) == (5, "w1")

    def test_advance__returns_false_and_changes_nothing_when_the_claim_was_lost(
        self, repo: Repo, clock: FakeClock
    ) -> None:
        repo.consumers.claim("notifier", "w1", 60)
        clock.advance(seconds=61)
        repo.consumers.claim("notifier", "w2", 60)
        assert repo.consumers.advance("notifier", "w1", 9) is False
        row = _row(repo)
        assert (row.last_seq, row.claimed_by) == (0, "w2")


class TestReleaseAndRenew:
    def test_release__clears_an_owned_claim(self, repo: Repo) -> None:
        repo.consumers.claim("notifier", "w1", 60)
        assert repo.consumers.release("notifier", "w1")
        assert _row(repo).claimed_by is None

    def test_release__returns_false_when_not_owned(self, repo: Repo) -> None:
        repo.consumers.claim("notifier", "w1", 60)
        assert repo.consumers.release("notifier", "w2") is False
        assert repo.consumers.release("missing", "w1") is False
        assert _row(repo).claimed_by == "w1"

    def test_renew__extends_an_owned_claim(self, repo: Repo, clock: FakeClock) -> None:
        repo.consumers.claim("notifier", "w1", 60)
        clock.advance(seconds=30)
        assert repo.consumers.renew("notifier", "w1", 60)
        assert _row(repo).claimed_until == clock.current + timedelta(seconds=60)

    def test_renew__returns_false_when_not_owned(self, repo: Repo, clock: FakeClock) -> None:
        repo.consumers.claim("notifier", "w1", 60)
        assert repo.consumers.renew("notifier", "w2", 60) is False
        clock.advance(seconds=61)
        assert repo.consumers.renew("notifier", "w1", 60) is False


class TestRecover:
    def test_recover__frees_consumers_whose_claim_expired(
        self, repo: Repo, clock: FakeClock
    ) -> None:
        repo.consumers.claim("old", "w1", 60)
        repo.consumers.claim("live", "w1", 600)
        clock.advance(seconds=61)
        assert repo.work.recover().consumers_freed == 1
        assert _row(repo, "old").claimed_by is None
        assert _row(repo, "live").claimed_by == "w1"
