"""The add-on image, built on the main image and run as Home Assistant runs it.

Skipped unless Docker is available and `NOVELHOG_CONTAINER_TESTS=1`, like the other container tests.
Each container gets a fresh named volume at `/data` (root-owned, as the Supervisor's is) and, when
the test gives options, an `/data/options.json` copied in before it starts.
"""

import http.client
import os
import shutil
import subprocess  # noqa: S404 - the tests drive the docker CLI
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
ADDON_DIR = ROOT / "homeassistant" / "novelhog"
BASE_IMAGE = "novelhog-addon-test-base"
BASE_TAG = "local"
ADDON_IMAGE = "novelhog-addon-test:local"
BUILD_TIMEOUT_S = 1200  # the first build downloads base images
UP_TIMEOUT_S = 120.0
HTTP_TIMEOUT_S = 10.0
UNLISTED_HOST = "homeserver"

pytestmark = [
    pytest.mark.container,
    pytest.mark.skipif(
        os.environ.get("NOVELHOG_CONTAINER_TESTS") != "1" or shutil.which("docker") is None,
        reason="set NOVELHOG_CONTAINER_TESTS=1 and install Docker to run the container tests",
    ),
]


def _docker(
    *args: str, check: bool = True, timeout: float = 60
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["docker", *args],  # noqa: S607
        capture_output=True,
        text=True,
        timeout=timeout,
        check=check,
    )


class Addon:
    """One running add-on container with its own `/data` volume."""

    def __init__(self, options: str | None, workdir: Path) -> None:
        self.name = f"novelhog-addon-test-{uuid.uuid4().hex[:8]}"
        self.volume = f"{self.name}-data"
        self.options = options
        self.workdir = workdir
        self.port = 0

    def start(self) -> None:
        _docker("volume", "create", self.volume)
        _docker(
            *["create", "--name", self.name, "-p", "127.0.0.1::8080"],
            *["-v", f"{self.volume}:/data", ADDON_IMAGE],
        )
        if self.options is not None:
            options_file = self.workdir / f"{self.name}-options.json"
            options_file.write_text(self.options)
            _docker("cp", str(options_file), f"{self.name}:/data/options.json")
        _docker("start", self.name)
        self.port = int(_docker("port", self.name, "8080/tcp").stdout.split(":")[-1])
        self._wait_for_api()

    def stop(self) -> None:
        """Remove the container and its volume; never raises."""
        _docker("rm", "-f", self.name, check=False)
        _docker("volume", "rm", "-f", self.volume, check=False)

    def request(self, path: str, host: str | None = None) -> int:
        """The status of `GET path`, addressed to `host` when given."""
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=HTTP_TIMEOUT_S)
        try:
            conn.request("GET", path, headers={"Host": host} if host else {})
            return conn.getresponse().status
        finally:
            conn.close()

    def file_exists(self, path: str) -> bool:
        return _docker("exec", self.name, "test", "-f", path, check=False).returncode == 0

    def _wait_for_api(self) -> None:
        deadline = time.monotonic() + UP_TIMEOUT_S
        while time.monotonic() < deadline:
            try:
                if self.request("/api/v1/settings") == 200:
                    return
            except OSError, http.client.HTTPException:
                pass
            time.sleep(0.5)
        logs = _docker("logs", "--tail", "50", self.name, check=False)
        pytest.fail(f"timed out waiting for the API\n{logs.stdout}{logs.stderr}")


@pytest.fixture(scope="module")
def image() -> Iterator[str]:
    """Build the main image, then the add-on image on top of it (CI builds on the published one)."""
    _docker(
        *["build", "-f", "docker/Dockerfile", "-t", f"{BASE_IMAGE}:{BASE_TAG}", str(ROOT)],
        timeout=BUILD_TIMEOUT_S,
    )
    _docker(
        *[
            "build",
            "--build-arg",
            f"BASE_IMAGE={BASE_IMAGE}",
            "--build-arg",
            f"BASE_TAG={BASE_TAG}",
        ],
        *["-t", ADDON_IMAGE, str(ADDON_DIR)],
        timeout=BUILD_TIMEOUT_S,
    )
    yield ADDON_IMAGE


@pytest.fixture(scope="module")
def default_addon(image: str, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Addon]:
    addon = Addon(None, tmp_path_factory.mktemp("default"))
    try:
        addon.start()
        yield addon
    finally:
        addon.stop()


@pytest.fixture(scope="module")
def configured_addon(image: str, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Addon]:
    addon = Addon(f'{{"allowed_hosts": ["{UNLISTED_HOST}"]}}', tmp_path_factory.mktemp("options"))
    try:
        addon.start()
        yield addon
    finally:
        addon.stop()


class TestAddonImage__WithoutOptions:
    def test_image__serves_the_app_on_8080(self, default_addon: Addon) -> None:
        assert default_addon.request("/api/v1/settings") == 200

    def test_image__writes_the_database_under_data_db(self, default_addon: Addon) -> None:
        assert default_addon.file_exists("/data/db/app.db")

    def test_image__rejects_a_host_name_nobody_listed(self, default_addon: Addon) -> None:
        assert default_addon.request("/api/v1/settings", host=UNLISTED_HOST) == 403


class TestAddonImage__WithAnOptionsFile:
    def test_image__options_reach_the_app(self, configured_addon: Addon) -> None:
        assert configured_addon.request("/api/v1/settings", host=UNLISTED_HOST) == 200
