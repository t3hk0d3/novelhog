"""Compare-and-set claims, fencing and expiry for claimable rows (spec section 4, "Claims").

Every claimable table has `claimed_by` / `claimed_until`. A claim is one `UPDATE … RETURNING`
whose outer `WHERE` repeats the "free or expired" condition, so concurrent workers never claim the
same row; there is no `SELECT … FOR UPDATE`. Writes that finish claimed work are fenced by
`owned`. Timestamps are computed in Python and passed in; no arithmetic happens in SQL.
These helpers are internal to `repo`.
"""

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Column, ColumnElement, Connection, Row, Table, and_, exists, or_, select
from sqlalchemy.sql import operators
from sqlalchemy.sql.elements import UnaryExpression

from novelhog.repo.schema import chapters, consumers, exports, novels

CLAIMABLE: tuple[Table, ...] = (novels, chapters, exports, consumers)


def _key(table: Table) -> Column[Any]:
    columns = list(table.primary_key.columns)
    if len(columns) != 1:
        message = f"{table.name} needs a single-column primary key to be claimed"
        raise ValueError(message)
    return columns[0]


def available(table: Table, now: datetime) -> ColumnElement[bool]:
    """The row is free or its claim has expired."""
    return or_(table.c.claimed_by.is_(None), table.c.claimed_until < now)


def owned(table: Table, owner: str, now: datetime) -> ColumnElement[bool]:
    """The row is held by `owner` under a live claim."""
    return and_(table.c.claimed_by == owner, table.c.claimed_until >= now)


def _sort_column(expression: ColumnElement[Any]) -> tuple[Column[Any], bool]:
    """Return the plain column inside a priority expression and whether it sorts descending."""
    descending = False
    if isinstance(expression, UnaryExpression) and expression.modifier is not None:
        descending = expression.modifier is operators.desc_op
        expression = expression.element
    if not isinstance(expression, Column):
        message = "claim priority must be columns, optionally wrapped in asc() or desc()"
        raise TypeError(message)
    return expression, descending


def _reject_claim_columns(extra_set: Mapping[str, object]) -> None:
    reserved = sorted({"claimed_by", "claimed_until"} & extra_set.keys())
    if reserved:
        message = f"extra_set must not set {', '.join(reserved)}"
        raise ValueError(message)


def claim(
    conn: Connection,
    table: Table,
    *,
    eligible: ColumnElement[bool],
    priority: list[ColumnElement[Any]],
    owner: str,
    now: datetime,
    ttl: timedelta,
    n: int = 1,
    extra_set: Mapping[str, object] = {},  # noqa: B006  (read-only default)
    returning: list[Column[Any]],
    retries: int = 3,
) -> list[Row[Any]]:
    """Claim up to `n` eligible, available rows for `owner` and return them sorted by `priority`.

    `priority` holds plain columns, optionally wrapped in `asc()` / `desc()`, that are not NULL
    on eligible rows; a priority column missing from `returning` is added to the returned rows.
    A statement that claims fewer than `n` rows while eligible rows remain (a lost race) is
    retried up to `retries` times.
    """
    _reject_claim_columns(extra_set)
    key = _key(table)
    sort_keys = [_sort_column(expression) for expression in priority] + [(key, False)]
    columns = list(returning) + [c for c, _ in sort_keys if c not in returning]
    until = now + ttl
    free = available(table, now)
    claimed: list[Row[Any]] = []
    for _ in range(retries + 1):
        wanted = n - len(claimed)
        candidates = select(key).where(and_(eligible, free)).order_by(*priority, key).limit(wanted)
        statement = (
            table.update()
            .where(key.in_(candidates), free)
            .values({"claimed_by": owner, "claimed_until": until, **extra_set})
            .returning(*columns)
        )
        claimed.extend(conn.execute(statement).all())
        if len(claimed) >= n:
            break
        if not conn.execute(select(exists().where(and_(eligible, free)))).scalar_one():
            break
    for column, descending in reversed(sort_keys):
        claimed.sort(key=lambda row, c=column: row._mapping[c], reverse=descending)  # type: ignore[misc]
    return claimed


def renew(
    conn: Connection,
    table: Table,
    keys: Sequence[object],
    *,
    owner: str,
    now: datetime,
    ttl: timedelta,
) -> int:
    """Push `claimed_until` forward on the rows in `keys` that `owner` still holds."""
    if not keys:
        return 0
    statement = (
        table.update()
        .where(_key(table).in_(keys), owned(table, owner, now))
        .values(claimed_until=now + ttl)
    )
    return conn.execute(statement).rowcount


def release(
    conn: Connection,
    table: Table,
    keys: Sequence[object],
    *,
    owner: str,
    now: datetime,
    extra_set: Mapping[str, object] = {},  # noqa: B006  (read-only default)
) -> int:
    """Clear the claim on the rows in `keys` that `owner` still holds; apply `extra_set` to them."""
    _reject_claim_columns(extra_set)
    if not keys:
        return 0
    statement = (
        table.update()
        .where(_key(table).in_(keys), owned(table, owner, now))
        .values({"claimed_by": None, "claimed_until": None, **extra_set})
    )
    return conn.execute(statement).rowcount


def fenced_update(
    conn: Connection,
    table: Table,
    key: object,
    *,
    owner: str,
    now: datetime,
    values: Mapping[str, object],
) -> bool:
    """Apply `values` to the row `key` only if `owner` still holds it; report whether it applied."""
    statement = (
        table.update().where(_key(table) == key, owned(table, owner, now)).values(dict(values))
    )
    return conn.execute(statement).rowcount == 1


def expired(conn: Connection, table: Table, now: datetime) -> list[Row[Any]]:
    """Return the rows whose claim lapsed before `now`."""
    statement = select(table).where(table.c.claimed_by.is_not(None), table.c.claimed_until < now)
    return list(conn.execute(statement).all())
