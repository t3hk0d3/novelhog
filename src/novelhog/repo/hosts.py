"""Hosts the installed scraper supports (spec sections 4 and 5.2), and their reach state.

`reach_after` says when a host may be contacted again. `reserve` takes the next slot before a
fetch (politeness shared by every worker); `failed` counts a failure and pauses the host by the
policy in `domain.hosts`; `succeeded` clears the failure count. Each is a single-row write in its
own transaction, outside the novel lock order, like a task claim.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import Connection, exists, func, literal, or_, select, update

from novelhog.domain.hosts import FailureKind, pause_for
from novelhog.repo import db
from novelhog.repo.context import RepoContext
from novelhog.repo.schema import hosts as hosts_table
from novelhog.repo.sql import dialect_insert
from novelhog.repo.types import UTCDateTime


@dataclass(frozen=True)
class Host:
    """A supported host; `reach_after` and `failing_since` are None for a healthy, idle host."""

    host: str
    reach_after: datetime | None = None
    failing_since: datetime | None = None


@dataclass(frozen=True)
class HostState:
    """A host after a failure: `paused_until` is None while it is not paused."""

    paused_until: datetime | None
    failures: int


@dataclass(frozen=True)
class PublishReport:
    """`added`: hosts that were not supported before; `unsupported`: hosts newly marked dropped."""

    added: list[str]
    unsupported: list[str]


def supported_hosts(conn: Connection) -> list[str]:
    """Return the supported hosts, sorted."""
    statement = (
        select(hosts_table.c.host).where(hosts_table.c.supported).order_by(hosts_table.c.host)
    )
    return list(conn.execute(statement).scalars())


def any_published(conn: Connection) -> bool:
    """Report whether the table holds any row."""
    return bool(conn.execute(select(exists(select(hosts_table.c.host)))).scalar_one())


class Hosts:
    """Publishes and reads the supported hosts."""

    def __init__(self, context: RepoContext) -> None:
        self._engine = context.engine
        self._clock = context.clock

    def publish(self, hosts: list[str]) -> PublishReport:
        """Upsert `hosts` as supported and mark every other host unsupported; never delete."""
        wanted = list(dict.fromkeys(hosts))
        now = self._clock()
        with db.write(self._engine) as conn:
            known = {row.host: row.supported for row in conn.execute(select(hosts_table)).all()}
            added = [h for h in wanted if not known.get(h, False)]
            dropped = sorted(h for h, supported in known.items() if supported and h not in wanted)
            if wanted:
                upsert = dialect_insert(self._engine, hosts_table)
                conn.execute(
                    upsert.values(
                        [{"host": h, "supported": True, "updated_at": now} for h in wanted]
                    ).on_conflict_do_update(
                        index_elements=["host"],
                        set_={"supported": True, "updated_at": now},
                        where=hosts_table.c.supported.is_(False),
                    )
                )
            if dropped:
                conn.execute(
                    update(hosts_table)
                    .where(hosts_table.c.host.in_(dropped))
                    .values(supported=False, updated_at=now)
                )
        return PublishReport(added=added, unsupported=dropped)

    def list_supported(self) -> list[Host]:
        """Return the supported hosts with their reach state, sorted."""
        statement = (
            select(hosts_table.c.host, hosts_table.c.reach_after, hosts_table.c.failing_since)
            .where(hosts_table.c.supported)
            .order_by(hosts_table.c.host)
        )
        with db.read(self._engine) as conn:
            return [Host(r.host, r.reach_after, r.failing_since) for r in conn.execute(statement)]

    def reserve(self, host: str, delay_s: float) -> datetime | None:
        """Take the host's next slot: `reach_after = now + delay_s` if it is free.

        Returns None when the slot was taken, else the `reach_after` the caller must wait for.
        """
        now = self._clock()
        free = or_(hosts_table.c.reach_after.is_(None), hosts_table.c.reach_after <= now)
        with db.write(self._engine) as conn:
            taken = conn.execute(
                update(hosts_table)
                .where(hosts_table.c.host == host, free)
                .values(reach_after=now + timedelta(seconds=delay_s))
                .returning(hosts_table.c.host)
            ).first()
            if taken is not None:
                return None
            reach_after: datetime | None = conn.execute(
                select(hosts_table.c.reach_after).where(hosts_table.c.host == host)
            ).scalar_one_or_none()
        return reach_after

    def failed(self, host: str, kind: FailureKind, *, retry_after: float | None) -> HostState:
        """Count one more failure of `kind` and pause the host if the policy says so.

        A pause never moves `reach_after` earlier. Only kinds that can pause a host are accepted:
        a transient or permanent failure leaves the host alone.
        """
        if pause_for(kind, 1_000, retry_after=None) is None:
            msg = f"a {kind.value} failure does not count against the host"
            raise ValueError(msg)
        now = self._clock()
        with db.write(self._engine) as conn:
            row = conn.execute(
                update(hosts_table)
                .where(hosts_table.c.host == host)
                .values(
                    failures=hosts_table.c.failures + 1,
                    failing_since=func.coalesce(
                        hosts_table.c.failing_since, literal(now, UTCDateTime())
                    ),
                    last_failure=kind.value,
                )
                .returning(hosts_table.c.failures, hosts_table.c.reach_after)
            ).one()
            failures, reach_after = int(row.failures), row.reach_after
            pause = pause_for(kind, failures, retry_after=retry_after)
            if pause is None:
                return HostState(paused_until=None, failures=failures)
            until = now + pause
            if reach_after is None or reach_after < until:
                conn.execute(
                    update(hosts_table).where(hosts_table.c.host == host).values(reach_after=until)
                )
                reach_after = until
        return HostState(paused_until=reach_after, failures=failures)

    def succeeded(self, host: str) -> None:
        """The host answered: clear its failure count; the `reach_after` slot stays."""
        with db.write(self._engine) as conn:
            conn.execute(
                update(hosts_table)
                .where(hosts_table.c.host == host, hosts_table.c.failures > 0)
                .values(failures=0, failing_since=None, last_failure=None)
            )

    def is_supported(self, host: str) -> bool:
        """Report whether `host` is currently supported."""
        with db.read(self._engine) as conn:
            statement = select(exists().where(hosts_table.c.host == host, hosts_table.c.supported))
            return bool(conn.execute(statement).scalar_one())

    def any_published(self) -> bool:
        """Report whether the worker has published hosts yet."""
        with db.read(self._engine) as conn:
            return any_published(conn)
