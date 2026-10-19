"""Reading the event log: `since` and `replay_status` (spec section 5.7)."""

from sqlalchemy import delete

from novelhog.repo import Repo, db
from novelhog.repo import events as event_log
from novelhog.repo.schema import events as events_table
from tests.conftest import FakeClock

NOVEL = "NV000000000001"
OTHER = "NV000000000002"


def _add(repo: Repo, entity_id: str, novel_id: str = NOVEL) -> int:
    with db.write(repo.engine) as conn:
        return event_log.append(conn, novel_id, entity_id, {"id": entity_id}, now=repo.clock())


class TestSince:
    def test_since__returns_later_events_in_order_up_to_limit(self, repo: Repo) -> None:
        for _ in range(4):
            _add(repo, NOVEL)
        assert [e.seq for e in repo.events.since(1, limit=2)] == [2, 3]
        assert [e.seq for e in repo.events.since(0)] == [1, 2, 3, 4]
        assert repo.events.since(4) == []

    def test_since__filters_by_novel_id(self, repo: Repo) -> None:
        _add(repo, NOVEL)
        _add(repo, OTHER, OTHER)
        _add(repo, "CH000000000001", OTHER)
        rows = repo.events.since(0, novel_id=OTHER)
        assert [e.seq for e in rows] == [2, 3]

    def test_since__applies_the_limit_after_the_novel_filter(self, repo: Repo) -> None:
        _add(repo, OTHER, OTHER)
        _add(repo, NOVEL)
        _add(repo, OTHER, OTHER)
        _add(repo, OTHER, OTHER)
        assert [e.seq for e in repo.events.since(0, novel_id=OTHER, limit=2)] == [1, 3]

    def test_since__derives_kind_from_the_entity_prefix(self, repo: Repo) -> None:
        for entity in (NOVEL, "CH000000000001", "EX000000000001"):
            _add(repo, entity)
        rows = repo.events.since(0)
        assert [e.kind for e in rows] == ["novel", "chapter", "export"]
        assert rows[0].payload == {"id": NOVEL}
        assert rows[0].at == repo.clock()


class TestReplayStatus:
    def test_replay_status__ok_for_a_young_existing_row(self, repo: Repo) -> None:
        _add(repo, NOVEL)
        _add(repo, NOVEL)
        assert repo.events.replay_status(1) == "ok"
        assert repo.events.replay_status(2) == "ok"

    def test_replay_status__ok_on_an_empty_log_at_zero(self, repo: Repo) -> None:
        assert repo.events.replay_status(0) == "ok"

    def test_replay_status__resync_when_seq_is_above_latest(self, repo: Repo) -> None:
        _add(repo, NOVEL)
        assert repo.events.replay_status(2) == "resync"

    def test_replay_status__resync_when_the_row_was_pruned(self, repo: Repo) -> None:
        _add(repo, NOVEL)
        _add(repo, NOVEL)
        with db.write(repo.engine) as conn:
            conn.execute(delete(events_table).where(events_table.c.seq == 1))
        assert repo.events.replay_status(1) == "resync"

    def test_replay_status__resync_when_the_row_is_older_than_max_age(
        self, repo: Repo, clock: FakeClock
    ) -> None:
        _add(repo, NOVEL)
        clock.advance(seconds=101)
        assert repo.events.replay_status(1, max_age_s=100) == "resync"
        assert repo.events.replay_status(1, max_age_s=200) == "ok"

    def test_replay_status__resync_when_more_than_max_rows_follow(self, repo: Repo) -> None:
        for _ in range(4):
            _add(repo, NOVEL)
        assert repo.events.replay_status(1, max_rows=3) == "ok"
        assert repo.events.replay_status(1, max_rows=2) == "resync"
