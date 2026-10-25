"""Errors raised by the scraper adapter (spec section 6.7).

A `HostFailureError` carries how a request to the site failed (`domain.hosts.FailureKind`), so
the worker can pause the host and decide whether the work is retried.
"""

from typing import ClassVar

from novelhog.domain.hosts import FailureKind


class InfrastructureError(Exception):
    """FlareSolverr is unreachable; not the site's fault, so the work is retried unchanged."""


class SiteError(Exception):
    """The site or the scraper failed: HTTP status, decode failure, empty TOC, missing content."""


class HostFailureError(Exception):
    """A request failed in a way that says something about the host; `retry_after` in seconds."""

    kind: ClassVar[FailureKind]

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class SiteUnreachable(HostFailureError):  # noqa: N818 - named by the spec
    """A direct request to the site was refused, failed DNS or timed out connecting."""

    kind = FailureKind.UNREACHABLE


class SiteThrottled(HostFailureError, SiteError):  # noqa: N818 - matches its siblings
    """The site answered 429 or 503: it wants fewer requests, for `retry_after` if it said."""

    kind = FailureKind.THROTTLED


class SiteChallenged(HostFailureError, SiteError):  # noqa: N818 - matches its siblings
    """A Cloudflare challenge that FlareSolverr could not solve."""

    kind = FailureKind.CHALLENGED


class SiteTransient(HostFailureError, SiteError):  # noqa: N818 - matches its siblings
    """A server error, read timeout or dropped connection; the same request may work later."""

    kind = FailureKind.TRANSIENT


class TocLoopError(SiteError):
    """A table of contents asked for more pages than allowed or for the same page twice."""
