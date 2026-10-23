"""GET /api/v1/events over a real TCP connection (uvicorn in a background thread)."""

import socket
import threading
import time
from collections.abc import Iterator
from contextlib import closing

import httpx
import pytest
import uvicorn

from novelhog.api.app import create_app
from novelhog.config import Settings
from novelhog.repo import Repo, db
from novelhog.repo import events as event_log

NV = "NV000000000001"
CH = "CH000000000001"


class Server:
    def __init__(
        self, base_url: str, repo: Repo, instance: uvicorn.Server, thread: threading.Thread
    ) -> None:
        self.base_url = base_url
        self.repo = repo
        self.instance = instance
        self.thread = thread

    def stop(self) -> None:
        self.instance.should_exit = True
        self.thread.join(timeout=10)

    def append(self, entity_id: str, payload: dict[str, object], novel_id: str = NV) -> None:
        with db.write(self.repo.engine) as conn:
            event_log.append(conn, novel_id, entity_id, payload, now=self.repo.clock())


@pytest.fixture
def server(settings: Settings, repo: Repo) -> Iterator[Server]:
    """The app served by uvicorn on a free local port, with fast polling and keepalive."""
    with closing(socket.socket()) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    app = create_app(settings, repo=repo, sse_poll_s=0.05, sse_keepalive_s=0.3)
    config = uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="warning", timeout_graceful_shutdown=2
    )
    instance = uvicorn.Server(config)
    thread = threading.Thread(target=instance.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not instance.started and time.monotonic() < deadline:
        time.sleep(0.01)
    assert instance.started
    running = Server(f"http://127.0.0.1:{port}", repo, instance, thread)
    yield running
    running.stop()
    assert not thread.is_alive()


class Frames:
    """Reads SSE frames (blank-line separated) from one streaming response."""

    def __init__(self, response: httpx.Response) -> None:
        self._chunks = response.iter_text()
        self._buffer = ""

    def take(self, count: int) -> list[str]:
        frames: list[str] = []
        while len(frames) < count:
            while "\n\n" not in self._buffer:
                self._buffer += next(self._chunks)
            frame, self._buffer = self._buffer.split("\n\n", 1)
            frames.append(frame)
        return frames


def _open(server: Server, **kwargs: object) -> httpx.Client:
    return httpx.Client(base_url=server.base_url, timeout=10, **kwargs)  # type: ignore[arg-type]


class TestEventsOverTcp:
    def test_events__responds_with_event_stream_and_delivers_new_event(
        self, server: Server
    ) -> None:
        with _open(server) as http, http.stream("GET", "/api/v1/events") as response:
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")
            assert response.headers["cache-control"] == "no-cache"
            assert response.headers["x-accel-buffering"] == "no"
            frames = Frames(response)
            assert frames.take(1) == ["id: 0\nevent: ready\ndata: {}"]
            server.append(CH, {"id": CH, "novel_id": NV})
            assert frames.take(1) == [
                f'id: 1\nevent: chapter\ndata: {{"id":"{CH}","novel_id":"{NV}"}}'
            ]

    def test_events__sends_keepalive_comment_when_idle(self, server: Server) -> None:
        with _open(server) as http, http.stream("GET", "/api/v1/events") as response:
            assert Frames(response).take(2)[1] == ": keepalive"

    def test_events__resumes_from_last_event_id(self, server: Server) -> None:
        server.append(NV, {"id": NV})
        server.append(NV, {"id": NV, "again": True})
        headers = {"Last-Event-ID": "1"}
        with _open(server, headers=headers) as http, http.stream("GET", "/api/v1/events") as r:
            (frame,) = Frames(r).take(1)
        assert frame == f'id: 2\nevent: novel\ndata: {{"id":"{NV}","again":true}}'

    def test_events__unknown_novel_returns_404(self, server: Server) -> None:
        with _open(server) as http:
            unknown = http.get("/api/v1/events", params={"novel_id": "NV999999999999"})
            malformed = http.get("/api/v1/events", params={"novel_id": "nope"})
        for response in (unknown, malformed):
            assert response.status_code == 404
            assert response.json()["error"]["code"] == "not_found"

    def test_events__two_connections_receive_the_same_events(self, server: Server) -> None:
        with _open(server) as a, _open(server) as b:
            with a.stream("GET", "/api/v1/events") as ra, b.stream("GET", "/api/v1/events") as rb:
                first, second = Frames(ra), Frames(rb)
                first.take(1)
                second.take(1)
                server.append(NV, {"id": NV})
                server.append(CH, {"id": CH, "novel_id": NV})
                got_a = [f for f in first.take(2) if not f.startswith(":")]
                got_b = [f for f in second.take(2) if not f.startswith(":")]
        assert got_a == got_b
        assert [f.splitlines()[0] for f in got_a] == ["id: 1", "id: 2"]

    def test_events__server_stops_while_a_client_is_still_connected(self, server: Server) -> None:
        with _open(server) as http, http.stream("GET", "/api/v1/events") as response:
            Frames(response).take(1)
            server.stop()
            assert not server.thread.is_alive()
