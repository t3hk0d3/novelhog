"""Tests for export formats, file names and keys."""

import json
import unicodedata
from pathlib import Path
from typing import Any

import pytest

from novelhog.domain.exports import (
    FORMATS,
    Format,
    export_filename,
    export_key,
    format_for_mime,
)

CASES: list[dict[str, Any]] = json.loads(
    (Path(__file__).parents[2] / "fixtures" / "filenames.json").read_text(encoding="utf-8")
)


class TestFormats:
    def test_formats__epub_entry(self) -> None:
        info = FORMATS[Format.EPUB]
        assert info.mime == "application/epub+zip"
        assert info.extension == "epub"
        assert info.accepted_images == {"image/jpeg", "image/png", "image/gif"}

    def test_format_for_mime__returns_epub(self) -> None:
        assert format_for_mime("application/epub+zip") is Format.EPUB

    def test_format_for_mime__unknown_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="application/pdf"):
            format_for_mime("application/pdf")


class TestExportFilename__FromFixture:
    @pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
    def test_export_filename__matches_expected(self, case: dict[str, Any]) -> None:
        assert (
            export_filename(case["title"], case["start"], case["end"], case["ext"])
            == case["expected"]
        )


class TestExportFilename__OnPlainTitle:
    def test_export_filename__uses_title_chapters_and_extension(self) -> None:
        assert export_filename("Book", 3, 7, "epub") == "Book Chapters 3-7.epub"


class TestExportFilename__OnLongEmojiTitle:
    def test_export_filename__limits_title_bytes_and_keeps_suffix(self) -> None:
        name = export_filename("\U0001f600" * 300, 1, 300, "epub")
        suffix = " Chapters 1-300.epub"
        assert name.endswith(suffix)
        title = name.removesuffix(suffix)
        assert len(title.encode()) <= 150
        assert title == "\U0001f600" * len(title)
        assert len(name.encode()) < 255


class TestExportFilename__OnDecomposedAccent:
    def test_export_filename__returns_nfc(self) -> None:
        name = export_filename("Café", 1, 2, "epub")
        assert unicodedata.is_normalized("NFC", name)


class TestExportKey:
    def test_export_key__joins_ids_and_filename(self) -> None:
        assert export_key("NVabc", "EXdef", "A B.epub") == "exports/NVabc/EXdef/A B.epub"
