"""GET /api/v1/events over a real TCP connection (uvicorn in a background thread)."""

import logging
import socket
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import closing
from typing import Any

import anyio.to_thread
import httpx
import pytest
import uvicorn

from novelhog.api.app import create_app
from novelhog.api.serve import build_server
from novelhog.config import Settings
from novelhog.repo import Repo, db
from novelhog.repo import events as event_log

NV = "NV000000000001"
CH = "CH000000000001"


THREAD_LIMIT = 2


class Server:
    def __init__(
        self,
        base_url: str,
        repo: Repo,
        instance: uvicorn.Server,
        thread: threading.Thread,
        open_streams: list[int],
    ) -> None:
        self.base_url = base_url
        self.repo = repo
        self.instance = instance
        self.thread = thread
        self._open_streams = open_streams

    @property
    def open_streams(self) -> int:
        """Event requests the app is still serving."""
        return self._open_streams[0]

    def wait_for_streams(self, count: int, within: float = 3) -> None:
        """Wait until exactly `count` event requests are being served."""
        deadline = time.monotonic() + within
        while self.open_streams != count and time.monotonic() < deadline:
            time.sleep(0.01)
        assert self.open_streams == count

    def stop(self) -> None:
        self.instance.should_exit = True
        self.thread.join(timeout=10)

    def append(self, entity_id: str, payload: dict[str, object], novel_id: str = NV) -> None:
        with db.write(self.repo.engine) as conn:
            event_log.append(conn, novel_id, entity_id, payload, now=self.repo.clock())


StartServer = Callable[..., Server]


@pytest.fixture
def start_server(settings: Settings, repo: Repo) -> Iterator[StartServer]:
    """Factory serving the app with uvicorn on a free local port (a daemon thread).

    The worker-thread limiter is cut to `THREAD_LIMIT`, so any stream that holds a thread starves
    the REST routes in these tests; the wrapper counts the event requests still being served.
    """
    running: list[Server] = []

    def start(*, keepalive_s: float = 0.3, graceful_s: int = 2) -> Server:
        with closing(socket.socket()) as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        app = create_app(settings, repo=repo, sse_poll_s=0.05, sse_keepalive_s=keepalive_s)
        open_streams = [0]

        async def wrapped(scope: Any, receive: Any, send: Any) -> None:
            if scope["type"] == "lifespan":
                anyio.to_thread.current_default_thread_limiter().total_tokens = THREAD_LIMIT
                await app(scope, receive, send)
                return
            counted = scope["type"] == "http" and scope["path"] == "/api/v1/events"
            open_streams[0] += counted
            try:
                await app(scope, receive, send)
            finally:
                open_streams[0] -= counted

        instance = build_server(app, host="127.0.0.1", port=port, graceful_s=graceful_s)
        instance.config.app = wrapped  # count requests; the server still flags `app` on shutdown
        thread = threading.Thread(target=instance.run, daemon=True)
        thread.start()
        deadline = time.monotonic() + 10
        while not instance.started and time.monotonic() < deadline:
            time.sleep(0.01)
        assert instance.started
        server = Server(f"http://127.0.0.1:{port}", repo, instance, thread, open_streams)
        running.append(server)
        return server

    yield start
    for server in running:
        server.stop()
        assert not server.thread.is_alive()


@pytest.fixture
def server(start_server: StartServer) -> Server:
    """The app served over TCP with fast polling and keepalive."""
    return start_server()


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

    def take_events(self, count: int) -> list[str]:
        """Take `count` event frames, skipping keepalive comments."""
        events: list[str] = []
        while len(events) < count:
            events += [f for f in self.take(1) if not f.startswith(":")]
        return events


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
            assert frames.take_events(1) == [
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
            (frame,) = Frames(r).take_events(1)
        assert frame == f'id: 2\nevent: novel\ndata: {{"id":"{NV}","again":true}}'

    def test_events__streams_a_task_event_as_event_task_with_its_seq_as_id(
        self, server: Server
    ) -> None:
        with _open(server) as http, http.stream("GET", "/api/v1/events") as response:
            frames = Frames(response)
            frames.take(1)
            server.append("TK000000000001", {"id": "TK000000000001", "state": "queued"})
            assert frames.take_events(1) == [
                'id: 1\nevent: task\ndata: {"id":"TK000000000001","state":"queued"}'
            ]

    def test_events__resumes_from_a_last_event_id_that_has_no_row(self, server: Server) -> None:
        with db.write(server.repo.engine) as conn:
            event_log.take_clock(conn)  # seq 1: a task-only transaction, no event row
        server.append(NV, {"id": NV})
        headers = {"Last-Event-ID": "1"}
        with _open(server, headers=headers) as http, http.stream("GET", "/api/v1/events") as r:
            (frame,) = Frames(r).take_events(1)
        assert frame == f'id: 2\nevent: novel\ndata: {{"id":"{NV}"}}'

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
                got_a = first.take_events(2)
                got_b = second.take_events(2)
        assert got_a == got_b
        assert [f.splitlines()[0] for f in got_a] == ["id: 1", "id: 2"]

    def test_events__idle_streams_do_not_starve_the_rest_api(
        self, start_server: StartServer
    ) -> None:
        server = start_server(keepalive_s=600)  # idle: nothing yields between polls
        clients = [_open(server) for _ in range(THREAD_LIMIT + 1)]
        contexts = [c.stream("GET", "/api/v1/events") for c in clients]
        try:
            readers = [Frames(context.__enter__()) for context in contexts]
            for reader in readers:
                reader.take(1)
            server.wait_for_streams(THREAD_LIMIT + 1)
            started = time.monotonic()
            with httpx.Client(base_url=server.base_url, timeout=3) as http:
                assert http.get("/api/v1/settings").status_code == 200
            assert time.monotonic() - started < 2
            assert len(readers) == server.open_streams
        finally:
            for context, client in zip(contexts, clients, strict=True):
                context.__exit__(None, None, None)
                client.close()

    def test_events__disconnect_frees_the_stream_quickly_despite_long_keepalive(
        self, start_server: StartServer
    ) -> None:
        server = start_server(keepalive_s=600)
        with _open(server) as http, http.stream("GET", "/api/v1/events") as response:
            reader = Frames(response)
            reader.take(1)
            server.wait_for_streams(1)
        started = time.monotonic()
        server.wait_for_streams(0, within=1)
        assert time.monotonic() - started < 1

    def test_events__shutdown_ends_open_streams_before_the_graceful_timeout_without_errors(
        self, start_server: StartServer
    ) -> None:
        server = start_server(graceful_s=5)
        errors: list[str] = []

        class Collect(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                if record.levelno >= logging.ERROR:
                    errors.append(record.getMessage())

        handler = Collect()
        logger = logging.getLogger("uvicorn.error")
        logger.addHandler(handler)
        try:
            with _open(server) as a, _open(server) as b:
                with (
                    a.stream("GET", "/api/v1/events") as ra,
                    b.stream("GET", "/api/v1/events") as rb,
                ):
                    readers = [Frames(ra), Frames(rb)]
                    for reader in readers:
                        reader.take(1)
                    server.wait_for_streams(2)
                    started = time.monotonic()
                    server.stop()
                    elapsed = time.monotonic() - started
                    assert not server.thread.is_alive()
        finally:
            logger.removeHandler(handler)
        assert elapsed < 2
        assert errors == []

    def test_events__app_started_twice_keeps_streams_open_after_the_second_start(
        self, settings: Settings, repo: Repo
    ) -> None:
        app = create_app(settings, repo=repo, sse_poll_s=0.05, sse_keepalive_s=0.3)
        for round_number in (1, 2):
            with closing(socket.socket()) as probe:
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]
            instance = build_server(app, host="127.0.0.1", port=port, graceful_s=2)
            thread = threading.Thread(target=instance.run, daemon=True)
            thread.start()
            deadline = time.monotonic() + 10
            while not instance.started and time.monotonic() < deadline:
                time.sleep(0.01)
            assert instance.started
            try:
                with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=10) as http:
                    with http.stream("GET", "/api/v1/events") as response:
                        reader = Frames(response)
                        reader.take(1)
                        time.sleep(0.4)  # several polls: an ended stream would be closed by now
                        keepalives = reader.take(1)
                        assert keepalives == [": keepalive"], round_number
            finally:
                instance.should_exit = True
                thread.join(timeout=10)
                assert not thread.is_alive()


class TestServeHelper:
    def test_build_server__graceful_timeout_not_above_poll_raises_value_error(
        self, settings: Settings, repo: Repo
    ) -> None:
        app = create_app(settings, repo=repo, sse_poll_s=1.0)
        with pytest.raises(ValueError, match="graceful_s"):
            build_server(app, host="127.0.0.1", port=0, graceful_s=1)
