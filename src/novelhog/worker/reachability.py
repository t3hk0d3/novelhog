"""Consecutive site-unreachable failures per host (spec section 6.1)."""

SITE_UNREACHABLE_RETRIES = 3


class Reachability:
    """Counts consecutive `SiteUnreachable` failures per host, in memory, for one worker process.

    Below the threshold such a failure is retried like an infrastructure error; from the
    `SITE_UNREACHABLE_RETRIES`-th on it counts as a site error. A success resets the host.
    """

    def __init__(self) -> None:
        self._failures: dict[str, int] = {}

    def failed(self, host: str) -> bool:
        """Count one more unreachable failure; True when the host has now failed too often."""
        count = self._failures.get(host, 0) + 1
        self._failures[host] = count
        return count >= SITE_UNREACHABLE_RETRIES

    def succeeded(self, host: str) -> None:
        """A request to the host worked; start counting again."""
        self._failures.pop(host, None)
