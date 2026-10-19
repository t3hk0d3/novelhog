"""Structured JSON logging to stdout (spec section 11)."""

import logging
from typing import Literal

import structlog
from structlog.typing import FilteringBoundLogger


def setup(service: Literal["api", "worker", "cli"]) -> None:
    """Configure structlog to emit one JSON object per line on stdout, tagged with `service`."""
    structlog.reset_defaults()
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(service=service)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True, key="timestamp"),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=False,
    )


def get(**bound: str) -> FilteringBoundLogger:
    """Return a logger with `bound` fields (novel_id, chapter_id, worker_id, host) attached."""
    logger: FilteringBoundLogger = structlog.get_logger().bind(**bound)
    return logger
