"""Durable event-log readers: named cursors with claims (spec section 4, "Claims"; section 6.6)."""

from datetime import timedelta

from sqlalchemy import select

from novelhog.repo import claims, db
from novelhog.repo.context import RepoContext
from novelhog.repo.schema import consumers, event_clock
from novelhog.repo.sql import dialect_insert


class Consumers:
    """Cursor rows in `consumers`; only the claim holder reads and advances a cursor."""

    def __init__(self, context: RepoContext) -> None:
        self._engine = context.engine
        self._clock = context.clock

    def claim(self, name: str, owner: str, ttl: int) -> int | None:
        """Claim the consumer for `ttl` seconds and return its `last_seq`; None if held by another.

        A missing row is created with `last_seq` at the current clock, so history is not replayed.
        """
        now = self._clock()
        with db.write(self._engine) as conn:
            start = conn.execute(
                select(event_clock.c.seq).where(event_clock.c.id == 1)
            ).scalar_one()
            conn.execute(
                dialect_insert(self._engine, consumers)
                .values(name=name, last_seq=start, updated_at=now)
                .on_conflict_do_nothing(index_elements=["name"])
            )
            rows = claims.claim(
                conn,
                consumers,
                eligible=consumers.c.name == name,
                priority=[consumers.c.name],
                owner=owner,
                now=now,
                ttl=timedelta(seconds=ttl),
                returning=[consumers.c.last_seq],
            )
        return int(rows[0].last_seq) if rows else None

    def renew(self, name: str, owner: str, ttl: int) -> bool:
        """Extend the claim by `ttl` seconds; False when `owner` no longer holds it."""
        with db.write(self._engine) as conn:
            return (
                claims.renew(
                    conn,
                    consumers,
                    [name],
                    owner=owner,
                    now=self._clock(),
                    ttl=timedelta(seconds=ttl),
                )
                == 1
            )

    def advance(self, name: str, owner: str, last_seq: int) -> bool:
        """Store `last_seq` and release the claim in one fenced write.

        Returns False, changing nothing, when the claim was lost. Raises `ValueError` when it
        would move the cursor backwards.
        """
        now = self._clock()
        with db.write(self._engine) as conn:
            applied = claims.fenced_update(
                conn,
                consumers,
                name,
                owner=owner,
                now=now,
                values={
                    "last_seq": last_seq,
                    "updated_at": now,
                    "claimed_by": None,
                    "claimed_until": None,
                },
                where=consumers.c.last_seq <= last_seq,
            )
            if applied:
                return True
            current = conn.execute(
                select(consumers.c.last_seq).where(
                    consumers.c.name == name, claims.owned(consumers, owner, now)
                )
            ).scalar_one_or_none()
        if current is not None:
            message = f"consumer {name} is at {current}; cannot move it back to {last_seq}"
            raise ValueError(message)
        return False

    def release(self, name: str, owner: str) -> bool:
        """Clear the claim without moving the cursor; False when `owner` does not hold it."""
        with db.write(self._engine) as conn:
            return claims.release(conn, consumers, [name], owner=owner, now=self._clock()) == 1
