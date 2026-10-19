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


@pytest.fixture(autouse=True)
def _restore_pillow_limit() -> Iterator[None]:
    """ImageFetcher sets Pillow's global pixel limit; undo it after every test."""
    from PIL import Image

    saved = Image.MAX_IMAGE_PIXELS
    yield
    Image.MAX_IMAGE_PIXELS = saved
