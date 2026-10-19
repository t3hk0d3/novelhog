"""A local fake of FlareSolverr's `POST /v1` API that serves pages from a URL-to-HTML map."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import TracebackType
from typing import Any, Self

HTML_DIR = Path(__file__).parent / "html"


def load_html(name: str) -> str:
    """Return the text of the fixture page `tests/fixtures/html/<name>`."""
    return (HTML_DIR / name).read_text(encoding="utf-8")


class FakeFlareSolverr:
    """Serves `sessions.create`, `sessions.destroy` and `request.get` on 127.0.0.1.

    `pages` maps a requested URL to the HTML FlareSolverr would return for it. A URL missing from
    the map gets an error response, like a failed navigation. Every command received is kept in
    `commands` (the decoded JSON bodies) so tests can inspect what the scraper sent.
    """

    def __init__(self, pages: dict[str, str]) -> None:
        self.pages = pages
        self.commands: list[dict[str, Any]] = []
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self._server.daemon_threads = True
        self._thread = threading.Thread(
            target=lambda: self._server.serve_forever(poll_interval=0.01), daemon=True
        )

    @property
    def url(self) -> str:
        """Base URL of the server, e.g. `http://127.0.0.1:41234`."""
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    @property
    def requested_urls(self) -> list[str]:
        """The page URLs of every `request.get` received, in order."""
        return [c["url"] for c in self.commands if c.get("cmd") == "request.get"]

    def start(self) -> None:
        """Start serving in a daemon thread."""
        self._thread.start()

    def stop(self) -> None:
        """Stop serving and release the port."""
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    def __enter__(self) -> Self:
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.stop()

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - http.server naming
                if self.path != "/v1":
                    self._send(404, {"status": "error", "message": "not found"})
                    return
                length = int(self.headers.get("Content-Length", "0"))
                command: dict[str, Any] = json.loads(self.rfile.read(length))
                fake.commands.append(command)
                status, body = fake._respond(command)
                self._send(status, body)

            def _send(self, status: int, body: dict[str, Any]) -> None:
                payload = json.dumps(body).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                """Keep test output quiet."""

        return Handler

    def _respond(self, command: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        match command.get("cmd"):
            case "sessions.create" | "sessions.destroy":
                return 200, {"status": "ok", "message": "", "session": command.get("session")}
            case "request.get":
                url = command["url"]
                if url not in self.pages:
                    return 500, {"status": "error", "message": f"Error solving {url}"}
                solution = {"url": url, "status": 200, "response": self.pages[url]}
                return 200, {"status": "ok", "message": "", "solution": solution}
            case other:
                return 400, {"status": "error", "message": f"Unknown command {other}"}
