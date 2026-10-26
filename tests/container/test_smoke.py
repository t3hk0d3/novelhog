"""Container smoke tests: build the image and exercise it through Compose (spec section 13).

Skipped unless Docker is available and `NOVELHOG_CONTAINER_TESTS=1`, so `scripts/check.sh` never
builds an image.
"""

import http.client
import json
import os
import re
import shutil
import socket
import subprocess  # noqa: S404 - the tests drive the docker CLI
import time
import uuid
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
FILES = ["-f", "compose.dev.yaml", "-f", "compose.test.yaml"]
CMD_TIMEOUT_S = 300  # the first build downloads base images
UP_TIMEOUT_S = 120.0
HTTP_TIMEOUT_S = 10.0
WEB = "novelhog-web"
WORKER = "novelhog-worker"
MIGRATE = "novelhog-migrate"

pytestmark = [
    pytest.mark.container,
    pytest.mark.skipif(
        os.environ.get("NOVELHOG_CONTAINER_TESTS") != "1" or shutil.which("docker") is None,
        reason="set NOVELHOG_CONTAINER_TESTS=1 and install Docker to run the container tests",
    ),
]


def _parse_time(value: str | int) -> datetime:
    """Parse Docker's RFC 3339 nanosecond timestamp (fromisoformat takes at most microseconds)."""
    text = re.sub(r"(\.\d{6})\d*", r"\1", str(value))
    return datetime.fromisoformat(text)


class Stack:
    """A Compose project with its own name, port and volumes."""

    def __init__(self) -> None:
        self.project = f"novelhog-smoke-{uuid.uuid4().hex[:8]}"
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port: int = probe.getsockname()[1]
        self.env = {**os.environ, "NOVELHOG_HTTP_PORT": str(self.port)}

    def compose(
        self, *args: str, check: bool = True, timeout: float = CMD_TIMEOUT_S
    ) -> subprocess.CompletedProcess[str]:
        """Run `docker compose` for this project."""
        return subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["docker", "compose", "-p", self.project, *FILES, *args],  # noqa: S607
            cwd=ROOT,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=check,
        )

    def exec(self, service: str, *command: str, check: bool = True) -> str:
        """Run `command` inside a running service container and return its stdout."""
        return self.compose("exec", "-T", service, *command, check=check).stdout

    def container_id(self, service: str) -> str:
        """The (possibly stopped) container id of `service`."""
        return self.compose("ps", "-a", "-q", service).stdout.strip()

    def inspect(self, service: str) -> dict[str, object]:
        """`docker inspect` state of `service`."""
        out = subprocess.run(  # noqa: S603
            ["docker", "inspect", self.container_id(service)],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        ).stdout
        data: dict[str, object] = json.loads(out)[0]
        return data

    def state(self, service: str) -> dict[str, str | int]:
        """The `State` block of `service`'s container."""
        state: dict[str, str | int] = self.inspect(service)["State"]  # type: ignore[assignment]
        return state

    def request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, object] | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        """One HTTP request to Caddy; header names are lower-cased."""
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=HTTP_TIMEOUT_S)
        sent = dict(headers or {})
        payload = None
        if body is not None:
            payload = json.dumps(body).encode()
            sent["Content-Type"] = "application/json"
        try:
            conn.request(method, path, body=payload, headers=sent)
            response = conn.getresponse()
            data = response.read()
            return response.status, {k.lower(): v for k, v in response.getheaders()}, data
        finally:
            conn.close()

    def get(self, path: str) -> tuple[int, dict[str, str], bytes]:
        """GET `path`."""
        return self.request("GET", path)

    def wait_until(
        self, what: str, ready: Callable[[], bool], timeout: float = UP_TIMEOUT_S
    ) -> None:
        """Poll `ready` until it is true; fail with `what` after `timeout` seconds."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if ready():
                    return
            except OSError, subprocess.SubprocessError, http.client.HTTPException:
                pass
            time.sleep(0.5)
        logs = self.compose("logs", "--no-color", "--tail", "50", check=False).stdout
        pytest.fail(f"timed out waiting for {what}\n{logs}")

    def up(self) -> None:
        """Start migrate, web and worker, then wait until Caddy answers."""
        self.compose("up", "-d", "--build", "--no-color", timeout=CMD_TIMEOUT_S * 2)
        self.wait_until("web to serve /", lambda: self.get("/")[0] == 200)
        # Caddy serves / from disk while uvicorn is still starting; wait for the API too.
        self.wait_until("the API behind Caddy", lambda: self.get("/api/v1/settings")[0] == 200)

    def down(self) -> None:
        """Remove containers and volumes; never raises."""
        self.compose("down", "-v", "--remove-orphans", "-t", "5", check=False, timeout=180)


@pytest.fixture(scope="module")
def stack() -> Iterator[Stack]:
    s = Stack()
    try:
        s.up()
        yield s
    finally:
        s.down()


def test_migrate__runs_once_and_exits_0_before_web_and_worker_start(stack: Stack) -> None:
    migrate = stack.state(MIGRATE)
    assert migrate["Status"] == "exited"
    assert migrate["ExitCode"] == 0
    for service in (WEB, WORKER):
        assert _parse_time(migrate["FinishedAt"]) <= _parse_time(stack.state(service)["StartedAt"])


def test_migrate__works_with_working_directory_root(stack: Stack) -> None:
    out = stack.compose("exec", "-T", "-w", "/", WEB, "novelhog", "db", "migrate").stdout
    assert "database is at" in out


def test_caddy__serves_index_for_root_and_unknown_routes_without_caching(stack: Stack) -> None:
    for path in ("/", "/novels/NVabc123abc123", "/settings"):
        status, headers, body = stack.get(path)
        assert status == 200
        assert b'<div id="root">' in body
        assert headers["cache-control"] == "no-cache"


def test_caddy__404_for_missing_static_file_and_unknown_api_path(stack: Stack) -> None:
    status, headers, body = stack.get("/static/covers/missing.jpg")
    assert status == 404
    assert "immutable" not in headers.get("cache-control", "")
    assert b'<div id="root">' not in body
    status, _, body = stack.get("/api/v1/nope")
    assert status == 404
    assert b'<div id="root">' not in body


def test_caddy__serves_service_worker_and_manifest_at_the_root(stack: Stack) -> None:
    status, headers, _ = stack.get("/sw.js")
    assert status == 200
    assert headers["cache-control"] == "no-cache"
    status, headers, _ = stack.get("/manifest.webmanifest")
    assert status == 200
    assert "json" in headers["content-type"]
    status, _, _ = stack.get("/icons/192.png")
    assert status == 200


EXPORT_DIR = "/data/static/exports/NVabcdefabcdef/EXabcdefabcdef"
EXPORT_PATH = "/static/exports/NVabcdefabcdef/EXabcdefabcdef/A%20Title%20Chapters%201-2.epub"


def test_caddy__serves_a_nested_export_file_as_an_epub_attachment(stack: Stack) -> None:
    stack.exec(
        WORKER,
        "python",
        "-c",
        f"import pathlib; d = pathlib.Path({EXPORT_DIR!r}); d.mkdir(parents=True, "
        "exist_ok=True); (d / 'A Title Chapters 1-2.epub').write_bytes(b'PK-epub')",
    )
    status, headers, body = stack.get(EXPORT_PATH)
    assert status == 200
    assert body == b"PK-epub"
    assert headers["content-disposition"] == "attachment"
    assert headers["content-type"] == "application/epub+zip"


@pytest.mark.parametrize(
    "path", ["/static/../db/app.db", "/static/%2e%2e/db/app.db", "/static/..%2fdb/app.db"]
)
def test_caddy__never_serves_the_database_through_static(stack: Stack, path: str) -> None:
    exists = stack.compose("exec", "-T", WEB, "test", "-f", "/data/db/app.db", check=False)
    assert exists.returncode == 0  # the file is there to be leaked
    status, _, body = stack.get(path)
    assert not body.startswith(b"SQLite format 3")
    assert status == 404 or b'<div id="root">' in body


def test_caddy__serves_covers_with_immutable_caching(stack: Stack) -> None:
    stack.exec(
        WORKER,
        "python",
        "-c",
        "import pathlib; d = pathlib.Path('/data/static/covers'); d.mkdir(parents=True, "
        "exist_ok=True); (d / 'c.jpg').write_bytes(b'jpg')",
    )
    status, headers, _ = stack.get("/static/covers/c.jpg")
    assert status == 200
    assert headers["cache-control"] == "public, max-age=31536000, immutable"


def test_caddy__proxies_settings_to_the_api(stack: Stack) -> None:
    status, headers, body = stack.get("/api/v1/settings")
    assert status == 200
    assert headers["content-type"].startswith("application/json")
    assert isinstance(json.loads(body), dict)


def test_caddy__streams_events_without_buffering(stack: Stack) -> None:
    conn = http.client.HTTPConnection("127.0.0.1", stack.port, timeout=HTTP_TIMEOUT_S)
    try:
        conn.request("GET", "/api/v1/events")
        response = conn.getresponse()
        assert response.status == 200
        assert "event-stream" in response.getheader("Content-Type", "")
        assert response.getheader("Content-Encoding") is None
        seen = b""
        while b"event: ready" not in seen:
            line = response.fp.readline()
            assert line, "stream ended before the ready event"
            seen += line
        assert not response.isclosed()
    finally:
        conn.close()


def test_caddy__listens_on_the_configured_http_port(stack: Stack) -> None:
    assert stack.get("/")[0] == 200
    published = stack.compose("port", WEB, str(stack.port)).stdout.strip()
    assert published.endswith(f":{stack.port}")


def test_shared_database__api_write_succeeds(stack: Stack) -> None:
    status, _, body = stack.request(
        "POST",
        "/api/v1/novels",
        body={"toc_url": "https://www.royalroad.com/fiction/1/smoke"},
        headers={"Origin": f"http://127.0.0.1:{stack.port}"},
    )
    assert status in (200, 201), body


@pytest.mark.parametrize("child", ["api", "caddy"])
def test_supervision__killing_either_process_stops_the_web_container(child: str) -> None:
    s = Stack()
    try:
        s.up()
        s.exec(
            WEB,
            "python",
            "-c",
            "import os, signal, sys\n"
            "for p in os.listdir('/proc'):\n"
            "    if p.isdigit():\n"
            "        try:\n"
            "            c = open(f'/proc/{p}/cmdline').read().split('\\0')\n"
            "        except OSError:\n"
            "            continue\n"
            "        if sys.argv[1] == 'api':\n"
            "            hit = 'novelhog.cli' in c and 'api' in c\n"
            "        else:\n"
            "            hit = os.path.basename(c[0]) == 'caddy'\n"
            "        if hit:\n"
            "            os.kill(int(p), signal.SIGKILL)\n",
            child,
        )
        s.wait_until("web container to stop", lambda: s.state(WEB)["Status"] == "exited")
        assert s.state(WEB)["ExitCode"] != 0
    finally:
        s.down()


def test_compose_config__worker_and_flaresolverr_share_a_network_web_does_not_join() -> None:
    config = subprocess.run(  # noqa: S603
        ["docker", "compose", *FILES[:2], "config", "--format", "json"],  # noqa: S607
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    services = json.loads(config.stdout)["services"]
    shared = set(services[WORKER]["networks"]) & set(services["flaresolverr"]["networks"])
    assert shared
    assert shared.isdisjoint(services[WEB]["networks"])
    assert (
        services[WORKER]["environment"]["NOVELHOG_FLARESOLVERR_URL"] == "http://flaresolverr:8191"
    )
    assert "flaresolverr" not in services[WORKER]["depends_on"]
