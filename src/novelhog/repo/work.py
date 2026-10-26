"""v1 claim recovery that is not task work: the notifier's consumer claims (spec section 4).

Refreshes, downloads and builds are tasks; `Tasks.recover` recovers them (tasks spec section 8).
"""

from dataclasses import dataclass

from sqlalchemy import update

from novelhog.repo import db
from novelhog.repo.context import RepoContext
from novelhog.repo.schema import consumers


@dataclass(frozen=True)
class RecoveryReport:
    """What `recover` reset: consumers whose claim lapsed."""

    consumers_freed: int = 0


class Work:
    """Recovers the claims that are not tasks."""

    def __init__(self, context: RepoContext) -> None:
        self._engine = context.engine
        self._clock = context.clock

    def recover(self) -> RecoveryReport:
        """Free consumers whose claim lapsed (no events); their cursor stays where it was."""
        now = self._clock()
        with db.write(self._engine) as conn:
            freed = conn.execute(
                update(consumers)
                .where(consumers.c.claimed_by.is_not(None), consumers.c.claimed_until < now)
                .values(claimed_by=None, claimed_until=None)
            ).rowcount
        return RecoveryReport(consumers_freed=freed)
