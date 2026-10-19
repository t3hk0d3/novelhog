"""Event append and the commit-ordered event log (spec section 4, "Event log")."""

import threading
from datetime import timedelta
from typing import Any

from sqlalchemy import select

from novelhog.repo import Repo, db
from novelhog.repo import events as event_log
from novelhog.repo.schema import event_clock
from novelhog.repo.schema import events as events_table
from tests.conftest import FakeClock

NOVEL = "NV000000000001"


def _rows(repo: Repo) -> list[Any]:
    with db.read(repo.engine) as conn:
        return list(conn.execute(select(events_table).order_by(events_table.c.seq)).all())


class TestAppend:
    def test_append__increments_clock_and_uses_new_value_as_seq(self, repo: Repo) -> None:
        with db.write(repo.engine) as conn:
            first = event_log.append(conn, NOVEL, NOVEL, {"a": 1}, now=repo.clock())
            second = event_log.append(conn, NOVEL, NOVEL, {"a": 2}, now=repo.clock())
        with db.read(repo.engine) as conn:
            clock_value = conn.execute(select(event_clock.c.seq)).scalar_one()
        assert (first, second, clock_value) == (1, 2, 2)
        assert [row.seq for row in _rows(repo)] == [1, 2]

    def test_append__stores_novel_entity_payload_and_time(
        self, repo: Repo, clock: FakeClock
    ) -> None:
        with db.write(repo.engine) as conn:
            event_log.append(conn, NOVEL, "CH000000000001", {"x": [1]}, now=repo.clock())
        (row,) = _rows(repo)
        assert (row.novel_id, row.entity_id, row.payload, row.at) == (
            NOVEL,
            "CH000000000001",
            {"x": [1]},
            clock.current,
        )

    def test_append__builds_a_callable_payload_after_taking_the_clock(self, repo: Repo) -> None:
        with db.write(repo.engine) as conn:

            def payload() -> dict[str, object]:
                return {"clock": conn.execute(select(event_clock.c.seq)).scalar_one()}

            event_log.append(conn, NOVEL, NOVEL, payload, now=repo.clock())
        (row,) = _rows(repo)
        assert row.payload == {"clock": 1}


def _seqs_after(repo: Repo, cursor: int) -> list[int]:
    with db.read(repo.engine) as conn:
        statement = (
            select(events_table.c.seq)
            .where(events_table.c.seq > cursor)
            .order_by(events_table.c.seq)
        )
        return [row.seq for row in conn.execute(statement).all()]


class _TwoWriters:
    """The first writer takes the clock and holds it; the second starts while it is held."""

    def __init__(self, repo: Repo) -> None:
        self.repo = repo
        self.release_first = threading.Event()
        self.release_second = threading.Event()
        self.first_has_clock = threading.Event()
        self.second_has_clock = threading.Event()
        self.threads = [
            threading.Thread(target=self._first, daemon=True),
            threading.Thread(target=self._second, daemon=True),
        ]

    def _first(self) -> None:
        with db.write(self.repo.engine) as conn:
            event_log.append(conn, NOVEL, NOVEL, {"w": 1}, now=self.repo.clock())
            self.first_has_clock.set()
            self.release_first.wait(timeout=30)

    def _second(self) -> None:
        self.first_has_clock.wait(timeout=30)
        with db.write(self.repo.engine) as conn:
            event_log.append(conn, NOVEL, NOVEL, {"w": 2}, now=self.repo.clock())
            self.second_has_clock.set()
            self.release_second.wait(timeout=30)

    def start(self) -> None:
        for thread in self.threads:
            thread.start()
        assert self.first_has_clock.wait(timeout=30)

    def finish(self) -> None:
        self.release_first.set()
        self.release_second.set()
        for thread in self.threads:
            thread.join(timeout=30)
            assert not thread.is_alive()


class TestAppend__TwoTransactionsCommittingInOppositeOrder:
    def test_append__second_waits_for_first(self, repo: Repo) -> None:
        writers = _TwoWriters(repo)
        writers.start()
        try:
            assert not writers.second_has_clock.wait(timeout=0.5)
            writers.release_first.set()
            assert writers.second_has_clock.wait(timeout=30)
        finally:
            writers.finish()
        assert _seqs_after(repo, 0) == [1, 2]

    def test_append__poller_never_sees_a_later_seq_before_an_earlier_one_commits(
        self, repo: Repo
    ) -> None:
        writers = _TwoWriters(repo)
        writers.start()
        try:
            writers.release_second.set()  # the second writer would commit at once if it could
            assert not writers.second_has_clock.wait(timeout=0.5)
            assert _seqs_after(repo, 0) == []
            writers.release_first.set()
        finally:
            writers.finish()
        assert _seqs_after(repo, 0) == [1, 2]


class TestLatestSeq:
    def test_latest_seq__is_zero_on_an_empty_log(self, repo: Repo) -> None:
        assert repo.events.latest_seq() == 0

    def test_latest_seq__returns_the_clock_value_afterwards(self, repo: Repo) -> None:
        with db.write(repo.engine) as conn:
            event_log.append(conn, NOVEL, NOVEL, {}, now=repo.clock() + timedelta(seconds=1))
            event_log.append(conn, NOVEL, NOVEL, {}, now=repo.clock())
        assert repo.events.latest_seq() == 2
