"""The event log: commit-ordered `seq` values from `event_clock` (spec section 4, "Event log").

`append` is internal to `repo`; the public `Events` class only reads the clock.
"""

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime

from sqlalchemy import Connection, insert, select, update

from novelhog.repo import db
from novelhog.repo.context import RepoContext
from novelhog.repo.schema import event_clock
from novelhog.repo.schema import events as events_table

Payload = dict[str, object] | Callable[[], dict[str, object]]


def append(
    conn: Connection, novel_id: str, entity_id: str, payload: Payload, *, now: datetime
) -> int:
    """Take the next `seq` from `event_clock`, store the event and return the `seq`.

    The clock update holds the row lock until the transaction ends, so event writers commit in
    `seq` order. A callable `payload` is evaluated after the clock was taken, so snapshots built
    by it include every earlier committed change; later appends in the same transaction (the
    clock is already held) may pass plain dicts.

    Lock order (spec section 4): a transaction that writes any row of a novel first calls
    `novels.lock_novel(conn, novel_id)` (and abandons the write if it returns False). Then, in an
    event-writing transaction, every row write, and every
    locking read such as `DELETE ... RETURNING`, comes first and `append` comes last; no new row
    lock may be taken after the first `append`. Taking the clock first in one transaction and
    last in another deadlocks on Postgres.
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


def emit_novel_change(
    conn: Connection,
    novel_id: str,
    changed_chapter_ids: Sequence[str],
    *,
    now: datetime,
    static_base_url: str,
    released: list[int] | None = None,
    errors: Mapping[str, tuple[str, datetime]] | None = None,
    positions_changed: bool = False,
) -> None:
    """Append the events of a chapter change: one `CH` per chapter, one `NV`, then the `EX`s.

    The `EX` events go to every waiting export of the novel whose range contains a changed
    chapter (all waiting exports when `positions_changed`, because a sync moved or removed
    chapters). `released` adds the sync's released positions to the `NV` payload; `errors` gives
    the message and time of chapters that just failed (other chapters in `error` carry their
    previous error forward). Call it last in the transaction, after `novels.lock_novel` and every
    row write; snapshots are built lazily inside `append`.
    """
    # Imported here: novels, chapters and exports import this module.
    from novelhog.repo import chapters, exports, novels  # noqa: PLC0415

    failures = dict(errors or {})

    def chapter_payload(chapter_id: str) -> Payload:
        return lambda: chapters.event_payload(conn, novel_id, chapter_id, failures)

    def export_payload(export_id: str) -> Payload:
        return lambda: exports.snapshot(conn, export_id, static_base_url=static_base_url).to_json()

    def novel_payload() -> dict[str, object]:
        view = novels.snapshot(conn, novel_id, now=now, static_base_url=static_base_url)
        return view.to_json() if released is None else view.to_event(released)

    for chapter_id in changed_chapter_ids:
        append(conn, novel_id, chapter_id, chapter_payload(chapter_id), now=now)
    append(conn, novel_id, novel_id, novel_payload, now=now)
    for export_id in exports.affected_waiting(
        conn, novel_id, changed_chapter_ids, positions_changed=positions_changed
    ):
        append(conn, novel_id, export_id, export_payload(export_id), now=now)


class Events:
    """Read access to the event log."""

    def __init__(self, context: RepoContext) -> None:
        self._engine = context.engine

    def latest_seq(self) -> int:
        """Return the clock value: the last `seq` handed out (0 on an empty log)."""
        with db.read(self._engine) as conn:
            return int(
                conn.execute(select(event_clock.c.seq).where(event_clock.c.id == 1)).scalar_one()
            )
