"""Tests for create_app startup and export_openapi."""

import json
import subprocess
import sys
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from novelhog import vapid
from novelhog.api.app import create_app
from novelhog.config import Settings
from novelhog.repo import Repo, SchemaOutdated


class TestStartup__OnDatabaseAtHead:
    def test_startup__creates_keys_when_push_configured(
        self, settings: Settings, repo: Repo
    ) -> None:
        with TestClient(create_app(settings, repo=repo)):
            assert vapid.load(repo) is not None

    def test_startup__creates_no_keys_without_push(self, settings: Settings, repo: Repo) -> None:
        with TestClient(create_app(replace(settings, public_url=None), repo=repo)):
            assert vapid.load(repo) is None


class TestStartup__OnOutdatedDatabase:
    def test_startup__raises_schema_outdated(
        self, settings: Settings, empty_database_url: str
    ) -> None:
        stale = Repo(empty_database_url)
        try:
            with pytest.raises(SchemaOutdated), TestClient(create_app(settings, repo=stale)):
                pass
        finally:
            stale.engine.dispose()


class TestStartup__WithoutDatabase:
    def test_export_openapi__prints_document_with_single_schema_per_name(self) -> None:
        result = subprocess.run(  # noqa: S603
            [sys.executable, "-m", "novelhog.api.export_openapi"],
            capture_output=True,
            text=True,
            check=True,
        )
        document = json.loads(result.stdout)
        schemas = document["components"]["schemas"]
        assert "/api/v1/settings" in document["paths"]
        assert {"SettingsOut", "ErrorBody"} <= set(schemas)
        assert not any(name.endswith(("-Input", "-Output")) for name in schemas)

    def test_create_app__builds_with_check_schema_off(self, settings: Settings) -> None:
        app = create_app(settings, check_schema=False)
        assert app.openapi()["info"]["title"] == "novelhog"
