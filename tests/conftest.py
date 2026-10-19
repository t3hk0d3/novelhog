"""Shared pytest fixtures."""

from collections.abc import Iterator

import pytest
import structlog


@pytest.fixture(autouse=True)
def _reset_structlog() -> Iterator[None]:
    """Restore structlog's defaults and context variables after every test."""
    yield
    structlog.reset_defaults()
    structlog.contextvars.clear_contextvars()
