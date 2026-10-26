"""The worker's FlareSolverr client: one reused browser session, and the clearances it earns.

The session (`novelhog-<worker_id>`) is created on the first request and reused for every later
one, so a Cloudflare challenge is solved once per site rather than once per page; FlareSolverr
replaces its browser after `SESSION_TTL_MINUTES`. A session FlareSolverr lost (it restarted) is
created again once. Every solution's Cloudflare clearance is recorded in `clearances`, for the
worker's direct requests (spec sections 6.7 and 8.2). `close` destroys the session.
"""

import threading
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import requests

from novelhog import log
from novelhog.clearance import Clearance, ClearanceCache

REQUEST_TIMEOUT = 60.0  # seconds FlareSolverr may take for one page, challenge included
SESSION_TTL_MINUTES = 30


class FlareSolverrUnavailable(Exception):  # noqa: N818 - says what it is, like `Refused`
    """FlareSolverr could not be reached."""


class FlareSolverrFailed(Exception):  # noqa: N818 - says what it is, like `Refused`
    """FlareSolverr answered but could not load the page (an unsolved challenge, a timeout)."""


@dataclass(frozen=True)
class Solution:
    """A page FlareSolverr loaded: its HTML and the clearance recorded from it, if any."""

    html: str
    clearance: Clearance | None


class FlareSolverr:
    """Loads pages through one FlareSolverr session; safe to share, requests are serialised."""

    def __init__(
        self,
        url: str,
        worker_id: str,
        *,
        clearances: ClearanceCache | None = None,
        timeout: float = REQUEST_TIMEOUT,
    ) -> None:
        self._url = url.rstrip("/") + "/v1"
        self._session_name = f"novelhog-{worker_id}"
        self.clearances = ClearanceCache() if clearances is None else clearances
        self._timeout = timeout
        self._session: str | None = None
        self._lock = threading.Lock()

    def get(self, url: str, cookies: Mapping[str, str] | None = None) -> Solution:
        """Load `url` in the session, sending `cookies`; record and return its clearance.

        Raises `FlareSolverrUnavailable` or `FlareSolverrFailed`.
        """
        with self._lock:
            try:
                solution = self._get(url, cookies)
            except FlareSolverrFailed as error:
                if "session" not in str(error).lower():
                    raise
                self._session = None  # FlareSolverr lost it; open a new one once
                solution = self._get(url, cookies)
        html = solution.get("response")
        if not isinstance(html, str) or not html:
            msg = f"FlareSolverr returned no content for {url}"
            raise FlareSolverrFailed(msg)
        clearance = self.clearances.record(
            str(solution.get("userAgent") or ""), solution.get("cookies") or []
        )
        if clearance is not None:
            log.get().info("clearance_solved", domain=clearance.domain)
        return Solution(html, clearance)

    def close(self) -> None:
        """Destroy the session, if one was opened; a failure is logged and ignored."""
        with self._lock:
            if self._session is None:
                return
            session, self._session = self._session, None
            try:
                self._post({"cmd": "sessions.destroy", "session": session})
            except FlareSolverrUnavailable, FlareSolverrFailed:
                log.get().warning("flaresolverr_session_not_destroyed", session=session)

    def _get(self, url: str, cookies: Mapping[str, str] | None) -> dict[str, Any]:
        if self._session is None:
            # Creating a session that already exists (left by an earlier run) reuses it.
            self._post({"cmd": "sessions.create", "session": self._session_name})
            self._session = self._session_name
        body = self._post(
            {
                "cmd": "request.get",
                "url": url,
                "session": self._session,
                "session_ttl_minutes": SESSION_TTL_MINUTES,
                "maxTimeout": int(self._timeout * 1000),
                "cookies": [{"name": k, "value": v} for k, v in (cookies or {}).items()],
            }
        )
        solution = body.get("solution")
        return solution if isinstance(solution, dict) else {}

    def _post(self, command: dict[str, Any]) -> dict[str, Any]:
        try:
            response = requests.post(self._url, json=command, timeout=self._timeout + 10)
        except requests.ConnectionError as error:  # a connect timeout included
            msg = f"Could not connect to FlareSolverr at {self._url}: {error}"
            raise FlareSolverrUnavailable(msg) from error
        except requests.RequestException as error:
            raise FlareSolverrFailed(str(error)) from error
        try:
            body = response.json()
        except ValueError:
            body = {}
        if not response.ok or not isinstance(body, dict) or body.get("status") != "ok":
            message = body.get("message") if isinstance(body, dict) else None
            msg = f"FlareSolverr {command['cmd']} failed: {message or response.status_code}"
            raise FlareSolverrFailed(msg)
        return body
