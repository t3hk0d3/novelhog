"""Notification channels: Web Push over the outbound guard (spec 6.6, 8.6)."""

import json
import traceback
from dataclasses import dataclass
from typing import Protocol

import requests
from py_vapid import Vapid
from pywebpush import WebPushException, webpush

from novelhog import log, vapid
from novelhog.config import Settings
from novelhog.domain.types import Subscription
from novelhog.net import Refused
from novelhog.notifications.rules import NotificationMessage
from novelhog.repo import Repo
from novelhog.vapid import VapidKeys

TTL_S = 86400
TIMEOUT_S = 10
MAX_TITLE_CHARS = 100
MAX_BODY_CHARS = 200
_GONE = (404, 410)


@dataclass(frozen=True)
class Delivered:
    """The push service accepted the message."""


@dataclass(frozen=True)
class Gone:
    """The endpoint no longer exists; the subscription should be deleted."""


@dataclass(frozen=True)
class Failed:
    """Delivery failed for a reason worth logging; there is no retry."""

    reason: str


SendResult = Delivered | Gone | Failed


class NotificationChannel(Protocol):
    """One delivery service; the registry maps `name` to the channel."""

    name: str

    def send(self, subscription: Subscription, message: NotificationMessage) -> SendResult: ...


def _clip(text: str, limit: int) -> str:
    """Shorten `text` to `limit` characters so the encrypted payload stays below 4 KB."""
    return text if len(text) <= limit else text[: limit - 1] + "\u2026"


class WebPushChannel:
    """Sends the message as a JSON Web Push payload signed with the stored VAPID key pair."""

    name = "webpush"

    def __init__(self, vapid: VapidKeys, subject: str, session: requests.Session) -> None:
        self._vapid = vapid
        self._subject = subject
        self._session = session

    def send(self, subscription: Subscription, message: NotificationMessage) -> SendResult:
        """Deliver `message`; 404 and 410 mean the subscription is gone."""
        payload = json.dumps(
            {
                "title": _clip(message.title, MAX_TITLE_CHARS),
                "body": _clip(message.body, MAX_BODY_CHARS),
                "url": message.url,
                "icon": message.icon,
            },
            ensure_ascii=False,
        )
        try:
            webpush(
                subscription_info=subscription.address,
                data=payload,
                vapid_private_key=Vapid.from_raw(self._vapid.private_key.encode("ascii")),
                vapid_claims={"sub": self._subject},
                ttl=TTL_S,
                timeout=TIMEOUT_S,
                headers={"Urgency": "normal", "Topic": message.topic},
                requests_session=self._session,
            )
        except WebPushException as error:
            status = getattr(error.response, "status_code", None)
            if status in _GONE:
                return Gone()
            return Failed(f"push service answered {status}" if status else str(error.message))
        except Refused as error:
            return Failed(f"Refused: {error}")
        except Exception as error:  # noqa: BLE001 - one bad subscription must not stop the rest
            # Only the type is reported: messages of request errors carry the endpoint URL and
            # messages of key-decoding errors may carry key material.
            # The traceback frames are logged without the message, which may hold key material.
            log.get().warning(
                "webpush_unexpected_error",
                error_type=type(error).__name__,
                frames="".join(traceback.format_tb(error.__traceback__)),
            )
            return Failed(f"{type(error).__name__} while sending")
        return Delivered()


def build_registry(
    settings: Settings, repo: Repo, session: requests.Session
) -> dict[str, NotificationChannel]:
    """Return the channels that can run now: `webpush` needs push settings and VAPID keys."""
    registry: dict[str, NotificationChannel] = {}
    if settings.vapid_subject is None or not settings.webpush_enabled:
        return registry
    keys = vapid.load(repo)
    if keys is None:
        log.get().info("webpush_keys_missing")
        return registry
    registry["webpush"] = WebPushChannel(keys, settings.vapid_subject, session)
    return registry


class RegistryHolder:
    """Hands out the channel registry, rebuilding it while push is configured but not registered.

    The worker may start before the API has created the VAPID keys; checking again on each
    `get()` (one `settings` SELECT while `webpush` is missing) picks the keys up without a restart.
    """

    def __init__(self, settings: Settings, repo: Repo, session: requests.Session) -> None:
        self._settings = settings
        self._repo = repo
        self._session = session
        self._registry: dict[str, NotificationChannel] = build_registry(settings, repo, session)

    def get(self) -> dict[str, NotificationChannel]:
        """Return the current registry."""
        if self._settings.webpush_enabled and "webpush" not in self._registry:
            self._registry = build_registry(self._settings, self._repo, self._session)
        return self._registry
