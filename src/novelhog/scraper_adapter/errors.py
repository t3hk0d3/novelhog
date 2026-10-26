"""Errors raised by the scraper adapter (spec section 6.7)."""


class InfrastructureError(Exception):
    """FlareSolverr is unreachable; not the site's fault, so the work is retried unchanged."""


class SiteUnreachable(InfrastructureError):  # noqa: N818 - named by the spec
    """A direct request to the site was refused, failed DNS or timed out connecting.

    The worker retries it like an infrastructure error until a host has failed too often.
    """


class SiteError(Exception):
    """The site or the scraper failed: HTTP status, decode failure, empty TOC, missing content."""


class TocLoopError(SiteError):
    """A table of contents asked for more pages than allowed or for the same page twice."""
