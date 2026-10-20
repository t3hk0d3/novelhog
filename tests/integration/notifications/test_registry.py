"""The channel registry (spec 6.6)."""

import requests

from novelhog import config, vapid
from novelhog.notifications.channels import RegistryHolder, build_registry
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


class TestRegistryHolder:
    def test_get__picks_up_keys_stored_after_the_holder_was_built(self, repo: Repo) -> None:
        holder = RegistryHolder(ON, repo, SESSION)
        assert holder.get() == {}
        vapid.ensure_keys(repo)
        assert list(holder.get()) == ["webpush"]

    def test_get__keeps_the_registry_once_webpush_is_registered(self, repo: Repo) -> None:
        vapid.ensure_keys(repo)
        holder = RegistryHolder(ON, repo, SESSION)
        assert holder.get() is holder.get()

    def test_get__stays_empty_when_push_is_not_configured(self, repo: Repo) -> None:
        vapid.ensure_keys(repo)
        assert RegistryHolder(config.load({}), repo, SESSION).get() == {}
