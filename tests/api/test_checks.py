"""Tests for the host and origin checks."""

from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient

URL = "/api/v1/settings"


def _get(client: TestClient, host: str) -> int:
    return int(client.get(URL, headers={"Host": host}).status_code)


class TestHostCheck__WithAcceptedHost:
    @pytest.mark.parametrize(
        "host",
        [
            "localhost",
            "localhost:8080",
            "127.0.0.1",
            "10.1.2.3:8080",
            "[::1]:8080",
            "novels.example.org",
            "novels.example.org:443",
            "extra.test",
            "EXTRA.test:81",
        ],
    )
    def test_host_check__passes(self, client: TestClient, host: str) -> None:
        assert _get(client, host) == 200


class TestHostCheck__WithOtherHost:
    @pytest.mark.parametrize(
        "host", ["evil.example.com", "evil.example.com:8080", "testserver", ""]
    )
    def test_host_check__returns_403_host_not_allowed(self, client: TestClient, host: str) -> None:
        response = client.get(URL, headers={"Host": host})
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "host_not_allowed"


class TestOriginCheck__WithUnsafeMethod:
    @pytest.mark.parametrize("method", ["POST", "PATCH", "DELETE"])
    def test_origin_check__passes_when_origin_matches_host(
        self, client: TestClient, method: str
    ) -> None:
        response = client.request(
            method, "/nope", headers={"Host": "localhost:8080", "Origin": "http://localhost:8080"}
        )
        assert response.status_code == 404

    @pytest.mark.parametrize("method", ["POST", "PATCH", "DELETE"])
    @pytest.mark.parametrize(
        "origin",
        ["http://evil.example.com", "http://localhost:9999", "null", "http://localhost"],
    )
    def test_origin_check__returns_403_cross_origin(
        self, client: TestClient, method: str, origin: str
    ) -> None:
        response = client.request(
            method, "/nope", headers={"Host": "localhost:8080", "Origin": origin}
        )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "cross_origin"

    def test_origin_check__passes_default_port_origin_behind_proxy(
        self, client: TestClient
    ) -> None:
        response = client.post(
            "/nope", headers={"Host": "novels.example.org", "Origin": "https://novels.example.org"}
        )
        assert response.status_code == 404

    def test_origin_check__passes_without_origin(self, client: TestClient) -> None:
        assert client.post("/nope").status_code == 404


class TestOriginCheck__WithGet:
    def test_origin_check__passes_with_different_origin(self, client: TestClient) -> None:
        response = client.get(URL, headers={"Origin": "http://evil.example.com"})
        assert response.status_code == 200

    def test_origin_check__passes_without_origin(self, client: TestClient) -> None:
        assert client.get(URL).status_code == 200


def test_host_check__allows_public_host_only_when_configured(
    make_client: Callable[..., TestClient],
) -> None:
    client = make_client(public_url=None, allowed_hosts=())
    assert _get(client, "novels.example.org") == 403
