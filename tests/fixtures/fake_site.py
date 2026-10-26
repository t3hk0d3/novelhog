"""A local HTTP site for hosts whose scraper config does not force FlareSolverr."""

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import TracebackType
from typing import Any, Self


class FakeSite:
    """Serves `pages` (path to HTML) on 127.0.0.1 and answers 404 for any other path.

    Every requested path is kept in `requests`. With `challenge` set, every request gets a
    Cloudflare challenge instead: `"header"` is a 403 with `cf-mitigated: challenge`, `"body"` a
    503 from `Server: cloudflare` with a "Just a moment..." page. `reply` (status, headers, body)
    answers every request with exactly that, for responses that are not challenges. A request
    carrying the clearance in `cleared_by` (the `cf_clearance` value and the user agent) is served
    despite `challenge`; every request's headers are kept in `headers`.
    """

    def __init__(
        self,
        pages: dict[str, str],
        challenge: str | None = None,
        reply: tuple[int, dict[str, str], str] | None = None,
        cleared_by: tuple[str, str] | None = None,
    ) -> None:
        self.pages = pages
        self.challenge = challenge
        self.cleared_by = cleared_by
        self.headers: list[dict[str, str]] = []
        self.reply = reply
        self.requests: list[str] = []
        site = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - http.server naming
                site.requests.append(self.path)
                site.headers.append(dict(self.headers.items()))
                html = site.pages.get(self.path)
                status = 200 if html is not None else 404
                headers: dict[str, str] = {}
                if site.cleared_by is not None and self._cleared(*site.cleared_by):
                    pass
                elif site.challenge == "header":
                    status, html, headers = 403, "denied", {"cf-mitigated": "challenge"}
                elif site.challenge == "body":
                    status, html = 503, "<html><title>Just a moment...</title></html>"
                    headers = {}
                if site.reply is not None:
                    status, headers, html = site.reply
                payload = (html or "not found").encode("utf-8")
                self.send_response(status)
                for name, value in headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def _cleared(self, token: str, user_agent: str) -> bool:
                cookies = self.headers.get("Cookie", "")
                return (
                    f"cf_clearance={token}" in cookies
                    and self.headers.get("User-Agent") == user_agent
                )

            def version_string(self) -> str:
                return "cloudflare" if site.challenge == "body" else super().version_string()

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                """Keep test output quiet."""

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(
            target=lambda: self._server.serve_forever(poll_interval=0.01), daemon=True
        )

    @property
    def url(self) -> str:
        """Base URL, e.g. `http://127.0.0.1:41234`."""
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def __enter__(self) -> Self:
        self._thread.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)
