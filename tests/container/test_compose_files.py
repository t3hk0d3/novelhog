"""Contract of the two Compose files: `compose.yaml` pulls the published image for end users and
`compose.dev.yaml` builds from source for development.

`docker compose config` starts nothing, so these run wherever the docker CLI exists and need no
`NOVELHOG_CONTAINER_TESTS=1`.
"""

import json
import os
import shutil
import subprocess  # noqa: S404 - the tests drive the docker CLI
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.skipif(
    shutil.which("docker") is None, reason="install Docker to check the Compose files"
)

ROOT = Path(__file__).resolve().parents[2]
IMAGE = "ghcr.io/t3hk0d3/novelhog"
NOVELHOG_SERVICES = ["novelhog-migrate", "novelhog-web", "novelhog-worker"]
SERVICES = [*NOVELHOG_SERVICES, "flaresolverr"]


def _services(compose_file: str, version: str | None = None) -> dict[str, Any]:
    """The resolved services of `compose_file`, with `NOVELHOG_VERSION` set or unset."""
    env = {k: v for k, v in os.environ.items() if k != "NOVELHOG_VERSION"}
    if version is not None:
        env["NOVELHOG_VERSION"] = version
    config = subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["docker", "compose", "-f", compose_file, "config", "--format", "json"],  # noqa: S607
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    services: dict[str, Any] = json.loads(config.stdout)["services"]
    return services


class TestComposeYaml__Parsed:
    def test_compose_yaml__defines_every_service(self) -> None:
        assert sorted(_services("compose.yaml")) == sorted(SERVICES)

    def test_compose_yaml__novelhog_services_use_the_published_image(self) -> None:
        services = _services("compose.yaml")
        assert all(services[name]["image"].startswith(f"{IMAGE}:") for name in NOVELHOG_SERVICES)

    def test_compose_yaml__no_service_builds(self) -> None:
        services = _services("compose.yaml")
        assert [name for name, s in services.items() if "build" in s] == []

    def test_compose_yaml__no_service_watches(self) -> None:
        services = _services("compose.yaml")
        assert [name for name, s in services.items() if "develop" in s] == []


class TestComposeYaml__WithVersion:
    def test_compose_yaml__novelhog_services_use_that_tag(self) -> None:
        services = _services("compose.yaml", version="1.2.3")
        assert {services[name]["image"] for name in NOVELHOG_SERVICES} == {f"{IMAGE}:1.2.3"}


class TestComposeYaml__WithoutVersion:
    def test_compose_yaml__novelhog_services_use_latest(self) -> None:
        services = _services("compose.yaml")
        assert {services[name]["image"] for name in NOVELHOG_SERVICES} == {f"{IMAGE}:latest"}


class TestComposeYaml__NetworkLayout:
    def test_compose_yaml__worker_and_flaresolverr_share_a_network_web_does_not_join(self) -> None:
        services = _services("compose.yaml")
        shared = set(services["novelhog-worker"]["networks"]) & set(
            services["flaresolverr"]["networks"]
        )
        assert shared
        assert shared.isdisjoint(services["novelhog-web"]["networks"])


class TestComposeDevYaml__Parsed:
    def test_compose_dev_yaml__novelhog_services_build_the_dockerfile(self) -> None:
        services = _services("compose.dev.yaml")
        dockerfiles = {services[name]["build"]["dockerfile"] for name in NOVELHOG_SERVICES}
        assert dockerfiles == {"docker/Dockerfile"}

    def test_compose_dev_yaml__web_and_worker_have_watch_rules(self) -> None:
        services = _services("compose.dev.yaml")
        assert all(
            services[name]["develop"]["watch"] for name in ["novelhog-web", "novelhog-worker"]
        )
