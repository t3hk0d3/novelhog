"""Request hooks on the scraper's `RequestHelper` (spec section 6.7).

Two jobs share the one patched `get_url_content`: the TOC loop guard (a per-operation page
budget) and the Cloudflare fallback (a direct request that meets a challenge is retried through
FlareSolverr, and later requests for that host go through it too).
"""

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from urllib.parse import urlparse

import requests

from novelhog import log
from novelhog.scraper_adapter.errors import TocLoopError
from novelhog.scraper_adapter.scraper import CommonGetRequestError, RequestHelper, ScraperError

_CHALLENGE_STATUSES = (403, 503)
_CHALLENGE_MARKERS = ("just a moment", "cf-chl")


@dataclass
class _Budget:
    max_pages: int
    seen: set[str] = field(default_factory=set)

    def record(self, url: str) -> None:
        if url in self.seen:
            msg = f"The table of contents requested {url} twice"
            raise TocLoopError(msg)
        if len(self.seen) >= self.max_pages:
            msg = f"The table of contents has more than {self.max_pages} pages"
            raise TocLoopError(msg)
        self.seen.add(url)


class CloudflareChallengeError(ScraperError):  # type: ignore[misc]
    """A direct request got a Cloudflare challenge page. Not retried directly (it would repeat)."""


_active: ContextVar[_Budget | None] = ContextVar("novelhog_page_budget", default=None)
_flaresolverr_url: ContextVar[str | None] = ContextVar("novelhog_flaresolverr_url", default=None)
_sticky_hosts: set[str] = set()  # hosts routed through FlareSolverr for the rest of the process
_sticky_lock = threading.Lock()


def _host_of(url: str) -> str:
    return (urlparse(url).hostname or "").lower().removeprefix("www.")


def _is_challenge(response: requests.Response) -> bool:
    if response.status_code not in _CHALLENGE_STATUSES:
        return False
    if response.headers.get("cf-mitigated", "").lower() == "challenge":
        return True
    if response.headers.get("server", "").lower() != "cloudflare":
        return False
    body = response.text.lower()
    return any(marker in body for marker in _CHALLENGE_MARKERS)


def _guarded_common_request(self: RequestHelper, url: str) -> str:
    """The direct request; a challenge response raises `CloudflareChallengeError` at once."""
    try:
        content: str = RequestHelper.novelhog_original_common_request(self, url)
    except CommonGetRequestError as error:
        cause = error.__context__
        if isinstance(cause, requests.HTTPError) and _is_challenge(cause.response):
            raise CloudflareChallengeError(str(error)) from error
        raise
    return content


def _enable_sticky(host: str) -> None:
    with _sticky_lock:
        first = host not in _sticky_hosts
        _sticky_hosts.add(host)
    if first:
        log.get().info("flaresolverr_fallback_enabled", host=host)


def _guarded_get_url_content(self: RequestHelper, url: str) -> str:
    budget = _active.get()
    if budget is not None:
        budget.record(url)
    flaresolverr = _flaresolverr_url.get()
    host = _host_of(url)
    if flaresolverr is not None and not self.use_flaresolverr and host in _sticky_hosts:
        self.enable_flaresolverr(flaresolverr)
    try:
        result: str = RequestHelper.novelhog_original_get_url_content(self, url)
    except CloudflareChallengeError:
        if flaresolverr is None:
            raise
        # Retry once through FlareSolverr; the host is marked only if that works.
        self.enable_flaresolverr(flaresolverr)
        try:
            result = RequestHelper.novelhog_original_get_url_content(self, url)
        except Exception:
            self.use_flaresolverr = False
            raise
        _enable_sticky(host)
    return result


# Installed once, even if this module is imported twice; it does nothing outside the contexts.
if not hasattr(RequestHelper, "novelhog_original_get_url_content"):
    RequestHelper.novelhog_original_get_url_content = RequestHelper.get_url_content
    RequestHelper.get_url_content = _guarded_get_url_content
    RequestHelper.novelhog_original_common_request = RequestHelper._get_url_common_request
    RequestHelper._get_url_common_request = _guarded_common_request


@contextmanager
def page_budget(max_pages: int) -> Iterator[None]:
    """Count the scraper's page requests in this block; raise `TocLoopError` on a loop or overrun.

    A page counts once however often the scraper retries it. The budget follows the current
    thread (and async task), so concurrent operations do not share it.
    """
    token = _active.set(_Budget(max_pages))
    try:
        yield
    finally:
        _active.reset(token)


@contextmanager
def flaresolverr_fallback(flaresolverr_url: str) -> Iterator[None]:
    """Let direct requests in this block fall back to FlareSolverr at `flaresolverr_url`."""
    token = _flaresolverr_url.set(flaresolverr_url)
    try:
        yield
    finally:
        _flaresolverr_url.reset(token)
