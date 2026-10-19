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


def _commit_two_writers(repo: Repo) -> None:
    """Run two writers: the first takes the clock and holds it, the second must wait."""
    proceed = threading.Event()
    first_has_clock = threading.Event()
    order: list[str] = []

    def first() -> None:
        with db.write(repo.engine) as conn:
            event_log.append(conn, NOVEL, NOVEL, {"w": 1}, now=repo.clock())
            first_has_clock.set()
            proceed.wait(timeout=30)
            order.append("first-commit")

    def second() -> None:
        first_has_clock.wait(timeout=30)
        with db.write(repo.engine) as conn:
            event_log.append(conn, NOVEL, NOVEL, {"w": 2}, now=repo.clock())
            order.append("second-has-clock")

    t1 = threading.Thread(target=first)
    t2 = threading.Thread(target=second)
    t1.start()
    t2.start()
    first_has_clock.wait(timeout=30)
    t2.join(timeout=0.5)
    waited = t2.is_alive()
    proceed.set()
    t1.join(timeout=30)
    t2.join(timeout=30)
    assert waited, "the second writer should wait for the first"
    assert order == ["first-commit", "second-has-clock"]


class TestAppend__TwoTransactionsCommittingInOppositeOrder:
    def test_append__second_waits_for_first(self, repo: Repo) -> None:
        _commit_two_writers(repo)
        assert [row.seq for row in _rows(repo)] == [1, 2]

    def test_append__poller_never_skips_an_event(self, repo: Repo) -> None:
        cursor = 0
        seen: list[int] = []
        stop = threading.Event()

        def poll() -> None:
            nonlocal cursor
            while True:
                done = stop.is_set()
                with db.read(repo.engine) as conn:
                    rows = conn.execute(
                        select(events_table.c.seq)
                        .where(events_table.c.seq > cursor)
                        .order_by(events_table.c.seq)
                    ).all()
                for row in rows:
                    seen.append(row.seq)
                    cursor = row.seq
                if done:
                    return

        poller = threading.Thread(target=poll)
        poller.start()
        _commit_two_writers(repo)
        stop.set()
        poller.join(timeout=30)
        assert seen == [1, 2]


class TestLatestSeq:
    def test_latest_seq__is_zero_on_an_empty_log(self, repo: Repo) -> None:
        assert repo.events.latest_seq() == 0

    def test_latest_seq__returns_the_clock_value_afterwards(self, repo: Repo) -> None:
        with db.write(repo.engine) as conn:
            event_log.append(conn, NOVEL, NOVEL, {}, now=repo.clock() + timedelta(seconds=1))
            event_log.append(conn, NOVEL, NOVEL, {}, now=repo.clock())
        assert repo.events.latest_seq() == 2
