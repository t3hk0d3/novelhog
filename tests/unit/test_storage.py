"""Tests for static storage."""

import os
from collections.abc import Callable
from pathlib import Path

import pytest

from novelhog.domain.urls import static_url
from novelhog.storage import InvalidKey, LocalStaticStorage, cover_key

BASE = "http://example.test/static"
BAD_KEYS = ["../x", "a/../b", "/abs/x", "a\\b", "", "a\0b", "a/./b", "a//b", "a/b/"]


@pytest.fixture
def store(tmp_path: Path) -> LocalStaticStorage:
    return LocalStaticStorage(tmp_path / "static", BASE)


@pytest.fixture
def fail_replace(monkeypatch: pytest.MonkeyPatch) -> Callable[[], None]:
    """Return a function that makes ``os.replace`` fail from then on."""

    def boom(src: str, dst: Path) -> None:
        raise OSError

    def activate() -> None:
        monkeypatch.setattr("novelhog.storage.os.replace", boom)

    return activate


def _files(root: Path) -> list[Path]:
    return [p for p in root.rglob("*") if p.is_file()]


class TestPut__OnSuccess:
    def test_put__writes_bytes_and_creates_directories(
        self, store: LocalStaticStorage, tmp_path: Path
    ) -> None:
        store.put("exports/NV1/EX1/book.epub", b"data", "application/epub+zip", "book.epub")
        assert (tmp_path / "static/exports/NV1/EX1/book.epub").read_bytes() == b"data"

    def test_put_file__copies_content(self, store: LocalStaticStorage, tmp_path: Path) -> None:
        source = tmp_path / "src.bin"
        source.write_bytes(b"file-data")
        store.put_file("a/b.bin", source, "application/octet-stream")
        assert store.get("a/b.bin") == b"file-data"

    def test_put__leaves_no_temporary_file(self, store: LocalStaticStorage, tmp_path: Path) -> None:
        store.put("a/b.bin", b"x", "application/octet-stream")
        assert [p.name for p in _files(tmp_path / "static")] == ["b.bin"]

    def test_put_file__leaves_no_temporary_file(
        self, store: LocalStaticStorage, tmp_path: Path
    ) -> None:
        source = tmp_path / "src.bin"
        source.write_bytes(b"x")
        store.put_file("a/b.bin", source, "application/octet-stream")
        assert [p.name for p in _files(tmp_path / "static")] == ["b.bin"]


class TestPut__Durability:
    def test_put__stored_file_is_world_readable(
        self, store: LocalStaticStorage, tmp_path: Path
    ) -> None:
        store.put("a/b.bin", b"x", "application/octet-stream")
        assert (tmp_path / "static/a/b.bin").stat().st_mode & 0o777 == 0o644

    def test_put_file__stored_file_is_world_readable(
        self, store: LocalStaticStorage, tmp_path: Path
    ) -> None:
        source = tmp_path / "src.bin"
        source.write_bytes(b"x")
        store.put_file("a/b.bin", source, "application/octet-stream")
        assert (tmp_path / "static/a/b.bin").stat().st_mode & 0o777 == 0o644

    def test_put__syncs_temporary_file_before_rename(
        self, store: LocalStaticStorage, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[str] = []
        real_replace = os.replace

        def fake_fsync(fd: int) -> None:
            calls.append("fsync")

        def fake_replace(src: str, dst: Path) -> None:
            calls.append("replace")
            real_replace(src, dst)

        monkeypatch.setattr("novelhog.storage.os.fsync", fake_fsync)
        monkeypatch.setattr("novelhog.storage.os.replace", fake_replace)
        store.put("a/b.bin", b"x", "application/octet-stream")
        assert calls == ["fsync", "replace"]


class TestPut__OnWriteFailure:
    def test_put__leaves_no_temporary_and_no_target(
        self,
        store: LocalStaticStorage,
        tmp_path: Path,
        fail_replace: Callable[[], None],
    ) -> None:
        fail_replace()
        with pytest.raises(OSError):
            store.put("a/b.bin", b"x", "application/octet-stream")
        assert _files(tmp_path / "static") == []

    def test_put_file__missing_source_leaves_no_temporary_and_no_target(
        self, store: LocalStaticStorage, tmp_path: Path
    ) -> None:
        with pytest.raises(FileNotFoundError):
            store.put_file("a/b.bin", tmp_path / "missing", "application/octet-stream")
        assert _files(tmp_path / "static") == []

    def test_put__keeps_existing_target_intact(
        self, store: LocalStaticStorage, fail_replace: Callable[[], None]
    ) -> None:
        store.put("a/b.bin", b"old", "application/octet-stream")
        fail_replace()
        with pytest.raises(OSError):
            store.put("a/b.bin", b"new", "application/octet-stream")
        assert store.get("a/b.bin") == b"old"


class TestGet:
    def test_get__returns_stored_bytes(self, store: LocalStaticStorage) -> None:
        store.put("a/b.bin", b"x", "application/octet-stream")
        assert store.get("a/b.bin") == b"x"

    def test_get__returns_none_for_missing_key(self, store: LocalStaticStorage) -> None:
        assert store.get("a/missing.bin") is None


class TestDelete:
    def test_delete__ignores_key_below_a_file(self, store: LocalStaticStorage) -> None:
        store.put("a/b.bin", b"x", "application/octet-stream")
        store.delete("a/b.bin/c")

    def test_delete__ignores_key_naming_a_directory(self, store: LocalStaticStorage) -> None:
        store.put("a/b.bin", b"x", "application/octet-stream")
        store.delete("a")

    def test_delete__removes_file(self, store: LocalStaticStorage) -> None:
        store.put("a/b.bin", b"x", "application/octet-stream")
        store.delete("a/b.bin")
        assert store.get("a/b.bin") is None

    def test_delete__ignores_missing_key(self, store: LocalStaticStorage) -> None:
        store.delete("a/missing.bin")


class TestUrl:
    def test_url__equals_static_url(self, store: LocalStaticStorage) -> None:
        key = "exports/NV1/EX1/Re:Zero #2.epub"
        assert store.url(key) == static_url(BASE, key)


class TestInvalidKey:
    @pytest.mark.parametrize("key", BAD_KEYS)
    def test_put__raises(self, store: LocalStaticStorage, key: str) -> None:
        with pytest.raises(InvalidKey):
            store.put(key, b"x", "text/plain")

    @pytest.mark.parametrize("key", BAD_KEYS)
    def test_put_file__raises(self, store: LocalStaticStorage, tmp_path: Path, key: str) -> None:
        source = tmp_path / "s"
        source.write_bytes(b"x")
        with pytest.raises(InvalidKey):
            store.put_file(key, source, "text/plain")

    @pytest.mark.parametrize("key", BAD_KEYS)
    def test_get__raises(self, store: LocalStaticStorage, key: str) -> None:
        with pytest.raises(InvalidKey):
            store.get(key)

    @pytest.mark.parametrize("key", BAD_KEYS)
    def test_delete__raises(self, store: LocalStaticStorage, key: str) -> None:
        with pytest.raises(InvalidKey):
            store.delete(key)

    @pytest.mark.parametrize("key", BAD_KEYS)
    def test_url__raises(self, store: LocalStaticStorage, key: str) -> None:
        with pytest.raises(InvalidKey):
            store.url(key)

    def test_invalid_key__is_value_error(self) -> None:
        assert issubclass(InvalidKey, ValueError)


class TestCoverKey:
    def test_cover_key__same_bytes_same_key(self) -> None:
        assert cover_key("NV1", b"abc", "png") == cover_key("NV1", b"abc", "png")

    def test_cover_key__different_bytes_different_key(self) -> None:
        assert cover_key("NV1", b"abc", "png") != cover_key("NV1", b"abd", "png")

    def test_cover_key__has_documented_shape(self) -> None:
        key = cover_key("NV1", b"abc", "png")
        assert key == "covers/NV1/ba7816bf8f01cfea.png"
