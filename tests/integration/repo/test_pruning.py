"""Event pruning (spec section 4, "Pruning")."""

from sqlalchemy import select, update

from novelhog.repo import Repo, db
from novelhog.repo import events as event_log
from novelhog.repo.schema import consumers
from novelhog.repo.schema import events as events_table
from tests.conftest import FakeClock

NOVEL = "NV000000000001"
DAY = 86400


def _add(repo: Repo, entity_id: str = NOVEL, payload: dict[str, object] | None = None) -> int:
    with db.write(repo.engine) as conn:
        return event_log.append(conn, NOVEL, entity_id, payload or {}, now=repo.clock())


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


class TestPruneTaskEvents:
    def test_prune__deletes_task_events_by_age_even_as_an_entitys_newest_row(
        self, repo: Repo, clock: FakeClock
    ) -> None:
        _add(repo, "TK000000000001")  # 1, old and its entity's only row
        _add(repo, NOVEL)  # 2, old and its entity's newest row
        clock.advance(seconds=DAY + 1)
        _add(repo, "TK000000000002")  # 3, recent
        report = repo.events.prune()
        assert report.deleted == 1
        assert _seqs(repo) == [2, 3]

    def test_prune__still_keeps_the_newest_nv_ch_and_ex_row_of_an_entity(
        self, repo: Repo, clock: FakeClock
    ) -> None:
        for entity in (NOVEL, "CH000000000001", "EX000000000001"):
            _add(repo, entity)
        clock.advance(seconds=DAY + 1)
        _add(repo, "TK000000000001")
        assert repo.events.prune().deleted == 0
        assert _seqs(repo) == [1, 2, 3, 4]


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

    def test_prune__moves_a_lagging_consumer_reports_skipped_releases_and_fences_its_owner(
        self, repo: Repo, clock: FakeClock
    ) -> None:
        _add(repo, payload={"released": [1], "followed": True})  # 1: a release
        _add(repo, payload={"released": [2], "followed": False})  # 2: not followed
        _add(repo, "CH000000000001", {"released": [3], "followed": True})  # 3: not an NV event
        _add(repo, payload={"released": [], "followed": True})  # 4: nothing released
        repo.consumers.claim("dead", "w", 10 * DAY)
        with db.write(repo.engine) as conn:
            conn.execute(update(consumers).values(last_seq=0))
        clock.advance(seconds=8 * DAY)
        _add(repo)  # 5
        _add(repo)  # 6
        report = repo.events.prune()
        assert report.consumers_moved == [("dead", 0, 4, 1)]
        assert _seqs(repo) == [3, 5, 6]
        assert repo.consumers.advance("dead", "w", 5) is False
        assert repo.consumers.renew("dead", "w", 60) is False
        assert repo.consumers.claim("dead", "w2", 60) == 4
