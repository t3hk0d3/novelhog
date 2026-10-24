"""Tests for the exporter registry and dispatch."""

from pathlib import Path

import pytest

from novelhog import exporter
from novelhog.domain.exports import Format
from novelhog.exporter.base import BookInput


class _Fake:
    format = Format.EPUB

    def __init__(self) -> None:
        self.calls: list[tuple[BookInput, Path]] = []

    def build(self, book: BookInput, out: Path) -> None:
        self.calls.append((book, out))


_BOOK = BookInput("id", "T", "en", None, [])


class TestRegistry:
    def test_registry__has_an_exporter_for_every_format(self) -> None:
        assert set(exporter.REGISTRY) == set(Format)

    def test_registry__exporter_format_matches_key(self) -> None:
        assert all(e.format == fmt for fmt, e in exporter.REGISTRY.items())


class TestBuild:
    def test_build__dispatches_to_registered_exporter(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        fake = _Fake()
        monkeypatch.setattr(exporter, "REGISTRY", {Format.EPUB: fake})
        exporter.build(Format.EPUB, _BOOK, tmp_path / "x")
        assert fake.calls == [(_BOOK, tmp_path / "x")]


class TestBuild__WithUnregisteredFormat:
    def test_build__raises_value_error(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(exporter, "REGISTRY", {})
        with pytest.raises(ValueError, match="no exporter"):
            exporter.build(Format.EPUB, _BOOK, tmp_path / "x")
