"""Hosts the installed scraper supports (spec sections 4 and 5.2)."""

from dataclasses import dataclass

from sqlalchemy import Connection, exists, select, update

from novelhog.repo import db
from novelhog.repo.context import RepoContext
from novelhog.repo.schema import hosts as hosts_table
from novelhog.repo.sql import dialect_insert


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
