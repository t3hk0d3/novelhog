"""Tests for the subscriptions routes."""

import base64
import os
from collections.abc import Callable

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient

from novelhog.repo import Repo

URL = "/api/v1/webpush/subscriptions"
ENDPOINT = "https://fcm.googleapis.com/fcm/send/abc"


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _keys() -> dict[str, str]:
    point = (
        ec.generate_private_key(ec.SECP256R1())
        .public_key()
        .public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    )
    return {"p256dh": _b64(point), "auth": _b64(os.urandom(16))}


def _body(endpoint: str = ENDPOINT) -> dict[str, object]:
    return {"endpoint": endpoint, "keys": _keys()}


def _assert_error(response: object, status: int, code: str) -> None:
    assert getattr(response, "status_code") == status  # noqa: B009
    assert getattr(response, "json")()["error"]["code"] == code  # noqa: B009


class TestCreateWebpushSubscription__WithANewKnownEndpoint:
    def test_create__returns_201_with_label_from_user_agent(self, client: TestClient) -> None:
        response = client.post(URL, json=_body(), headers={"User-Agent": "Firefox/200"})
        assert response.status_code == 201
        body = response.json()
        assert body["channel"] == "webpush"
        assert body["label"] == "Firefox/200"
        assert body["id"].startswith("SB")
        assert "endpoint" not in body

    @pytest.mark.parametrize(
        "endpoint",
        [
            "https://updates.push.services.mozilla.com/wpush/v2/x",
            "https://web.push.apple.com/x",
            "https://api.push.apple.com/x",
            "https://abc.notify.windows.com/x",
        ],
    )
    def test_create__accepts_every_known_push_service(
        self, client: TestClient, endpoint: str
    ) -> None:
        assert client.post(URL, json=_body(endpoint)).status_code == 201


class TestCreateWebpushSubscription__WithAnExistingEndpoint:
    def test_create__returns_200_with_the_same_id(self, client: TestClient) -> None:
        first = client.post(URL, json=_body())
        second = client.post(URL, json=_body())
        assert second.status_code == 200
        assert second.json()["id"] == first.json()["id"]


class TestCreateWebpushSubscription__WithAnInvalidEndpoint:
    @pytest.mark.parametrize(
        "endpoint",
        [
            "http://fcm.googleapis.com/x",
            "https://example.com/x",
            "https://evil.com/?fcm.googleapis.com",
            "https://fcm.googleapis.com@evil.com/x",
            "https://notify.windows.com.evil.com/x",
            "https://fcm.googleapis.com:8443/x",
            "not a url",
        ],
    )
    def test_create__returns_422_invalid_endpoint(self, client: TestClient, endpoint: str) -> None:
        _assert_error(client.post(URL, json=_body(endpoint)), 422, "invalid_endpoint")


class TestCreateWebpushSubscription__WithBadKeys:
    def test_create__missing_keys_returns_422_validation_error(self, client: TestClient) -> None:
        _assert_error(client.post(URL, json={"endpoint": ENDPOINT}), 422, "validation_error")

    def test_create__missing_auth_returns_422_validation_error(self, client: TestClient) -> None:
        body = {"endpoint": ENDPOINT, "keys": {"p256dh": _keys()["p256dh"]}}
        _assert_error(client.post(URL, json=body), 422, "validation_error")

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("p256dh", "not+url/safe=="),
            ("p256dh", "!!!"),
            ("p256dh", _b64(b"\x04" + os.urandom(10))),
            ("p256dh", _b64(b"\x02" + os.urandom(64))),
            ("auth", "a+b/"),
            ("auth", _b64(os.urandom(15))),
        ],
    )
    def test_create__malformed_key_returns_422_validation_error(
        self, client: TestClient, field: str, value: str
    ) -> None:
        keys = {**_keys(), field: value}
        _assert_error(
            client.post(URL, json={"endpoint": ENDPOINT, "keys": keys}), 422, "validation_error"
        )


class TestCreateWebpushSubscription__WithPushDisabled:
    def test_create__returns_503_channel_disabled(
        self, make_client: Callable[..., TestClient]
    ) -> None:
        client = make_client(vapid_subject=None, public_url=None)
        _assert_error(client.post(URL, json=_body()), 503, "channel_disabled")


class TestListSubscriptions:
    def test_list__returns_every_channels_subscriptions_without_addresses(
        self, client: TestClient, repo: Repo
    ) -> None:
        repo.subscriptions.upsert("telegram", {"chat_id": 1}, "1", "chat")
        client.post(URL, json=_body())
        body = client.get("/api/v1/subscriptions").json()
        assert {s["channel"] for s in body} == {"telegram", "webpush"}
        assert all(set(s) == {"id", "channel", "label", "created_at"} for s in body)


class TestDeleteSubscription:
    def test_delete__returns_204_and_removes_it(self, client: TestClient) -> None:
        sub_id = client.post(URL, json=_body()).json()["id"]
        assert client.delete(f"/api/v1/subscriptions/{sub_id}").status_code == 204
        assert client.get("/api/v1/subscriptions").json() == []

    def test_delete__unknown_id_returns_404(self, client: TestClient) -> None:
        _assert_error(client.delete("/api/v1/subscriptions/SB000000000000"), 404, "not_found")
        _assert_error(client.delete("/api/v1/subscriptions/nonsense"), 404, "not_found")
