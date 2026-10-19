"""The event log: commit-ordered `seq` values from `event_clock` (spec section 4, "Event log").

`append` is internal to `repo`; the public `Events` class only reads the clock.
"""

from collections.abc import Callable
from datetime import datetime

from sqlalchemy import Connection, Engine, insert, select, update

from novelhog.repo import db
from novelhog.repo.schema import event_clock
from novelhog.repo.schema import events as events_table

Payload = dict[str, object] | Callable[[], dict[str, object]]


def append(
    conn: Connection, novel_id: str, entity_id: str, payload: Payload, *, now: datetime
) -> int:
    """Take the next `seq` from `event_clock`, store the event and return the `seq`.

    The clock update holds the row lock until the transaction ends, so event writers commit in
    `seq` order. A callable `payload` is evaluated after the clock was taken, so snapshots built
    by it include every earlier committed change.
    """
    statement = update(event_clock).values(seq=event_clock.c.seq + 1).returning(event_clock.c.seq)
    seq = conn.execute(statement.where(event_clock.c.id == 1)).scalar_one()
    data = payload() if callable(payload) else payload
    conn.execute(
        insert(events_table).values(
            seq=seq, novel_id=novel_id, entity_id=entity_id, payload=data, at=now
        )
    )
    return int(seq)


class Events:
    """Read access to the event log."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def latest_seq(self) -> int:
        """Return the clock value: the last `seq` handed out (0 on an empty log)."""
        with db.read(self._engine) as conn:
            return int(
                conn.execute(select(event_clock.c.seq).where(event_clock.c.id == 1)).scalar_one()
            )
