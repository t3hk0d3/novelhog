"""The hosts repository (spec sections 4 and 5.3)."""

import threading
from collections.abc import Iterator
from datetime import datetime, timedelta

import pytest
from sqlalchemy import insert, select, update

from novelhog.domain.hosts import FailureKind
from novelhog.repo import Repo, db
from novelhog.repo.hosts import Host, HostState, PublishReport
from novelhog.repo.schema import hosts, novels
from tests.conftest import FakeClock
from tests.integration.helpers import novel_row


def _stored(repo: Repo) -> dict[str, bool]:
    with db.read(repo.engine) as conn:
        return {r.host: r.supported for r in conn.execute(select(hosts)).all()}


class TestPublish:
    def test_publish__inserts_new_hosts_as_supported_with_updated_at(
        self, repo: Repo, clock: FakeClock
    ) -> None:
        report = repo.hosts.publish(["a.com", "b.com"])
        assert _stored(repo) == {"a.com": True, "b.com": True}
        with db.read(repo.engine) as conn:
            times = {r.updated_at for r in conn.execute(select(hosts)).all()}
        assert times == {clock.current}
        assert report == PublishReport(added=["a.com", "b.com"], unsupported=[])

    def test_publish__marks_missing_hosts_unsupported_without_deleting(self, repo: Repo) -> None:
        repo.hosts.publish(["a.com", "b.com"])
        report = repo.hosts.publish(["a.com"])
        assert _stored(repo) == {"a.com": True, "b.com": False}
        assert report == PublishReport(added=[], unsupported=["b.com"])

    def test_publish__succeeds_when_a_novel_references_a_dropped_host(self, repo: Repo) -> None:
        repo.hosts.publish(["example.com"])
        with db.write(repo.engine) as conn:
            conn.execute(insert(novels).values(**novel_row()))
        repo.hosts.publish(["other.com"])
        assert _stored(repo) == {"example.com": False, "other.com": True}

    def test_publish__marks_a_returning_host_supported_again(self, repo: Repo) -> None:
        repo.hosts.publish(["a.com"])
        repo.hosts.publish(["b.com"])
        report = repo.hosts.publish(["a.com"])
        assert _stored(repo) == {"a.com": True, "b.com": False}
        assert report.added == ["a.com"]

    def test_publish__concurrent_publishers_do_not_fail_and_store_each_host_once(
        self, repo: Repo
    ) -> None:
        barrier = threading.Barrier(4)
        errors: list[BaseException] = []

        def publisher() -> None:
            try:
                barrier.wait(timeout=30)
                repo.hosts.publish(["a.com", "b.com"])
            except BaseException as error:  # noqa: BLE001  (checked in the main thread)
                errors.append(error)

        threads = [threading.Thread(target=publisher, daemon=True) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
            assert not t.is_alive()
        assert errors == []
        assert _stored(repo) == {"a.com": True, "b.com": True}


class TestQueries:
    def test_list_and_is_supported__see_only_supported_hosts(self, repo: Repo) -> None:
        repo.hosts.publish(["a.com", "b.com"])
        repo.hosts.publish(["b.com"])
        assert repo.hosts.list_supported() == [Host("b.com")]
        assert repo.hosts.is_supported("b.com")
        assert not repo.hosts.is_supported("a.com")

    def test_any_published__is_false_when_empty_and_true_after_publish(self, repo: Repo) -> None:
        assert not repo.hosts.any_published()
        repo.hosts.publish(["a.com"])
        assert repo.hosts.any_published()


HOST = "a.com"
DELAY = 2.0


def _row(repo: Repo) -> dict[str, object]:
    with db.read(repo.engine) as conn:
        row = conn.execute(select(hosts).where(hosts.c.host == HOST)).one()
    return dict(row._mapping)


def _set_reach_after(repo: Repo, when: datetime | None) -> None:
    with db.write(repo.engine) as conn:
        conn.execute(update(hosts).where(hosts.c.host == HOST).values(reach_after=when))


@pytest.fixture
def other_repo(migrated_database: str, clock: FakeClock) -> Iterator[Repo]:
    """A second `Repo` on the same database, for races between two workers."""
    other = Repo(migrated_database, clock=clock)
    yield other
    other.engine.dispose()


@pytest.fixture
def host(repo: Repo) -> str:
    repo.hosts.publish([HOST])
    return HOST


class TestReserve:
    def test_reserve__takes_a_free_host_and_sets_reach_after(
        self, repo: Repo, clock: FakeClock, host: str
    ) -> None:
        assert repo.hosts.reserve(host, DELAY) is None
        assert _row(repo)["reach_after"] == clock.current + timedelta(seconds=DELAY)

    def test_reserve__takes_a_host_whose_reach_after_passed(
        self, repo: Repo, clock: FakeClock, host: str
    ) -> None:
        _set_reach_after(repo, clock.current - timedelta(seconds=1))
        assert repo.hosts.reserve(host, DELAY) is None
        assert _row(repo)["reach_after"] == clock.current + timedelta(seconds=DELAY)

    def test_reserve__returns_a_future_reach_after_and_changes_nothing(
        self, repo: Repo, clock: FakeClock, host: str
    ) -> None:
        later = clock.current + timedelta(minutes=5)
        _set_reach_after(repo, later)
        assert repo.hosts.reserve(host, DELAY) == later
        assert _row(repo)["reach_after"] == later

    def test_reserve__gives_one_slot_to_two_workers_at_once(
        self, repo: Repo, other_repo: Repo, host: str
    ) -> None:
        barrier = threading.Barrier(2)
        results: list[datetime | None] = []

        def reserve(r: Repo) -> None:
            barrier.wait(timeout=30)
            results.append(r.hosts.reserve(host, DELAY))

        threads = [threading.Thread(target=reserve, args=(r,)) for r in (repo, other_repo)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        assert sorted(r is None for r in results) == [False, True]


class TestFailed:
    def test_failed__below_the_threshold_counts_without_pausing(
        self, repo: Repo, clock: FakeClock, host: str
    ) -> None:
        state = repo.hosts.failed(host, FailureKind.UNREACHABLE, retry_after=None)
        assert state == HostState(paused_until=None, failures=1)
        row = _row(repo)
        assert (row["failures"], row["failing_since"], row["last_failure"]) == (
            1,
            clock.current,
            "unreachable",
        )
        assert row["reach_after"] is None

    def test_failed__at_the_threshold_pauses_the_host(
        self, repo: Repo, clock: FakeClock, host: str
    ) -> None:
        repo.hosts.failed(host, FailureKind.UNREACHABLE, retry_after=None)
        state = repo.hosts.failed(host, FailureKind.UNREACHABLE, retry_after=None)
        until = clock.current + timedelta(minutes=5)
        assert state == HostState(paused_until=until, failures=2)
        assert _row(repo)["reach_after"] == until

    def test_failed__keeps_failing_since_from_the_first_failure(
        self, repo: Repo, clock: FakeClock, host: str
    ) -> None:
        first = clock.current
        repo.hosts.failed(host, FailureKind.UNREACHABLE, retry_after=None)
        clock.advance(minutes=1)
        repo.hosts.failed(host, FailureKind.UNREACHABLE, retry_after=None)
        assert _row(repo)["failing_since"] == first

    def test_failed__uses_retry_after_for_a_throttled_host(
        self, repo: Repo, clock: FakeClock, host: str
    ) -> None:
        state = repo.hosts.failed(host, FailureKind.THROTTLED, retry_after=120)
        assert state.paused_until == clock.current + timedelta(seconds=120)

    def test_failed__never_moves_reach_after_earlier(
        self, repo: Repo, clock: FakeClock, host: str
    ) -> None:
        later = clock.current + timedelta(minutes=30)
        _set_reach_after(repo, later)
        state = repo.hosts.failed(host, FailureKind.THROTTLED, retry_after=10)
        assert state.paused_until == later
        assert _row(repo)["reach_after"] == later

    @pytest.mark.parametrize("kind", [FailureKind.TRANSIENT, FailureKind.PERMANENT])
    def test_failed__rejects_kinds_that_never_pause(
        self, repo: Repo, host: str, kind: FailureKind
    ) -> None:
        with pytest.raises(ValueError, match=kind.value):
            repo.hosts.failed(host, kind, retry_after=None)


class TestSucceeded:
    def test_succeeded__resets_the_failure_state_and_keeps_reach_after(
        self, repo: Repo, clock: FakeClock, host: str
    ) -> None:
        repo.hosts.failed(host, FailureKind.THROTTLED, retry_after=60)
        repo.hosts.succeeded(host)
        row = _row(repo)
        assert (row["failures"], row["failing_since"], row["last_failure"]) == (0, None, None)
        assert row["reach_after"] == clock.current + timedelta(seconds=60)


class TestListSupported__WithReachState:
    def test_list_supported__carries_reach_after_and_failing_since(
        self, repo: Repo, clock: FakeClock, host: str
    ) -> None:
        repo.hosts.failed(host, FailureKind.THROTTLED, retry_after=60)
        assert repo.hosts.list_supported() == [
            Host(
                host, reach_after=clock.current + timedelta(seconds=60), failing_since=clock.current
            )
        ]
