"""Event pruning (spec section 4, "Pruning")."""

from sqlalchemy import select, update

from novelhog.repo import Repo, db
from novelhog.repo import events as event_log
from novelhog.repo.schema import consumers
from novelhog.repo.schema import events as events_table
from tests.conftest import FakeClock

NOVEL = "NV000000000001"
DAY = 86400


def _add(repo: Repo, entity_id: str = NOVEL) -> int:
    with db.write(repo.engine) as conn:
        return event_log.append(conn, NOVEL, entity_id, {}, now=repo.clock())


def _seqs(repo: Repo) -> list[int]:
    with db.read(repo.engine) as conn:
        return list(conn.execute(select(events_table.c.seq).order_by(events_table.c.seq)).scalars())


class TestPrune:
    def test_prune__deletes_events_below_the_boundary_keeping_the_newest_of_each_entity(
        self, repo: Repo, clock: FakeClock
    ) -> None:
        for entity in (NOVEL, "CH000000000001", NOVEL, "CH000000000002"):  # seq 1..4
            _add(repo, entity)
        clock.advance(seconds=DAY + 1)
        _add(repo, "CH000000000001")  # 5, recent
        report = repo.events.prune()
        assert report.deleted == 2
        assert _seqs(repo) == [3, 4, 5]

    def test_prune__deletes_nothing_when_every_event_is_recent(self, repo: Repo) -> None:
        _add(repo)
        _add(repo)
        assert repo.events.prune().deleted == 0
        assert _seqs(repo) == [1, 2]


class TestPruneConsumers:
    def test_prune__never_deletes_events_after_the_slowest_consumers_last_seq(
        self, repo: Repo, clock: FakeClock
    ) -> None:
        for _ in range(4):
            _add(repo)
        repo.consumers.claim("slow", "w", 60)  # last_seq 4 ...
        with db.write(repo.engine) as conn:
            conn.execute(update(consumers).values(last_seq=1))
        clock.advance(seconds=DAY + 1)
        _add(repo, "CH000000000001")
        report = repo.events.prune()
        assert report.consumers_moved == []
        # boundary capped at 2: only seq 1 is deletable, and it has a newer NV row
        assert _seqs(repo) == [2, 3, 4, 5]

    def test_prune__moves_a_lagging_consumer_to_the_age_boundary_and_reports_it(
        self, repo: Repo, clock: FakeClock
    ) -> None:
        for _ in range(3):  # seq 1..3, old
            _add(repo)
        repo.consumers.claim("dead", "w", 60)
        with db.write(repo.engine) as conn:
            conn.execute(update(consumers).values(last_seq=0, claimed_by=None, claimed_until=None))
        clock.advance(seconds=8 * DAY)
        _add(repo)  # 4
        _add(repo)  # 5
        report = repo.events.prune()
        assert report.consumers_moved == [("dead", 0, 3)]
        assert _seqs(repo) == [4, 5]
        assert repo.consumers.claim("dead", "w", 60) == 3
