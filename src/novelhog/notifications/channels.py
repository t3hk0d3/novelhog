"""Notification channels: Web Push over the outbound guard (spec 6.6, 8.6)."""

import json
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
            {"title": message.title, "body": message.body, "url": message.url, "icon": message.icon}
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
        except (requests.RequestException, Refused) as error:
            return Failed(f"{type(error).__name__}: {error}")
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
        log.get().warning("webpush_keys_missing")
        return registry
    registry["webpush"] = WebPushChannel(keys, settings.vapid_subject, session)
    return registry
