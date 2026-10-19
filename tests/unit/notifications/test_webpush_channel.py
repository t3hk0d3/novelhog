"""The Web Push channel (pywebpush monkeypatched)."""

import json
from datetime import UTC, datetime
from typing import Any

import pytest
import requests
from py_vapid import Vapid
from pywebpush import WebPushException

from novelhog import vapid
from novelhog.domain.types import Subscription
from novelhog.net import Refused
from novelhog.notifications import channels
from novelhog.notifications.channels import (
    Delivered,
    Failed,
    Gone,
    WebPushChannel,
)
from novelhog.notifications.rules import NotificationMessage

KEYS = vapid._generate()  # noqa: SLF001 - a real key pair for the signing check
SESSION = requests.Session()
SUBSCRIPTION = Subscription(
    id="SB000000000001",
    channel="webpush",
    address={"endpoint": "https://push.example/a", "keys": {"p256dh": "p", "auth": "a"}},
    address_key="https://push.example/a",
    label=None,
    created_at=datetime(2026, 10, 19, tzinfo=UTC),
)
MESSAGE = NotificationMessage(
    "T", "B", "https://n.example/novels/NV1", "https://n.example/i.png", "NV1"
)


class _Response:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def fake(**kwargs: Any) -> _Response:
        calls.append(kwargs)
        return _Response(201)

    monkeypatch.setattr(channels, "webpush", fake)
    return calls


def _raising(monkeypatch: pytest.MonkeyPatch, error: Exception) -> None:
    def fake(**_kwargs: Any) -> None:
        raise error

    monkeypatch.setattr(channels, "webpush", fake)


def _channel() -> WebPushChannel:
    return WebPushChannel(KEYS, "mailto:me@example.com", SESSION)


class TestSend__OnSuccess:
    def test_send__returns_delivered_with_the_json_payload_and_push_options(
        self, sent: list[dict[str, Any]]
    ) -> None:
        assert _channel().send(SUBSCRIPTION, MESSAGE) == Delivered()
        (call,) = sent
        assert call["subscription_info"] == SUBSCRIPTION.address
        assert json.loads(call["data"]) == {
            "title": "T",
            "body": "B",
            "url": MESSAGE.url,
            "icon": MESSAGE.icon,
        }
        assert call["vapid_claims"] == {"sub": "mailto:me@example.com"}
        assert call["ttl"] == 86400
        assert call["timeout"] == 10
        assert call["headers"] == {"Urgency": "normal", "Topic": "NV1"}
        assert call["requests_session"] is SESSION

    def test_send__signs_with_the_stored_key_pair(self, sent: list[dict[str, Any]]) -> None:
        _channel().send(SUBSCRIPTION, MESSAGE)
        signer = sent[0]["vapid_private_key"]
        assert signer.public_key.public_numbers() == (
            Vapid.from_raw_public(KEYS.public_key.encode()).public_key.public_numbers()
        )


class TestSend__OnGone:
    @pytest.mark.parametrize("status", [404, 410])
    def test_send__returns_gone(self, monkeypatch: pytest.MonkeyPatch, status: int) -> None:
        _raising(monkeypatch, WebPushException("gone", response=_Response(status)))
        assert _channel().send(SUBSCRIPTION, MESSAGE) == Gone()


class TestSend__OnFailure:
    def test_send__returns_failed_for_another_status(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _raising(monkeypatch, WebPushException("boom", response=_Response(500)))
        result = _channel().send(SUBSCRIPTION, MESSAGE)
        assert result == Failed("push service answered 500")

    def test_send__returns_failed_for_a_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _raising(monkeypatch, requests.Timeout("slow"))
        result = _channel().send(SUBSCRIPTION, MESSAGE)
        assert isinstance(result, Failed)
        assert "Timeout" in result.reason

    def test_send__returns_failed_for_a_refused_address(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _raising(monkeypatch, Refused("private"))
        assert _channel().send(SUBSCRIPTION, MESSAGE) == Failed("Refused: private")
