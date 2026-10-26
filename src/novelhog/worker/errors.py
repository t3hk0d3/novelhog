"""Error summaries and host-failure logging shared by the worker's processors."""

from structlog.typing import FilteringBoundLogger

from novelhog.domain.hosts import FailureKind
from novelhog.repo.hosts import HostState

_SUMMARY_LIMIT = 300


def log_host_paused(
    logger: FilteringBoundLogger, host: str, kind: FailureKind, state: HostState
) -> None:
    """Log `host_paused`: the host is not contacted again before `reach_after`."""
    logger.warning(
        "host_paused",
        host=host,
        kind=kind.value,
        failures=state.failures,
        reach_after=state.paused_until,
    )


def unreachable_message(error: Exception) -> str:
    """Return `Site unreachable: <message>` on one line, cut to the same length as `summarize`."""
    return "Site unreachable: " + " ".join(str(error).split())[:_SUMMARY_LIMIT]


def summarize(error: Exception) -> str:
    """Return `<ExceptionType>: <message>` on one line, cut to a short length."""
    text = " ".join(str(error).split())[:_SUMMARY_LIMIT]
    return f"{type(error).__name__}: {text}"
