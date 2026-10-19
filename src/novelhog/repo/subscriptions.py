"""Notification subscriptions (spec sections 4, 5.6 and 6.6).

Subscriptions belong to no novel, so the novel lock order does not apply, and spec section 4
("Event log") lists no event for subscription writes: none is appended.
"""

import builtins

from sqlalchemy import delete, select

from novelhog import ids
from novelhog.domain.errors import NotFound
from novelhog.domain.payloads import SubscriptionView
from novelhog.domain.types import Subscription
from novelhog.repo import db
from novelhog.repo.context import RepoContext
from novelhog.repo.schema import subscriptions
from novelhog.repo.sql import dialect_insert


class Subscriptions:
    """Registers, lists and removes notification subscriptions of any channel."""

    def __init__(self, context: RepoContext) -> None:
        self._engine = context.engine
        self._clock = context.clock

    def upsert(
        self, channel: str, address: dict[str, object], address_key: str, label: str | None
    ) -> tuple[SubscriptionView, bool]:
        """Insert the subscription or replace the address and label of the existing one.

        An existing `(channel, address_key)` keeps its id and `created_at`. Returns the view and
        whether the row was created. A single `INSERT ... ON CONFLICT DO UPDATE ... RETURNING`
        makes this safe against concurrent callers: the row was created exactly when the
        returned id is the freshly generated one.
        """
        new_id = ids.new_id("SB")
        now = self._clock()
        statement = (
            dialect_insert(self._engine, subscriptions)
            .values(
                id=new_id,
                channel=channel,
                address=address,
                address_key=address_key,
                label=label,
                created_at=now,
            )
            .on_conflict_do_update(
                index_elements=["channel", "address_key"],
                set_={"address": address, "label": label},
            )
            .returning(subscriptions.c.id, subscriptions.c.created_at)
        )
        with db.write(self._engine) as conn:
            row = conn.execute(statement).one()
        view = SubscriptionView(id=row.id, channel=channel, label=label, created_at=row.created_at)
        return view, row.id == new_id

    def list(self) -> builtins.list[SubscriptionView]:
        """Return every subscription by `created_at`, without addresses."""
        statement = select(
            subscriptions.c.id,
            subscriptions.c.channel,
            subscriptions.c.label,
            subscriptions.c.created_at,
        ).order_by(subscriptions.c.created_at, subscriptions.c.id)
        with db.read(self._engine) as conn:
            return [
                SubscriptionView(id=r.id, channel=r.channel, label=r.label, created_at=r.created_at)
                for r in conn.execute(statement)
            ]

    def all_with_address(self) -> builtins.list[Subscription]:
        """Return every subscription including its address (for the worker only)."""
        statement = select(subscriptions).order_by(subscriptions.c.created_at, subscriptions.c.id)
        with db.read(self._engine) as conn:
            return [
                Subscription(
                    id=r.id,
                    channel=r.channel,
                    address=r.address,
                    address_key=r.address_key,
                    label=r.label,
                    created_at=r.created_at,
                )
                for r in conn.execute(statement)
            ]

    def delete(self, subscription_id: str) -> None:
        """Remove the subscription; raise `NotFound` for an unknown or malformed id."""
        if not ids.is_valid(subscription_id, "SB"):
            raise NotFound("subscription", subscription_id)
        with db.write(self._engine) as conn:
            result = conn.execute(
                delete(subscriptions).where(subscriptions.c.id == subscription_id)
            )
        if result.rowcount == 0:
            raise NotFound("subscription", subscription_id)
