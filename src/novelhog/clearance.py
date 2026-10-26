"""Cloudflare clearances from FlareSolverr, kept in memory for the worker's direct requests.

FlareSolverr solves a Cloudflare challenge in a browser and returns the page as HTML, so it cannot
fetch an image and is slow for pages. Its solution also carries the browser's cookies and user
agent: a direct request that sends the `cf_clearance` cookie with that same user agent is let
through. The cache keeps the latest clearance per cookie domain for this worker process only.
"""

import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from http.cookiejar import Cookie
from typing import Any
from urllib.parse import urlparse

from requests.cookies import RequestsCookieJar

CLEARANCE_COOKIE = "cf_clearance"


@dataclass(frozen=True)
class Clearance:
    """The cookies and user agent a direct request to `domain` (or a subdomain) must send."""

    domain: str
    user_agent: str
    cookies: tuple[Cookie, ...]
    expires_at: float | None  # epoch seconds; None when the clearance cookie gave no expiry

    def jar(self, extra: Mapping[str, str] | None = None) -> RequestsCookieJar:
        """A fresh jar holding the cookies, each bound to its own domain, plus `extra` for any."""
        jar = RequestsCookieJar()
        for name, value in (extra or {}).items():
            jar.set(name, value)
        for cookie in self.cookies:
            jar.set_cookie(cookie)
        return jar


def _domain(value: str) -> str:
    return value.lower().lstrip(".")


def _matches(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


def _expiry(raw: Mapping[str, Any]) -> float | None:
    value = raw.get("expiry", raw.get("expires"))
    if not isinstance(value, int | float) or value <= 0:
        return None  # a session cookie (FlareSolverr reports -1)
    return float(value)


def _cookie(raw: Mapping[str, Any]) -> Cookie:
    """A domain cookie (sent to the domain and its subdomains) from FlareSolverr's cookie dict."""
    return Cookie(
        version=0,
        name=str(raw["name"]),
        value=str(raw["value"]),
        port=None,
        port_specified=False,
        domain="." + _domain(str(raw["domain"])),
        domain_specified=True,
        domain_initial_dot=True,
        path=str(raw.get("path") or "/"),
        path_specified=True,
        secure=bool(raw.get("secure", False)),
        expires=None,
        discard=True,
        comment=None,
        comment_url=None,
        rest={},
    )


class ClearanceCache:
    """The latest Cloudflare clearance per cookie domain, safe to share between threads."""

    def __init__(self, now: Callable[[], float] = time.time) -> None:
        self._now = now
        self._entries: dict[str, Clearance] = {}
        self._lock = threading.Lock()

    def record(self, user_agent: str, cookies: Sequence[Mapping[str, Any]]) -> Clearance | None:
        """Keep a FlareSolverr solution's cookies; None (and nothing kept) without `cf_clearance`.

        Every cookie on the clearance cookie's domain or a parent of it is kept with it.
        """
        found = next((c for c in cookies if c.get("name") == CLEARANCE_COOKIE), None)
        if found is None or not user_agent or not found.get("domain"):
            return None
        domain = _domain(str(found["domain"]))
        kept = tuple(
            _cookie(c)
            for c in cookies
            if c.get("domain") and _matches(domain, _domain(c["domain"]))
        )
        clearance = Clearance(domain, user_agent, kept, _expiry(found))
        with self._lock:
            self._entries[domain] = clearance
        return clearance

    def for_url(self, url: str) -> Clearance | None:
        """The live clearance whose domain covers the URL's host; the most specific one wins."""
        host = (urlparse(url).hostname or "").lower()
        now = self._now()
        with self._lock:
            for domain, entry in list(self._entries.items()):
                if entry.expires_at is not None and entry.expires_at <= now:
                    del self._entries[domain]
            matching = [e for d, e in self._entries.items() if _matches(host, d)]
        return max(matching, key=lambda e: len(e.domain), default=None)

    def forget(self, clearance: Clearance) -> None:
        """Drop `clearance` (the site refused it); a newer one recorded since is kept."""
        with self._lock:
            if self._entries.get(clearance.domain) is clearance:
                del self._entries[clearance.domain]
