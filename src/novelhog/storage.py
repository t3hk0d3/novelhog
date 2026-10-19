"""Static storage for covers and export files (spec §8.3)."""

import contextlib
import hashlib
import os
import shutil
import tempfile
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Protocol

from novelhog.domain.urls import static_url


class InvalidKey(ValueError):  # noqa: N818 - name fixed by the task contract
    """A storage key that is empty, absolute, contains ``..`` or a backslash."""


class StaticStorage(Protocol):
    """Where covers and export files live; the worker writes, the web server serves."""

    def put(self, key: str, data: bytes, mime: str, download_name: str | None = None) -> None: ...

    def put_file(
        self, key: str, path: Path, mime: str, download_name: str | None = None
    ) -> None: ...

    def get(self, key: str) -> bytes | None: ...

    def delete(self, key: str) -> None: ...

    def url(self, key: str) -> str: ...


def cover_key(novel_id: str, data: bytes, ext: str) -> str:
    """Return the content-addressed cover key ``covers/<novel_id>/<sha256[:16]>.<ext>``."""
    digest = hashlib.sha256(data).hexdigest()[:16]
    return f"covers/{novel_id}/{digest}.{ext}"


def _check_key(key: str) -> None:
    if not key or "\\" in key or "\0" in key or PurePosixPath(key).is_absolute():
        raise InvalidKey(key)
    if any(segment in ("", ".", "..") for segment in key.split("/")):
        raise InvalidKey(key)


class LocalStaticStorage:
    """Stores files under a root directory; ``mime`` and ``download_name`` are set by the web
    server from the key, so they are not stored here."""

    def __init__(self, root: Path, base_url: str) -> None:
        self._root = root
        self._base_url = base_url

    def _path(self, key: str) -> Path:
        _check_key(key)
        return self._root / key

    def put(self, key: str, data: bytes, mime: str, download_name: str | None = None) -> None:
        """Write ``data`` atomically under ``key``."""

        def write(handle: int) -> None:
            with os.fdopen(handle, "wb", closefd=False) as out:
                out.write(data)

        self._write_atomically(self._path(key), write)

    def put_file(self, key: str, path: Path, mime: str, download_name: str | None = None) -> None:
        """Copy the content of ``path`` atomically under ``key``."""

        def write(handle: int) -> None:
            with path.open("rb") as source, os.fdopen(handle, "wb", closefd=False) as out:
                shutil.copyfileobj(source, out)

        self._write_atomically(self._path(key), write)

    def get(self, key: str) -> bytes | None:
        """Return the stored bytes, or ``None`` when the key is missing."""
        try:
            return self._path(key).read_bytes()
        except FileNotFoundError, NotADirectoryError, IsADirectoryError:
            return None

    def delete(self, key: str) -> None:
        """Remove the file; a missing file is ignored."""
        with contextlib.suppress(FileNotFoundError):
            self._path(key).unlink()

    def url(self, key: str) -> str:
        """Return the public URL of ``key``."""
        return static_url(self._base_url, key)

    @staticmethod
    def _write_atomically(target: Path, write: Callable[[int], None]) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        handle, temp_name = tempfile.mkstemp(dir=target.parent, prefix=".tmp-")
        try:
            try:
                write(handle)
            finally:
                os.close(handle)
            os.replace(temp_name, target)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temp_name)
            raise
