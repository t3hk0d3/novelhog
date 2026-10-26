"""Column types shared by the schema (spec section 4, portability mapping)."""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import DateTime, String
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator, TypeEngine

_SQLITE_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"
_SQLITE_WIDTH = 27  # len("2026-10-19T07:41:10.123456Z")


def utcnow() -> datetime:
    """Return the current time as an aware UTC datetime."""
    return datetime.now(UTC)


class UTCDateTime(TypeDecorator[datetime]):
    """Aware UTC timestamps.

    Naive values are rejected and other zones converted to UTC. SQLite stores fixed-width
    `YYYY-MM-DDTHH:MM:SS.ffffffZ` text (so string order is time order); Postgres stores
    `timestamptz`. Values always come back as aware UTC datetimes.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def load_dialect_impl(self, dialect: Dialect) -> TypeEngine[Any]:
        """Use fixed-width text on SQLite and `timestamptz` elsewhere."""
        if dialect.name == "sqlite":
            return dialect.type_descriptor(String(_SQLITE_WIDTH))
        return dialect.type_descriptor(DateTime(timezone=True))

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> Any:
        """Validate and convert a datetime to the stored form."""
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            msg = "naive datetime is not allowed; use an aware datetime"
            raise ValueError(msg)
        value = value.astimezone(UTC)
        if dialect.name == "sqlite":
            return value.strftime(_SQLITE_FORMAT)
        return value

    def process_result_value(self, value: Any, dialect: Dialect) -> datetime | None:
        """Return the stored value as an aware UTC datetime."""
        if value is None:
            return None
        if isinstance(value, str):
            return datetime.strptime(value, _SQLITE_FORMAT).replace(tzinfo=UTC)  # noqa: DTZ007
        return value.astimezone(UTC)  # type: ignore[no-any-return]
