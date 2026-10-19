"""The one place `web_novel_scraper` is imported; tames its import-time side effects.

The scraper's logger setup and one invalid-escape SyntaxWarning would otherwise leak into the
worker's output (spec section 11: structured JSON on stdout).
"""

import logging
import warnings

from novelhog import log

_LOGGER_NAME = "main"  # the name `web_novel_scraper.logger_manager` logs under


class _StructlogHandler(logging.Handler):
    """Forwards the scraper's WARNING and above records to structlog as plain-text events."""

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)

    def emit(self, record: logging.LogRecord) -> None:
        message = record.getMessage()
        error = record.exc_info[1] if record.exc_info else None
        if error is not None:
            message = f"{message}: {type(error).__name__}: {error}"
        fields = {"scraper_module": str(getattr(record, "operation", record.name))}
        getattr(log.get(), record.levelname.lower())("scraper_log", message=message, **fields)


def take_over_scraper_logging() -> None:
    """Replace the scraper's stderr handler with one that feeds structlog (idempotent)."""
    logger = logging.getLogger(_LOGGER_NAME)
    ours = [h for h in logger.handlers if isinstance(h, _StructlogHandler)]
    for handler in list(logger.handlers):
        if handler not in ours:
            logger.removeHandler(handler)
    if not ours:
        logger.addHandler(_StructlogHandler())
    logger.propagate = False


# Ours goes in before the scraper loads: its logger setup adds a handler only when none exists.
take_over_scraper_logging()

with warnings.catch_warnings():
    warnings.simplefilter("ignore", SyntaxWarning)
    from web_novel_scraper.config_manager import ScraperConfig
    from web_novel_scraper.custom_processor.custom_processor import (
        CustomProcessor,
        ProcessorRegistry,
    )
    from web_novel_scraper.exceptions import FlaresolverrConnectionError, ScraperError
    from web_novel_scraper.models import Chapter
    from web_novel_scraper.novel_scraper import Novel
    from web_novel_scraper.request_helper import RequestHelper

take_over_scraper_logging()

__all__ = [
    "Chapter",
    "CustomProcessor",
    "FlaresolverrConnectionError",
    "Novel",
    "ProcessorRegistry",
    "RequestHelper",
    "ScraperConfig",
    "ScraperError",
    "take_over_scraper_logging",
]
