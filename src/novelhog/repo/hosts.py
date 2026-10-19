"""Hosts the installed scraper supports (spec sections 4 and 5.2)."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Connection, Engine, exists, insert, select, update

from novelhog.repo import db
from novelhog.repo.schema import hosts as hosts_table


@dataclass(frozen=True)
class Host:
    """A supported host."""

    host: str


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
    return bool(conn.execute(select(exists().where(hosts_table.c.host.is_not(None)))).scalar_one())


class Hosts:
    """Publishes and reads the supported hosts."""

    def __init__(self, engine: Engine, clock: Callable[[], datetime]) -> None:
        self._engine = engine
        self._clock = clock

    def publish(self, hosts: list[str]) -> PublishReport:
        """Upsert `hosts` as supported and mark every other host unsupported; never delete."""
        wanted = list(dict.fromkeys(hosts))
        now = self._clock()
        with db.write(self._engine) as conn:
            known = {row.host: row.supported for row in conn.execute(select(hosts_table)).all()}
            added = [h for h in wanted if not known.get(h, False)]
            dropped = sorted(h for h, supported in known.items() if supported and h not in wanted)
            for host in wanted:
                if host not in known:
                    conn.execute(
                        insert(hosts_table).values(host=host, supported=True, updated_at=now)
                    )
                elif not known[host]:
                    conn.execute(
                        update(hosts_table)
                        .where(hosts_table.c.host == host)
                        .values(supported=True, updated_at=now)
                    )
            if dropped:
                conn.execute(
                    update(hosts_table)
                    .where(hosts_table.c.host.in_(dropped))
                    .values(supported=False, updated_at=now)
                )
        return PublishReport(added=added, unsupported=dropped)

    def list_supported(self) -> list[Host]:
        """Return the supported hosts."""
        with db.read(self._engine) as conn:
            return [Host(host) for host in supported_hosts(conn)]

    def is_supported(self, host: str) -> bool:
        """Report whether `host` is currently supported."""
        with db.read(self._engine) as conn:
            statement = select(exists().where(hosts_table.c.host == host, hosts_table.c.supported))
            return bool(conn.execute(statement).scalar_one())

    def any_published(self) -> bool:
        """Report whether the worker has published hosts yet."""
        with db.read(self._engine) as conn:
            return any_published(conn)
