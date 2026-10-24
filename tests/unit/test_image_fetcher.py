"""Tests for the image fetcher."""

import io
import random
import socket
import threading
import time
from pathlib import Path

import pytest
import requests
from PIL import Image

from novelhog import images, net
from novelhog.image_fetcher import ImageFetcher
from tests.fixtures.images.helpers import Server, make_animated, make_image

FIXTURES = Path(__file__).parent.parent / "fixtures" / "images"


def fetcher(
    max_bytes: int = 100_000,
    max_pixels: int = 10_000,
    max_dimension: int = 0,
    private: bool = True,
    deadline: float = 60.0,
) -> ImageFetcher:
    session = net.GuardedSession(allow_private=private, resolver=lambda h: ["127.0.0.1"])
    return ImageFetcher(
        session,
        max_bytes=max_bytes,
        max_pixels=max_pixels,
        max_dimension=max_dimension,
        deadline=deadline,
    )


class TestImageFetcherFetch__Allowed:
    def test_fetch__returns_sniffed_image(self, http_server: Server) -> None:
        data = make_image("PNG", (20, 20))
        http_server.routes["/a.png"] = (200, {"Content-Type": "text/plain"}, data)
        result = fetcher().fetch(http_server.url("/a.png", host="site.test"))
        assert result is not None
        assert result.mime == "image/png"
        assert result.data == data

    def test_fetch__downscales_when_needed(self, http_server: Server) -> None:
        http_server.routes["/a.png"] = (200, {}, make_image("PNG", (80, 40)))
        result = fetcher(max_dimension=20).fetch(http_server.url("/a.png"))
        assert result is not None
        with Image.open(io.BytesIO(result.data)) as img:
            assert img.size == (20, 10)

    def test_fetch__sends_browser_user_agent(self, http_server: Server) -> None:
        seen: list[str] = []

        class Spy(requests.Session):
            def get(self, url, **kwargs):  # type: ignore[no-untyped-def]
                seen.append(str(kwargs["headers"]))
                return super().get(url, **kwargs)

        session = Spy()
        http_server.routes["/a.png"] = (200, {}, make_image("PNG"))
        ImageFetcher(session, max_bytes=10**6, max_pixels=10**6, max_dimension=0).fetch(
            http_server.url("/a.png")
        )
        assert "Mozilla" in seen[0]


class TestImageFetcherFetch__TooBig:
    def test_fetch__over_max_bytes_returns_none(self, http_server: Server) -> None:
        http_server.routes["/a.png"] = (200, {}, make_image("PNG", (50, 50)))
        assert fetcher(max_bytes=50).fetch(http_server.url("/a.png")) is None

    def test_fetch__reads_at_most_one_chunk_over_cap(self) -> None:
        class FakeRaw:
            pulled = 0

            def read1(self, size: int, decode_content: bool = True) -> bytes:
                self.pulled += size
                return b"x" * size

        class FakeResponse:
            ok = True

            def __init__(self) -> None:
                self.raw = FakeRaw()

            def __enter__(self) -> FakeResponse:
                return self

            def __exit__(self, *exc: object) -> None:
                return None

        response = FakeResponse()

        class FakeSession(requests.Session):
            def get(self, url, **kwargs):  # type: ignore[no-untyped-def]
                return response

        f = ImageFetcher(
            FakeSession(), max_bytes=100, max_pixels=10, max_dimension=0, deadline=60.0
        )
        assert f.fetch("http://x/") is None
        assert response.raw.pulled == 16 * 1024  # stopped after the first chunk

    def test_fetch__too_many_pixels_returns_none(self, http_server: Server) -> None:
        http_server.routes["/a.png"] = (200, {}, make_image("PNG", (200, 200)))
        assert fetcher(max_pixels=1000).fetch(http_server.url("/a.png")) is None


class TestImageFetcherFetch__NotAllowed:
    def test_fetch__svg_returns_none(self, http_server: Server) -> None:
        svg = (Path(__file__).parent.parent / "fixtures/images/evil.svg").read_bytes()
        http_server.routes["/a.svg"] = (200, {"Content-Type": "image/svg+xml"}, svg)
        assert fetcher().fetch(http_server.url("/a.svg")) is None

    def test_fetch__unknown_content_returns_none(self, http_server: Server) -> None:
        http_server.routes["/a"] = (200, {}, b"hello")
        assert fetcher().fetch(http_server.url("/a")) is None

    def test_fetch__truncated_image_returns_none(self, http_server: Server) -> None:
        http_server.routes["/a"] = (200, {}, make_image("PNG")[:12])
        assert fetcher().fetch(http_server.url("/a")) is None


class TestImageFetcherFetch__Failures:
    def test_fetch__non_2xx_returns_none(self, http_server: Server) -> None:
        assert fetcher().fetch(http_server.url("/missing")) is None

    def test_fetch__network_error_returns_none(self) -> None:
        assert fetcher().fetch("http://127.0.0.1:1/a.png") is None

    def test_fetch__refused_address_returns_none(self, http_server: Server) -> None:
        http_server.routes["/a.png"] = (200, {}, make_image("PNG"))
        assert fetcher(private=False).fetch(http_server.url("/a.png")) is None
        assert http_server.hits == []


class TestImageFetcherFetch__Malformed:
    @pytest.mark.parametrize(
        ("name", "max_dimension"),
        [
            ("corrupt_ihdr", 0),  # header unreadable: fails in the pixel check
            ("corrupt_ihdr", 16),
            ("corrupt_struct", 16),  # header fine, data corrupt: fails while downscaling
            ("corrupt_index", 16),
            ("corrupt_syntax", 16),
        ],
    )
    def test_fetch__corrupt_image_returns_none(
        self, http_server: Server, name: str, max_dimension: int
    ) -> None:
        http_server.routes["/a"] = (200, {}, (FIXTURES / f"{name}.bin").read_bytes())
        result = fetcher(max_pixels=40_000_000, max_dimension=max_dimension).fetch(
            http_server.url("/a")
        )
        assert result is None

    def test_fetch__seeded_fuzz_never_raises(self) -> None:
        rnd = random.Random(1234)  # noqa: S311 - seeded test data
        seeds = [
            make_image("PNG", (64, 48)),
            make_image("JPEG", (64, 48)),
            make_image("WEBP", (64, 48)),
            make_image("WEBP", (64, 48), mode="RGBA"),
            make_animated("GIF"),
            make_animated("WEBP"),
        ]
        payloads: list[bytes] = []

        class Raw:
            def __init__(self, data: bytes) -> None:
                self.data = data

            def read1(self, size: int, decode_content: bool = True) -> bytes:
                chunk, self.data = self.data[:size], self.data[size:]
                return chunk

        class Response:
            ok = True

            def __init__(self, data: bytes) -> None:
                self.raw = Raw(data)

            def __enter__(self) -> Response:
                return self

            def __exit__(self, *exc: object) -> None:
                return None

        class Fake(requests.Session):
            def get(self, url, **kwargs):  # type: ignore[no-untyped-def]
                return Response(payloads.pop())

        f = ImageFetcher(Fake(), max_bytes=10**6, max_pixels=40_000_000, max_dimension=16)
        for _ in range(500):
            data = bytearray(rnd.choice(seeds))
            for _ in range(rnd.randint(0, 6)):
                data[rnd.randrange(len(data))] = rnd.randrange(256)
            if rnd.random() < 0.3:
                del data[rnd.randrange(1, len(data)) :]
            payloads.append(bytes(data))
            f.fetch("http://x/")  # must not raise


class TestImageFetcherFetch__Deadline:
    def test_fetch__slow_drip_hits_overall_deadline(self, http_server: Server) -> None:
        http_server.routes["/slow"] = (200, {}, b"x" * 200)
        http_server.drip["/slow"] = 0.05
        started = time.monotonic()
        assert fetcher(deadline=0.3).fetch(http_server.url("/slow")) is None
        assert time.monotonic() - started < 3


class TestConfigurePixelLimit__Fetcher:
    def test_fetcher__does_not_change_the_global_limit(self) -> None:
        images.configure_pixel_limit(777)
        fetcher(max_pixels=10_000)
        assert Image.MAX_IMAGE_PIXELS == 777


def _serve_raw(head: bytes, body: bytes, linger: float = 0.0) -> int:
    """Serve one raw HTTP response on a local socket; returns the port."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()

    def run() -> None:
        conn, _ = listener.accept()
        conn.recv(65536)
        conn.sendall(head + body)
        time.sleep(linger)
        conn.close()
        listener.close()

    threading.Thread(target=run, daemon=True).start()
    port: int = listener.getsockname()[1]
    return port


class TestImageFetcherFetch__TransportErrors:
    @pytest.mark.parametrize(
        ("head", "body", "linger"),
        [
            pytest.param(
                b"HTTP/1.1 200 OK\r\nContent-Encoding: gzip\r\nContent-Length: 20\r\n"
                b"Connection: close\r\n\r\n",
                b"\x1f\x8b\x08\x00garbagegarbage!!",
                0.0,
                id="corrupt_gzip",
            ),
            pytest.param(
                b"HTTP/1.1 200 OK\r\nContent-Length: 1000\r\nConnection: close\r\n\r\n",
                make_image("PNG"),
                0.0,
                id="short_body",
            ),
            pytest.param(
                b"HTTP/1.1 200 OK\r\nContent-Length: 1000\r\n\r\n",
                make_image("PNG")[:10],
                2.0,
                id="stalled_body",
            ),
            pytest.param(
                b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n",
                b"zz\r\nabc\r\n",
                0.0,
                id="malformed_chunked",
            ),
        ],
    )
    def test_fetch__transport_error_returns_none(
        self, monkeypatch: pytest.MonkeyPatch, head: bytes, body: bytes, linger: float
    ) -> None:
        monkeypatch.setattr(net, "READ_TIMEOUT", 0.3)
        port = _serve_raw(head, body, linger)
        assert fetcher().fetch(f"http://127.0.0.1:{port}/") is None
