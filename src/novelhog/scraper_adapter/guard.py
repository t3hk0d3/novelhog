"""Request hooks on the scraper's `RequestHelper` (spec sections 6.7 and 6.8).

The patched `get_url_content` adds the TOC loop guard (a per-operation page budget). The patched
request methods add the outbound guard and the Cloudflare handling:

- a direct request goes through `net.GuardedSession` (every redirect hop is checked) and sends the
  worker's cached Cloudflare clearance for the host, if any. When it meets a challenge anyway, the
  page is loaded through the worker's `FlareSolverr` instead, which records a fresh clearance for
  the next direct requests;
- a host the scraper's config forces through FlareSolverr is loaded in the worker's FlareSolverr
  session (never one of the scraper's own), after its host is checked.
"""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

import requests

from novelhog import net
from novelhog.clearance import Clearance
from novelhog.flaresolverr import FlareSolverr, FlareSolverrFailed, FlareSolverrUnavailable
from novelhog.scraper_adapter.errors import SiteError, TocLoopError
from novelhog.scraper_adapter.scraper import (
    CommonGetRequestError,
    FlaresolverrConnectionError,
    FlaresolverrRequestError,
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
    """A Cloudflare challenge that FlareSolverr did not solve (or no FlareSolverr). Not retried."""


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
_flaresolverr: ContextVar[FlareSolverr | None] = ContextVar("novelhog_flaresolverr", default=None)


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


def _direct(self: RequestHelper, url: str, clearance: Clearance | None) -> requests.Response:
    """One direct request through the outbound guard, sending `clearance` when given."""
    outbound = _outbound.get()
    headers = None if clearance is None else {"User-Agent": clearance.user_agent}
    cookies = self.request_cookies if clearance is None else clearance.jar(self.request_cookies)
    with net.GuardedSession(
        allow_private=outbound.allow_private, resolver=outbound.resolver
    ) as session:
        try:
            return session.get(url, timeout=self.request_timeout, cookies=cookies, headers=headers)
        except net.NonPublic as error:
            raise _blocked(error.host) from error
        except net.Unresolvable as error:
            raise _unresolvable(url, error) from error
        except net.Refused as error:
            raise SiteError(str(error)) from error
        except requests.exceptions.RequestException as error:
            msg = f'Request Failed for URL "{url}": {error}'
            raise CommonGetRequestError(msg) from error


def _load(self: RequestHelper, solver: FlareSolverr, url: str) -> str:
    """The page as FlareSolverr loads it in the worker's session, in the scraper's errors."""
    try:
        return solver.get(url, self.request_cookies).html
    except FlareSolverrUnavailable as error:
        raise FlaresolverrConnectionError(str(error)) from error
    except FlareSolverrFailed as error:
        raise FlaresolverrRequestError(str(error)) from error


def _guarded_common_request(self: RequestHelper, url: str) -> str:
    """The direct request; a Cloudflare challenge is handed to FlareSolverr.

    Mirrors the scraper's own direct request (any status but 200 or an empty body fails). A
    challenge despite a cached clearance forgets it. FlareSolverr failing to load the page raises
    `CloudflareChallengeError`; FlareSolverr being down raises its connection error.
    """
    solver = _flaresolverr.get()
    clearance = None if solver is None else solver.clearances.for_url(url)
    response = _direct(self, url, clearance)
    if response.status_code != 200:
        msg = f'Request Failed for URL "{url}", Status Code is {response.status_code}.'
        if _is_challenge(response):
            if solver is None:
                raise CloudflareChallengeError(msg)
            if clearance is not None:
                solver.clearances.forget(clearance)
            try:
                return _load(self, solver, response.url)
            except FlaresolverrRequestError as error:
                unsolved = f"{msg} FlareSolverr: {error}"
                raise CloudflareChallengeError(unsolved) from error
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
    """Check the URL's host, then load it in the worker's FlareSolverr session."""
    outbound = _outbound.get()
    if not outbound.allow_private:
        host = urlparse(url).hostname or ""
        try:
            net.check_host(host, allow_private=False, resolver=outbound.resolver)
        except net.NonPublic as error:
            raise _blocked(error.host) from error
        except net.Unresolvable as error:
            raise _unresolvable(url, error) from error
    solver = _flaresolverr.get()
    if solver is None:
        msg = "No FlareSolverr for a host forced through it"
        raise FlaresolverrConnectionError(msg)
    content = _load(self, solver, url)
    if content == "":
        msg = f'Request for URL "{url}" using Flaresolverr returned no content.'
        raise ResponseIsEmptyError(msg)
    return content


def _guarded_get_url_content(self: RequestHelper, url: str) -> str:
    budget = _active.get()
    if budget is not None:
        budget.record(url)
    result: str = RequestHelper.novelhog_original_get_url_content(self, url)
    return result


# Installed once, even if this module is imported twice; it does nothing outside the contexts.
if not hasattr(RequestHelper, "novelhog_original_get_url_content"):
    RequestHelper.novelhog_original_get_url_content = RequestHelper.get_url_content
    RequestHelper.get_url_content = _guarded_get_url_content
    RequestHelper._get_url_common_request = _guarded_common_request
    RequestHelper._get_url_flaresolverr_request = _guarded_flaresolverr_request


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
def flaresolverr_fallback(flaresolverr: FlareSolverr) -> Iterator[None]:
    """Load challenged and forced pages in this block through `flaresolverr`."""
    token = _flaresolverr.set(flaresolverr)
    try:
        yield
    finally:
        _flaresolverr.reset(token)
