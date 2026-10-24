"""Fixtures for API tests: an app on the migrated test database and a TestClient."""

from collections.abc import Callable, Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from novelhog import config
from novelhog.api.app import create_app
from novelhog.config import Settings
from novelhog.repo import Repo

PUBLIC_URL = "https://novels.example.org"


@pytest.fixture
def settings(tmp_path: Path, migrated_database: str) -> Settings:
    """Settings with push configured and one extra allowed host."""
    base = config.load({})
    return replace(
        base,
        data_dir=tmp_path,
        database_url=migrated_database,
        public_url=PUBLIC_URL,
        vapid_subject="mailto:me@example.org",
        allowed_hosts=("extra.test",),
    )


@pytest.fixture
def make_client(settings: Settings, repo: Repo) -> Iterator[Callable[..., TestClient]]:
    """Factory building a started TestClient; `overrides` replace fields of `settings`."""
    clients: list[TestClient] = []

    def build(*, app_hook: Callable[[FastAPI], None] | None = None, **overrides: Any) -> TestClient:
        app = create_app(replace(settings, **overrides), repo=repo)
        if app_hook is not None:
            app_hook(app)
        client = TestClient(app, base_url="http://localhost", raise_server_exceptions=False)
        client.__enter__()
        clients.append(client)
        return client

    yield build
    for client in clients:
        client.__exit__(None, None, None)


@pytest.fixture
def client(make_client: Callable[..., TestClient]) -> TestClient:
    """A started client on an app with push configured."""
    return make_client()
