"""The hosts repository (spec sections 4 and 5.3)."""

from sqlalchemy import insert, select

from novelhog.repo import Repo, db
from novelhog.repo.hosts import Host, PublishReport
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
