"""The channel registry (spec 6.6)."""

import requests

from novelhog import config, vapid
from novelhog.notifications.channels import build_registry
from novelhog.repo import Repo

SESSION = requests.Session()
ON = config.load(
    {"NOVELHOG_PUBLIC_URL": "https://n.example", "NOVELHOG_VAPID_SUBJECT": "mailto:me@example.com"}
)


class TestBuildRegistry:
    def test_build_registry__registers_webpush_when_configured_and_keys_exist(
        self, repo: Repo
    ) -> None:
        vapid.ensure_keys(repo)
        registry = build_registry(ON, repo, SESSION)
        assert list(registry) == ["webpush"]
        assert registry["webpush"].name == "webpush"

    def test_build_registry__registers_nothing_without_keys(self, repo: Repo) -> None:
        assert build_registry(ON, repo, SESSION) == {}

    def test_build_registry__registers_nothing_when_push_is_not_configured(
        self, repo: Repo
    ) -> None:
        vapid.ensure_keys(repo)
        assert build_registry(config.load({}), repo, SESSION) == {}
