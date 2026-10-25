"""Host failure policy: when a failing host is paused, and how chapters back off.

A host's `reach_after` says when it may be contacted again. A throttled or challenged host is
paused at once; an unreachable one after `threshold` consecutive failures. Each pause doubles
from the kind's base up to `HOST_PAUSE_CAP`; a `Retry-After` the site sent wins over the base.
Transient and permanent failures never pause the host.
"""

from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum


class FailureKind(StrEnum):
    """How a request to a host failed."""

    THROTTLED = "throttled"  # 429 or 503
    CHALLENGED = "challenged"  # a Cloudflare challenge nobody solved
    UNREACHABLE = "unreachable"  # refused, DNS failure or connect timeout
    TRANSIENT = "transient"  # another 5xx, read timeout or reset
    PERMANENT = "permanent"  # the site answered, but the page is unusable


@dataclass(frozen=True)
class _Pause:
    threshold: int
    base: timedelta


HOST_PAUSE_CAP = timedelta(hours=1)
HOST_WAIT_WINDOW = timedelta(seconds=30)
CHAPTER_RETRY_BASE = timedelta(minutes=1)
CHAPTER_MAX_RETRIES = 5

_PAUSES: dict[FailureKind, _Pause] = {
    FailureKind.THROTTLED: _Pause(threshold=1, base=timedelta(minutes=1)),
    FailureKind.CHALLENGED: _Pause(threshold=1, base=timedelta(minutes=15)),
    FailureKind.UNREACHABLE: _Pause(threshold=2, base=timedelta(minutes=5)),
}


def pause_for(kind: FailureKind, failures: int, *, retry_after: float | None) -> timedelta | None:
    """How long to pause a host after its `failures`-th consecutive failure; None for no pause."""
    pause = _PAUSES.get(kind)
    if pause is None or failures < pause.threshold:
        return None
    if retry_after is not None:
        return min(timedelta(seconds=max(retry_after, 0)), HOST_PAUSE_CAP)
    doublings = min(failures - pause.threshold, 16)  # the cap is reached long before
    return min(pause.base * (1 << doublings), HOST_PAUSE_CAP)


def chapter_retry_delay(retries: int) -> timedelta:
    """How long a chapter waits before its next try, after `retries` counted retries."""
    return CHAPTER_RETRY_BASE * (1 << min(retries, 16))
