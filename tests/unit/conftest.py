"""Fixtures for the network and image unit tests."""

from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.fixtures.images.helpers import Server, make_cert


@pytest.fixture
def http_server() -> Iterator[Server]:
    server = Server()
    server.start()
    yield server
    server.stop()


@pytest.fixture
def tls_files(tmp_path: Path) -> tuple[Path, Path]:
    """Self-signed (cert, key) for the host name ``site.test``."""
    return make_cert(tmp_path, "site.test")


@pytest.fixture
def tls_server(tls_files: tuple[Path, Path]) -> Iterator[Server]:
    server = Server(tls=tls_files)
    server.start()
    yield server
    server.stop()
