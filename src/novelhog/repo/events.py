"""The event log: commit-ordered `seq` values from `event_clock` (spec section 4, "Event log").

`append` is internal to `repo`; the public `Events` class reads the log and prunes it.

Readers (`since`, `replay_status`) rely on `append` holding the clock row lock until commit, so
events become visible in `seq` order: a reader that has seen `seq` N never misses a lower `seq`
committing later. `tests/integration/repo/test_event_append.py` pins this invariant.
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from sqlalchemy import Connection, delete, func, insert, select, update

from novelhog.repo import db
from novelhog.repo.context import RepoContext
from novelhog.repo.schema import consumers, event_clock
from novelhog.repo.schema import events as events_table

_KINDS: dict[str, Literal["novel", "chapter", "export"]] = {
    "NV": "novel",
    "CH": "chapter",
    "EX": "export",
}

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
) -> None:
    """Append the events of a chapter change: one `CH` per chapter, one `NV`, then the `EX`s.

    The `EX` events go to every waiting export of the novel with a changed end chapter or whose
    range contains a changed chapter that still has a position. `released` adds the sync's
    released positions to the `NV` payload; `errors` gives the message and time of chapters that
    just failed (other chapters in `error` carry their previous error forward). Call it last in
    the transaction, after `novels.lock_novel` and every row write; snapshots are built lazily
    inside `append`.
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
    for export_id in exports.affected_waiting(conn, novel_id, changed_chapter_ids):
        append(conn, novel_id, export_id, export_payload(export_id), now=now)


@dataclass(frozen=True)
class EventRow:
    """A stored event: its `seq`, the novel and entity it belongs to, the snapshot and the time."""

    seq: int
    novel_id: str
    entity_id: str
    payload: dict[str, object]
    at: datetime

    @property
    def kind(self) -> Literal["novel", "chapter", "export"]:
        """The SSE event name, derived from the `entity_id` prefix."""
        return _KINDS[self.entity_id[:2]]


@dataclass(frozen=True)
class PruneReport:
    """What `prune` did: rows deleted, and the consumers moved forward.

    Each moved consumer is `(name, old last_seq, new last_seq, skipped_releases)`, where
    `skipped_releases` counts the `release` notification events (`NV` events with a non-empty
    `released` and `followed = true`, spec section 6.6) in the skipped range `(old, new]`.
    """

    deleted: int
    consumers_moved: list[tuple[str, int, int, int]]


def _first_seq_since(conn: Connection, cutoff: datetime, latest: int) -> int:
    """The smallest `seq` whose event is at or after `cutoff`, or `latest + 1` if none."""
    found = conn.execute(
        select(func.min(events_table.c.seq)).where(events_table.c.at >= cutoff)
    ).scalar_one()
    return latest + 1 if found is None else int(found)


def _count_releases(conn: Connection, after: int, up_to: int) -> int:
    """Count `release` notification events (spec section 6.6) with `after < seq <= up_to`."""
    payloads = conn.execute(
        select(events_table.c.payload).where(
            events_table.c.seq > after,
            events_table.c.seq <= up_to,
            events_table.c.entity_id.like("NV%"),
        )
    ).scalars()
    return sum(1 for p in payloads if p.get("released") and p.get("followed") is True)


class Events:
    """Read access to the event log, plus pruning."""

    def __init__(self, context: RepoContext) -> None:
        self._engine = context.engine
        self._clock = context.clock

    def latest_seq(self) -> int:
        """Return the clock value: the last `seq` handed out (0 on an empty log)."""
        with db.read(self._engine) as conn:
            return int(
                conn.execute(select(event_clock.c.seq).where(event_clock.c.id == 1)).scalar_one()
            )

    def since(self, seq: int, *, novel_id: str | None = None, limit: int = 500) -> list[EventRow]:
        """Return events with a `seq` above the given one, in order, at most `limit`."""
        statement = select(events_table).where(events_table.c.seq > seq)
        if novel_id is not None:
            statement = statement.where(events_table.c.novel_id == novel_id)
        statement = statement.order_by(events_table.c.seq).limit(limit)
        with db.read(self._engine) as conn:
            return [
                EventRow(row.seq, row.novel_id, row.entity_id, row.payload, row.at)
                for row in conn.execute(statement).all()
            ]

    def replay_status(
        self, seq: int, *, max_age_s: int = 86400, max_rows: int = 5000
    ) -> Literal["ok", "resync"]:
        """Say whether a client that last saw `seq` can be sent the events after it.

        `resync` when `seq` is above the clock, its row is gone (pruned) or older than `max_age_s`,
        or more than `max_rows` events follow. An empty log accepts only `seq` 0.
        """
        threshold = self._clock() - timedelta(seconds=max_age_s)
        with db.read(self._engine) as conn:
            latest = int(
                conn.execute(select(event_clock.c.seq).where(event_clock.c.id == 1)).scalar_one()
            )
            if seq > latest:
                return "resync"
            if seq == latest == 0:
                return "ok"
            at = conn.execute(
                select(events_table.c.at).where(events_table.c.seq == seq)
            ).scalar_one_or_none()
            if at is None or at < threshold:
                return "resync"
            following = conn.execute(
                select(func.count()).select_from(events_table).where(events_table.c.seq > seq)
            ).scalar_one()
            return "resync" if following > max_rows else "ok"

    def prune(self, *, keep_s: int = 86400, max_consumer_lag_s: int = 604800) -> PruneReport:
        """Delete old events, keeping the newest of every entity and all a consumer still needs.

        The boundary is the first `seq` younger than `keep_s`, capped at the slowest consumer's
        `last_seq + 1`. A consumer whose next event is older than `max_consumer_lag_s` is first
        moved forward so that it next reads the first event inside that window. The move also clears
        its claim, so the old owner's `advance` and `renew` return False.
        """
        now = self._clock()
        with db.write(self._engine) as conn:
            latest = int(
                conn.execute(select(event_clock.c.seq).where(event_clock.c.id == 1)).scalar_one()
            )
            lag_start = _first_seq_since(conn, now - timedelta(seconds=max_consumer_lag_s), latest)
            moved: list[tuple[str, int, int, int]] = []
            for name, last_seq in conn.execute(
                select(consumers.c.name, consumers.c.last_seq).order_by(consumers.c.name)
            ).all():
                if last_seq + 1 < lag_start:
                    applied = conn.execute(
                        update(consumers)
                        .where(consumers.c.name == name, consumers.c.last_seq == last_seq)
                        .values(
                            last_seq=lag_start - 1,
                            updated_at=now,
                            claimed_by=None,
                            claimed_until=None,
                        )
                    ).rowcount
                    if applied:
                        skipped = _count_releases(conn, int(last_seq), lag_start - 1)
                        moved.append((name, int(last_seq), lag_start - 1, skipped))
            boundary = _first_seq_since(conn, now - timedelta(seconds=keep_s), latest)
            slowest = conn.execute(select(func.min(consumers.c.last_seq))).scalar_one()
            if slowest is not None:
                boundary = min(boundary, int(slowest) + 1)
            newer = events_table.alias("newer")
            has_newer = (
                select(newer.c.seq)
                .where(
                    newer.c.entity_id == events_table.c.entity_id, newer.c.seq > events_table.c.seq
                )
                .exists()
            )
            deleted = conn.execute(
                delete(events_table).where(events_table.c.seq < boundary, has_newer)
            ).rowcount
        return PruneReport(deleted=deleted, consumers_moved=moved)
