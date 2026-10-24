"""Plain value types shared by `repo`, the API and the worker."""

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Host:
    """A host known to the scraper and whether it is supported."""

    host: str
    supported: bool


@dataclass(frozen=True)
class Subscription:
    """A notification subscription; `address` is channel-specific and never sent to clients."""

    id: str
    channel: str
    address: dict[str, object]
    address_key: str
    label: str | None
    created_at: datetime


@dataclass(frozen=True)
class VapidKeys:
    """A Web Push VAPID key pair."""

    public_key: str
    private_key: str
