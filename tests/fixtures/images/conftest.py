"""Pillow-generated image fixtures and a local HTTP server for fetch tests."""

import io
import threading
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from PIL import Image


def make_image(
    fmt: str, size: tuple[int, int] = (10, 10), mode: str = "RGB", **save: object
) -> bytes:
    """Encode a solid-colour image."""
    color = (200, 10, 10, 128) if mode == "RGBA" else (200, 10, 10)
    out = io.BytesIO()
    Image.new(mode, size, color).save(out, fmt, **save)
    return out.getvalue()


def make_animated(fmt: str) -> bytes:
    """Encode a two-frame animation (first frame red, second blue)."""
    frames = [Image.new("RGB", (8, 8), "red"), Image.new("RGB", (8, 8), "blue")]
    out = io.BytesIO()
    frames[0].save(out, fmt, save_all=True, append_images=frames[1:], duration=50, loop=0)
    return out.getvalue()


class Server:
    """Local HTTP server whose routes map a path to (status, headers, body) or a callable."""

    def __init__(self) -> None:
        self.routes: dict[str, tuple[int, dict[str, str], bytes]] = {}
        self.hits: list[tuple[str, str | None]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                outer.hits.append((self.path, self.headers.get("Host")))
                status, headers, body = outer.routes.get(self.path, (404, {}, b""))
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except OSError:
                    pass

            def log_message(self, format: str, *args: object) -> None:
                pass

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()

    def url(self, path: str, host: str = "127.0.0.1") -> str:
        return f"http://{host}:{self.port}{path}"


@pytest.fixture
def http_server() -> Iterator[Server]:
    server = Server()
    server.start()
    yield server
    server.stop()


@pytest.fixture
def png_bytes() -> Callable[..., bytes]:
    return lambda size=(10, 10): make_image("PNG", size)
