"""Tests for the image fetcher."""

import io
from pathlib import Path

import requests
from PIL import Image

from novelhog import net
from novelhog.image_fetcher import ImageFetcher
from tests.fixtures.images.conftest import Server, http_server, make_image  # noqa: F401


def fetcher(
    max_bytes: int = 100_000, max_pixels: int = 10_000, max_dimension: int = 0, private: bool = True
) -> ImageFetcher:
    session = net.GuardedSession(allow_private=private, resolver=lambda h: ["127.0.0.1"])
    return ImageFetcher(
        session, max_bytes=max_bytes, max_pixels=max_pixels, max_dimension=max_dimension
    )


class TestImageFetcherFetch__Allowed:
    def test_fetch__returns_sniffed_image(self, http_server: Server) -> None:  # noqa: F811
        data = make_image("PNG", (20, 20))
        http_server.routes["/a.png"] = (200, {"Content-Type": "text/plain"}, data)
        result = fetcher().fetch(http_server.url("/a.png", host="site.test"))
        assert result is not None
        assert result.mime == "image/png"
        assert result.data == data

    def test_fetch__downscales_when_needed(self, http_server: Server) -> None:  # noqa: F811
        http_server.routes["/a.png"] = (200, {}, make_image("PNG", (80, 40)))
        result = fetcher(max_dimension=20).fetch(http_server.url("/a.png"))
        assert result is not None
        with Image.open(io.BytesIO(result.data)) as img:
            assert img.size == (20, 10)

    def test_fetch__sends_browser_user_agent(self, http_server: Server) -> None:  # noqa: F811
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
    def test_fetch__over_max_bytes_returns_none(self, http_server: Server) -> None:  # noqa: F811
        http_server.routes["/a.png"] = (200, {}, make_image("PNG", (50, 50)))
        assert fetcher(max_bytes=50).fetch(http_server.url("/a.png")) is None

    def test_fetch__reads_at_most_one_byte_over_cap(self) -> None:
        class FakeResponse:
            ok = True
            pulled = 0

            def __enter__(self) -> FakeResponse:
                return self

            def __exit__(self, *exc: object) -> None:
                return None

            def iter_content(self, size: int):  # type: ignore[no-untyped-def]
                for _ in range(1000):
                    self.pulled += size
                    yield b"x" * size

        response = FakeResponse()

        class FakeSession(requests.Session):
            def get(self, url, **kwargs):  # type: ignore[no-untyped-def]
                return response

        f = ImageFetcher(FakeSession(), max_bytes=100, max_pixels=10, max_dimension=0)
        assert f.fetch("http://x/") is None
        assert response.pulled == 16 * 1024  # stopped after the first chunk

    def test_fetch__too_many_pixels_returns_none(self, http_server: Server) -> None:  # noqa: F811
        http_server.routes["/a.png"] = (200, {}, make_image("PNG", (200, 200)))
        assert fetcher(max_pixels=1000).fetch(http_server.url("/a.png")) is None


class TestImageFetcherFetch__NotAllowed:
    def test_fetch__svg_returns_none(self, http_server: Server) -> None:  # noqa: F811
        svg = (Path(__file__).parent.parent / "fixtures/images/evil.svg").read_bytes()
        http_server.routes["/a.svg"] = (200, {"Content-Type": "image/svg+xml"}, svg)
        assert fetcher().fetch(http_server.url("/a.svg")) is None

    def test_fetch__unknown_content_returns_none(self, http_server: Server) -> None:  # noqa: F811
        http_server.routes["/a"] = (200, {}, b"hello")
        assert fetcher().fetch(http_server.url("/a")) is None

    def test_fetch__truncated_image_returns_none(self, http_server: Server) -> None:  # noqa: F811
        http_server.routes["/a"] = (200, {}, make_image("PNG")[:12])
        assert fetcher().fetch(http_server.url("/a")) is None


class TestImageFetcherFetch__Failures:
    def test_fetch__non_2xx_returns_none(self, http_server: Server) -> None:  # noqa: F811
        assert fetcher().fetch(http_server.url("/missing")) is None

    def test_fetch__network_error_returns_none(self) -> None:
        assert fetcher().fetch("http://127.0.0.1:1/a.png") is None

    def test_fetch__refused_address_returns_none(self, http_server: Server) -> None:  # noqa: F811
        http_server.routes["/a.png"] = (200, {}, make_image("PNG"))
        assert fetcher(private=False).fetch(http_server.url("/a.png")) is None
        assert http_server.hits == []
