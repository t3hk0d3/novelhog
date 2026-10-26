"""Request hooks on the scraper's `RequestHelper` (spec sections 6.7 and 6.8).

Two jobs share the one patched `get_url_content`: the TOC loop guard (a per-operation page
budget) and the Cloudflare fallback (a direct request that meets a challenge is retried through
FlareSolverr, and later requests for that host go through it too). The patched request methods
add the outbound guard: a direct request goes through `net.GuardedSession` (every redirect hop is
checked), and a URL's host is checked before the URL is handed to FlareSolverr.
"""

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

import requests

from novelhog import log, net
from novelhog.scraper_adapter.errors import SiteError, TocLoopError
from novelhog.scraper_adapter.scraper import (
    CommonGetRequestError,
    RequestHelper,
    ResponseIsEmptyError,
    ScraperError,
)

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


class ThrottledResponseError(ScraperError):  # type: ignore[misc]
    """A direct request got 429 or 503. Not retried by the scraper: retrying is what it refuses.

    `retry_after` is the site's `Retry-After` in seconds, None when absent or unreadable.
    """

    def __init__(self, message: str, retry_after: float | None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class ServerResponseError(CommonGetRequestError):  # type: ignore[misc]
    """A direct request got another 5xx; the scraper retries it like its own request failures."""


_THROTTLE_STATUSES = (429, 503)


def _retry_after(value: str | None) -> float | None:
    """Seconds from a `Retry-After` header: a number of seconds or an HTTP date."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = parsedate_to_datetime(value)
    except TypeError, ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max((when - datetime.now(UTC)).total_seconds(), 0.0)


@dataclass(frozen=True)
class _Outbound:
    allow_private: bool
    resolver: Callable[[str], list[str]]


_active: ContextVar[_Budget | None] = ContextVar("novelhog_page_budget", default=None)
_GUARDED = _Outbound(allow_private=False, resolver=net.default_resolver)
_outbound: ContextVar[_Outbound] = ContextVar("novelhog_outbound", default=_GUARDED)
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


def _blocked(host: str) -> SiteError:
    return SiteError(f"Blocked non-public address: {host}")


def _unresolvable(url: str, error: net.Unresolvable) -> CommonGetRequestError:
    """A failed name lookup, raised like the scraper's own request failures (so it is retried)."""
    return CommonGetRequestError(f'Request Failed for URL "{url}": {error}')


def _guarded_common_request(self: RequestHelper, url: str) -> str:
    """The direct request through the outbound guard; a challenge raises at once.

    Mirrors the scraper's own direct request (any status but 200 or an empty body fails).
    """
    outbound = _outbound.get()
    with net.GuardedSession(
        allow_private=outbound.allow_private, resolver=outbound.resolver
    ) as session:
        try:
            response = session.get(url, timeout=self.request_timeout, cookies=self.request_cookies)
        except net.NonPublic as error:
            raise _blocked(error.host) from error
        except net.Unresolvable as error:
            raise _unresolvable(url, error) from error
        except net.Refused as error:
            raise SiteError(str(error)) from error
        except requests.exceptions.RequestException as error:
            msg = f'Request Failed for URL "{url}": {error}'
            raise CommonGetRequestError(msg) from error
    if response.status_code != 200:
        msg = f'Request Failed for URL "{url}", Status Code is {response.status_code}.'
        if _is_challenge(response):
            raise CloudflareChallengeError(msg)
        if response.status_code in _THROTTLE_STATUSES:
            raise ThrottledResponseError(msg, _retry_after(response.headers.get("Retry-After")))
        if response.status_code >= 500:
            raise ServerResponseError(msg)
        raise CommonGetRequestError(msg)
    content = response.text
    if content == "":
        msg = f'Request for URL "{url}" returned no content.'
        raise ResponseIsEmptyError(msg)
    return content


def _guarded_flaresolverr_request(self: RequestHelper, url: str) -> str:
    """Check the URL's host before FlareSolverr fetches it, then send the request."""
    outbound = _outbound.get()
    if not outbound.allow_private:
        host = urlparse(url).hostname or ""
        try:
            net.check_host(host, allow_private=False, resolver=outbound.resolver)
        except net.NonPublic as error:
            raise _blocked(error.host) from error
        except net.Unresolvable as error:
            raise _unresolvable(url, error) from error
    content: str = RequestHelper.novelhog_original_flaresolverr_request(self, url)
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
    RequestHelper._get_url_common_request = _guarded_common_request
    RequestHelper.novelhog_original_flaresolverr_request = (
        RequestHelper._get_url_flaresolverr_request
    )
    RequestHelper._get_url_flaresolverr_request = _guarded_flaresolverr_request


def reset_fallback() -> None:
    """Forget every host routed through FlareSolverr (tests start each case without any)."""
    with _sticky_lock:
        _sticky_hosts.clear()


def fallback_hosts() -> frozenset[str]:
    """The hosts currently routed through FlareSolverr."""
    with _sticky_lock:
        return frozenset(_sticky_hosts)


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
def outbound_guard(
    *, allow_private: bool, resolver: Callable[[str], list[str]] = net.default_resolver
) -> Iterator[None]:
    """Check chapter and TOC hosts in this block with `resolver`; `allow_private` skips it."""
    token = _outbound.set(_Outbound(allow_private=allow_private, resolver=resolver))
    try:
        yield
    finally:
        _outbound.reset(token)


@contextmanager
def flaresolverr_fallback(flaresolverr_url: str) -> Iterator[None]:
    """Let direct requests in this block fall back to FlareSolverr at `flaresolverr_url`."""
    token = _flaresolverr_url.set(flaresolverr_url)
    try:
        yield
    finally:
        _flaresolverr_url.reset(token)
