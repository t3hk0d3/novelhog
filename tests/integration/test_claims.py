"""Claim, renewal, release, fenced update and expiry primitives (spec section 4, "Claims")."""

import threading
from collections.abc import Iterator
from datetime import timedelta
from typing import Any, cast

import pytest
from sqlalchemy import (
    Column,
    Connection,
    Integer,
    MetaData,
    Row,
    Table,
    Text,
    event,
    insert,
    select,
)
from sqlalchemy.engine import Engine
from sqlalchemy.sql import Executable

from novelhog.repo import Repo, db
from novelhog.repo.claims import (
    CLAIMABLE,
    available,
    claim,
    expired,
    fenced_update,
    owned,
    release,
    renew,
)
from novelhog.repo.schema import chapters, consumers, exports, novels
from novelhog.repo.types import UTCDateTime
from tests.integration.helpers import NOW, chapter_row, insert_novel_with_chapter

TTL = timedelta(minutes=5)

_test_metadata = MetaData()
jobs = Table(
    "claims_test_jobs",
    _test_metadata,
    Column("id", Integer, primary_key=True, autoincrement=False),
    Column("rank", Integer, nullable=False),
    Column("kind", Text, nullable=False, server_default="work"),
    Column("note", Text),
    Column("claimed_by", Text),
    Column("claimed_until", UTCDateTime),
)


@pytest.fixture
def engine(repo: Repo) -> Iterator[Engine]:
    """The test engine with an empty `claims_test_jobs` table."""
    _test_metadata.create_all(repo.engine)
    yield repo.engine
    _test_metadata.drop_all(repo.engine)


def _add_jobs(engine: Engine, *rows: dict[str, Any]) -> None:
    with db.write(engine) as connection:
        connection.execute(insert(jobs), list(rows))


def _job(job_id: int, rank: int | None = None, **extra: Any) -> dict[str, Any]:
    return {"id": job_id, "rank": job_id if rank is None else rank, **extra}


def _claim(engine: Engine, owner: str, *, n: int = 1, **kwargs: Any) -> list[Row[Any]]:
    arguments: dict[str, Any] = {
        "eligible": jobs.c.kind == "work",
        "priority": [jobs.c.rank],
        "owner": owner,
        "now": NOW,
        "ttl": TTL,
        "n": n,
        "returning": [jobs.c.id, jobs.c.rank],
    }
    with db.write(engine) as connection:
        return claim(connection, jobs, **(arguments | kwargs))


def _state(engine: Engine, job_id: int) -> Row[Any]:
    with db.read(engine) as connection:
        return connection.execute(select(jobs).where(jobs.c.id == job_id)).one()


def _ids(rows: list[Row[Any]]) -> list[int]:
    return [row.id for row in rows]


class TestClaimable:
    def test_claimable__lists_the_four_claimable_tables(self) -> None:
        assert CLAIMABLE == (novels, chapters, exports, consumers)


class TestClaim__WithAnAvailableEligibleRow:
    def test_claim__sets_owner_expiry_and_extra_values(self, engine: Engine) -> None:
        _add_jobs(engine, _job(1))

        _claim(engine, "w1", extra_set={"note": "taken"})

        row = _state(engine, 1)
        assert (row.claimed_by, row.claimed_until, row.note) == ("w1", NOW + TTL, "taken")

    def test_claim__returns_the_claimed_rows(self, engine: Engine) -> None:
        _add_jobs(engine, _job(1))

        rows = _claim(engine, "w1")

        assert [(row.id, row.rank) for row in rows] == [(1, 1)]

    def test_claim__ignores_rows_that_are_not_eligible(self, engine: Engine) -> None:
        _add_jobs(engine, _job(1, kind="other"))

        assert _claim(engine, "w1") == []
        assert _state(engine, 1).claimed_by is None


class TestClaim__WithRowsOwnedByALiveClaim:
    def test_claim__skips_them_and_returns_other_rows(self, engine: Engine) -> None:
        _add_jobs(engine, _job(1), _job(2))
        _claim(engine, "w1")

        assert _ids(_claim(engine, "w2")) == [2]
        assert _state(engine, 1).claimed_by == "w1"

    def test_claim__returns_nothing_when_none_remain(self, engine: Engine) -> None:
        _add_jobs(engine, _job(1))
        _claim(engine, "w1")

        assert _claim(engine, "w2") == []

    def test_claim__treats_a_claim_expiring_exactly_now_as_live(self, engine: Engine) -> None:
        _add_jobs(engine, _job(1))
        _claim(engine, "w1")

        assert _claim(engine, "w2", now=NOW + TTL) == []


class TestClaim__WithARowWhoseClaimExpired:
    def test_claim__takes_it_over(self, engine: Engine) -> None:
        _add_jobs(engine, _job(1))
        _claim(engine, "w1")
        later = NOW + TTL + timedelta(seconds=1)

        rows = _claim(engine, "w2", now=later)

        assert _ids(rows) == [1]
        row = _state(engine, 1)
        assert (row.claimed_by, row.claimed_until) == ("w2", later + TTL)


class TestClaim__WithMoreThanOneRow:
    def test_claim__claims_up_to_n_rows_sorted_by_priority(self, engine: Engine) -> None:
        _add_jobs(engine, _job(1, rank=30), _job(2, rank=10), _job(3, rank=20), _job(4, rank=40))

        rows = _claim(engine, "w1", n=3)

        assert _ids(rows) == [2, 3, 1]
        assert _state(engine, 4).claimed_by is None

    def test_claim__sorts_by_every_priority_expression_in_order(self, engine: Engine) -> None:
        _add_jobs(engine, _job(1, rank=1), _job(2, rank=2), _job(3, rank=2), _job(4, rank=1))

        rows = _claim(engine, "w1", n=4, priority=[jobs.c.rank.desc(), jobs.c.id.desc()])

        assert _ids(rows) == [3, 2, 4, 1]

    def test_claim__sorts_by_a_priority_column_missing_from_returning(self, engine: Engine) -> None:
        _add_jobs(engine, _job(1, rank=2), _job(2, rank=1))

        rows = _claim(engine, "w1", n=2, returning=[jobs.c.id])

        assert _ids(rows) == [2, 1]

    def test_claim__claims_fewer_when_fewer_are_eligible(self, engine: Engine) -> None:
        _add_jobs(engine, _job(1), _job(2))

        assert _ids(_claim(engine, "w1", n=5)) == [1, 2]


class TestClaim__WithConcurrentThreads:
    """SQLite serialises writers (BEGIN IMMEDIATE), so these races only have teeth on Postgres."""

    @staticmethod
    def _race(engine: Engine, threads: int, batch: int) -> list[list[int]]:
        barrier = threading.Barrier(threads)
        results: list[list[int]] = [[] for _ in range(threads)]
        errors: list[BaseException] = []

        def worker(index: int) -> None:
            try:
                barrier.wait(timeout=30)
                while rows := _claim(engine, f"w{index}", n=batch):
                    results[index].extend(_ids(rows))
            except BaseException as error:  # noqa: BLE001  (re-raised in the main thread)
                errors.append(error)

        pool = [threading.Thread(target=worker, args=(i,)) for i in range(threads)]
        for thread in pool:
            thread.start()
        for thread in pool:
            thread.join(timeout=60)
        if errors:
            raise errors[0]
        return results

    def test_claim__8_threads_claiming_single_rows_claim_each_of_5_rows_once(
        self, engine: Engine
    ) -> None:
        _add_jobs(engine, *(_job(i) for i in range(1, 6)))

        results = self._race(engine, threads=8, batch=1)

        assert sorted(id_ for ids in results for id_ in ids) == [1, 2, 3, 4, 5]
        for index, ids in enumerate(results):
            assert len(ids) == len(set(ids))
            assert all(_state(engine, id_).claimed_by == f"w{index}" for id_ in ids)

    def test_claim__4_threads_claiming_batches_of_3_claim_each_of_12_rows_once(
        self, engine: Engine
    ) -> None:
        _add_jobs(engine, *(_job(i) for i in range(1, 13)))

        results = self._race(engine, threads=4, batch=3)

        assert sorted(id_ for ids in results for id_ in ids) == list(range(1, 13))


class TestClaim__WhenAConcurrentClaimCommitsFirst:
    def test_claim__a_blocked_claim_on_the_same_row_gets_nothing(self, engine: Engine) -> None:
        if engine.dialect.name != "postgresql":
            pytest.skip("SQLite serialises writers; the blocked claim only happens on Postgres")
        _add_jobs(engine, _job(1))
        outcome: list[list[int]] = []
        failures: list[BaseException] = []

        def second_claim() -> None:
            try:
                outcome.append(_ids(_claim(engine, "b")))
            except BaseException as error:  # noqa: BLE001  (re-raised in the main thread)
                failures.append(error)

        with db.write(engine) as connection:
            first = claim(
                connection,
                jobs,
                eligible=jobs.c.kind == "work",
                priority=[jobs.c.rank],
                owner="a",
                now=NOW,
                ttl=TTL,
                returning=[jobs.c.id],
            )
            thread = threading.Thread(target=second_claim)
            thread.start()
            thread.join(timeout=1)
            assert thread.is_alive(), "the second claim should block on the first one's row lock"
        thread.join(timeout=30)

        if failures:
            raise failures[0]
        assert _ids(first) == [1]
        assert outcome == [[]]
        assert _state(engine, 1).claimed_by == "a"


class _LosingFirstUpdate:
    """A connection wrapper whose first UPDATE matches nothing, as if a concurrent claim won."""

    def __init__(self, connection: Connection, *, always: bool = False) -> None:
        self._connection = connection
        self._always = always
        self.updates = 0

    def execute(self, statement: Executable, *args: Any, **kwargs: Any) -> Any:
        if getattr(statement, "is_update", False):
            self.updates += 1
            if self._always or self.updates == 1:
                return self._connection.execute(
                    cast(Any, statement).where(jobs.c.id < 0), *args, **kwargs
                )
        return self._connection.execute(statement, *args, **kwargs)


class TestClaim__WhenARaceIsLostAndEligibleRowsRemain:
    def test_claim__retries_and_returns_the_next_candidates(self, engine: Engine) -> None:
        _add_jobs(engine, _job(1), _job(2))

        with db.write(engine) as connection:
            wrapper = _LosingFirstUpdate(connection)
            rows = claim(
                cast(Connection, wrapper),
                jobs,
                eligible=jobs.c.kind == "work",
                priority=[jobs.c.rank],
                owner="w1",
                now=NOW,
                ttl=TTL,
                n=2,
                returning=[jobs.c.id],
            )

        assert _ids(rows) == [1, 2]
        assert wrapper.updates == 2

    def test_claim__gives_up_after_the_retry_limit(self, engine: Engine) -> None:
        _add_jobs(engine, _job(1))

        with db.write(engine) as connection:
            wrapper = _LosingFirstUpdate(connection, always=True)
            rows = claim(
                cast(Connection, wrapper),
                jobs,
                eligible=jobs.c.kind == "work",
                priority=[jobs.c.rank],
                owner="w1",
                now=NOW,
                ttl=TTL,
                returning=[jobs.c.id],
                retries=3,
            )

        assert rows == []
        assert wrapper.updates == 4

    def test_claim__does_not_retry_when_nothing_eligible_remains(self, engine: Engine) -> None:
        with db.write(engine) as connection:
            wrapper = _LosingFirstUpdate(connection, always=True)
            claim(
                cast(Connection, wrapper),
                jobs,
                eligible=jobs.c.kind == "work",
                priority=[jobs.c.rank],
                owner="w1",
                now=NOW,
                ttl=TTL,
                returning=[jobs.c.id],
            )

        assert wrapper.updates == 1


class TestClaim__Statements:
    def test_claim__never_issues_select_for_update(self, engine: Engine) -> None:
        _add_jobs(engine, _job(1), _job(2))
        statements: list[str] = []

        @event.listens_for(engine, "before_cursor_execute")
        def capture(_c: object, _cur: object, statement: str, *_rest: object) -> None:
            statements.append(statement)

        try:
            _claim(engine, "w1", n=2)
            _claim(engine, "w2", n=2)
        finally:
            event.remove(engine, "before_cursor_execute", capture)

        assert any(s.lstrip().upper().startswith("UPDATE") for s in statements)
        assert not any("FOR UPDATE" in s.upper() for s in statements)


class TestClaim__OnChapters:
    def test_claim__claims_pending_chapters_in_queue_order(self, repo: Repo) -> None:
        with db.write(repo.engine) as connection:
            insert_novel_with_chapter(connection)
            queued = {"state": "pending", "queued_at": NOW}
            connection.execute(
                chapters.update().where(chapters.c.id == "CH000000000001").values(**queued)
            )
            for row in (
                chapter_row(
                    id="CH000000000002", url="https://example.com/novel/2", position=2, **queued
                ),
                chapter_row(
                    id="CH000000000003",
                    url="https://example.com/novel/3",
                    position=3,
                    priority=True,
                    **queued,
                ),
                chapter_row(id="CH000000000004", url="https://example.com/novel/4", position=4),
            ):
                connection.execute(insert(chapters).values(**row))

        with db.write(repo.engine) as connection:
            rows = claim(
                connection,
                chapters,
                eligible=chapters.c.state == "pending",
                priority=[chapters.c.priority.desc(), chapters.c.queued_at, chapters.c.position],
                owner="w1",
                now=NOW,
                ttl=TTL,
                n=10,
                returning=[chapters.c.id],
            )

        assert [row.id for row in rows] == ["CH000000000003", "CH000000000001", "CH000000000002"]
        with db.read(repo.engine) as connection:
            owners = dict(connection.execute(select(chapters.c.id, chapters.c.claimed_by)).all())
        assert owners["CH000000000004"] is None
        assert owners["CH000000000003"] == "w1"


class TestClaim__Validation:
    def test_claim__rejects_extra_values_that_set_the_claim_columns(self, engine: Engine) -> None:
        for column in ("claimed_by", "claimed_until"):
            with pytest.raises(ValueError, match=column):
                _claim(engine, "w1", extra_set={column: None})

    def test_claim__orders_rows_tied_on_every_priority_by_key(self, engine: Engine) -> None:
        _add_jobs(engine, _job(3, rank=1), _job(1, rank=1), _job(2, rank=1))

        assert _ids(_claim(engine, "w1", n=3)) == [1, 2, 3]

    def test_claim__orders_tied_rows_by_key_after_descending_priority(self, engine: Engine) -> None:
        _add_jobs(engine, _job(1, rank=2), _job(2, rank=2), _job(3, rank=1))

        assert _ids(_claim(engine, "w1", n=3, priority=[jobs.c.rank.desc()])) == [1, 2, 3]


class TestRenew:
    def test_renew__pushes_expiry_forward_only_on_rows_owned_by_owner(self, engine: Engine) -> None:
        _add_jobs(engine, _job(1), _job(2), _job(3))
        _claim(engine, "w1", n=2)
        _claim(engine, "w2")
        later = NOW + timedelta(minutes=2)

        with db.write(engine) as connection:
            count = renew(connection, jobs, [1, 2, 3], owner="w1", now=later, ttl=TTL)

        assert count == 2
        assert _state(engine, 1).claimed_until == later + TTL
        assert _state(engine, 2).claimed_until == later + TTL
        assert _state(engine, 3).claimed_until == NOW + TTL

    def test_renew__returns_0_for_a_row_whose_claim_expired(self, engine: Engine) -> None:
        _add_jobs(engine, _job(1))
        _claim(engine, "w1")

        with db.write(engine) as connection:
            count = renew(
                connection, jobs, [1], owner="w1", now=NOW + TTL + timedelta(seconds=1), ttl=TTL
            )

        assert count == 0
        assert _state(engine, 1).claimed_until == NOW + TTL

    def test_renew__returns_0_for_no_keys(self, engine: Engine) -> None:
        with db.write(engine) as connection:
            assert renew(connection, jobs, [], owner="w1", now=NOW, ttl=TTL) == 0


class TestRelease:
    def test_release__clears_both_columns_and_applies_extra_values_only_when_owned(
        self, engine: Engine
    ) -> None:
        _add_jobs(engine, _job(1), _job(2))
        _claim(engine, "w1")
        _claim(engine, "w2")

        with db.write(engine) as connection:
            count = release(connection, jobs, [1, 2], owner="w1", now=NOW, extra_set={"note": "x"})

        assert count == 1
        released = _state(engine, 1)
        assert (released.claimed_by, released.claimed_until, released.note) == (None, None, "x")
        kept = _state(engine, 2)
        assert (kept.claimed_by, kept.claimed_until, kept.note) == ("w2", NOW + TTL, None)

    def test_release__leaves_an_expired_claim_alone(self, engine: Engine) -> None:
        _add_jobs(engine, _job(1))
        _claim(engine, "w1")

        with db.write(engine) as connection:
            count = release(connection, jobs, [1], owner="w1", now=NOW + TTL + timedelta(seconds=1))

        assert count == 0
        assert _state(engine, 1).claimed_by == "w1"


class TestRelease__WithWhere:
    def test_release__where_narrows_the_rows_so_an_owned_row_failing_it_keeps_its_claim(
        self, engine: Engine
    ) -> None:
        _add_jobs(engine, _job(1, kind="work"), _job(2, kind="other"))
        _claim(engine, "w1", n=2, eligible=jobs.c.id > 0)

        with db.write(engine) as connection:
            count = release(
                connection, jobs, [1, 2], owner="w1", now=NOW, where=jobs.c.kind == "work"
            )

        assert count == 1
        assert _state(engine, 1).claimed_by is None
        assert _state(engine, 2).claimed_by == "w1"

    def test_release__where_cannot_widen_the_fence_to_rows_owned_by_others(
        self, engine: Engine
    ) -> None:
        _add_jobs(engine, _job(1))
        _claim(engine, "w2")

        with db.write(engine) as connection:
            count = release(connection, jobs, [1], owner="w1", now=NOW, where=jobs.c.kind == "work")

        assert count == 0
        assert _state(engine, 1).claimed_by == "w2"


class TestRelease__Validation:
    def test_release__rejects_extra_values_that_set_the_claim_columns(self, engine: Engine) -> None:
        for column in ("claimed_by", "claimed_until"):
            with db.write(engine) as connection, pytest.raises(ValueError, match=column):
                release(connection, jobs, [1], owner="w1", now=NOW, extra_set={column: "x"})


class TestFencedUpdate:
    def test_fenced_update__applies_values_on_a_row_owned_by_owner(self, engine: Engine) -> None:
        _add_jobs(engine, _job(1))
        _claim(engine, "w1")

        with db.write(engine) as connection:
            applied = fenced_update(connection, jobs, 1, owner="w1", now=NOW, values={"note": "n"})

        assert applied is True
        assert _state(engine, 1).note == "n"

    def test_fenced_update__where_failing_on_an_owned_row_changes_nothing(
        self, engine: Engine
    ) -> None:
        _add_jobs(engine, _job(1))
        _claim(engine, "w1")

        with db.write(engine) as connection:
            applied = fenced_update(
                connection,
                jobs,
                1,
                owner="w1",
                now=NOW,
                values={"note": "n"},
                where=jobs.c.kind == "other",
            )

        assert applied is False
        row = _state(engine, 1)
        assert (row.note, row.claimed_by) == (None, "w1")

    def test_fenced_update__where_matching_a_row_owned_by_someone_else_changes_nothing(
        self, engine: Engine
    ) -> None:
        _add_jobs(engine, _job(1))
        _claim(engine, "w2")

        with db.write(engine) as connection:
            applied = fenced_update(
                connection,
                jobs,
                1,
                owner="w1",
                now=NOW,
                values={"note": "n"},
                where=jobs.c.kind == "work",
            )

        assert applied is False
        assert _state(engine, 1).note is None

    @pytest.mark.parametrize("situation", ["other owner", "unclaimed", "expired", "deleted"])
    def test_fenced_update__changes_nothing_when_the_claim_is_not_mine(
        self, engine: Engine, situation: str
    ) -> None:
        _add_jobs(engine, _job(1))
        now = NOW
        if situation == "other owner":
            _claim(engine, "w2")
        elif situation == "expired":
            _claim(engine, "w1")
            now = NOW + TTL + timedelta(seconds=1)
        elif situation == "deleted":
            with db.write(engine) as connection:
                connection.execute(jobs.delete())

        with db.write(engine) as connection:
            applied = fenced_update(connection, jobs, 1, owner="w1", now=now, values={"note": "n"})

        assert applied is False
        if situation != "deleted":
            assert _state(engine, 1).note is None


class TestExpired:
    def test_expired__returns_claimed_rows_past_their_expiry_and_nothing_else(
        self, engine: Engine
    ) -> None:
        _add_jobs(engine, _job(1), _job(2), _job(3), _job(4))
        _claim(engine, "w1", eligible=jobs.c.id == 1)  # expires at NOW + TTL
        later = NOW + timedelta(minutes=10)
        _claim(engine, "w1", n=2, now=later, eligible=jobs.c.id.in_([2, 3]))  # still live
        # job 4 stays unclaimed
        at = NOW + TTL + timedelta(seconds=1)

        with db.read(engine) as connection:
            assert _ids(expired(connection, jobs, at)) == [1]
            assert expired(connection, jobs, NOW + TTL) == []


class TestPredicates:
    def test_available_and_owned__compile_to_the_spec_conditions(self) -> None:
        free = str(available(jobs, NOW))
        mine = str(owned(jobs, "w1", NOW))

        assert "claimed_by IS NULL" in free and "claimed_until <" in free
        assert "claimed_by =" in mine and "claimed_until >=" in mine
