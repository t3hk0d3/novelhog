"""Tests for GET /api/v1/settings."""

from collections.abc import Callable

from fastapi.testclient import TestClient

from novelhog import vapid
from novelhog.config import Settings
from novelhog.repo import Repo


class TestGetSettings__WithPushConfigured:
    def test_get_settings__reports_webpush_enabled_with_public_key(
        self, client: TestClient, repo: Repo
    ) -> None:
        keys = vapid.load(repo)
        assert keys is not None
        body = client.get("/api/v1/settings").json()
        assert body["notifications"]["channels"]["webpush"] == {
            "enabled": True,
            "vapid_public_key": keys.public_key,
        }

    def test_get_settings__never_contains_private_key(self, client: TestClient, repo: Repo) -> None:
        keys = vapid.load(repo)
        assert keys is not None
        assert keys.private_key not in client.get("/api/v1/settings").text


class TestGetSettings__WithoutPushConfigured:
    def test_get_settings__reports_webpush_disabled(
        self, make_client: Callable[..., TestClient]
    ) -> None:
        body = make_client(public_url=None).get("/api/v1/settings").json()
        assert body["notifications"]["channels"]["webpush"] == {
            "enabled": False,
            "vapid_public_key": None,
        }


class TestGetSettings:
    def test_get_settings__includes_static_base_url_and_refresh_intervals(
        self, client: TestClient, settings: Settings
    ) -> None:
        body = client.get("/api/v1/settings").json()
        assert body["static_base_url"] == settings.static_base_url
        assert body["refresh_min_interval"] == settings.refresh_min_interval
        assert body["refresh_max_interval"] == settings.refresh_max_interval
