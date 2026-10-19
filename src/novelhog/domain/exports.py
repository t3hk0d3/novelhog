"""Export formats, file names and storage keys (spec sections 8.3 and 8.5)."""

import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum

from novelhog.domain.text import strip_xml_invalid


class Format(StrEnum):
    """An export format. v1 ships EPUB only."""

    EPUB = "epub"


class ExportState(StrEnum):
    """Lifecycle state of an export."""

    WAITING = "waiting"
    BUILDING = "building"
    READY = "ready"
    FAILED = "failed"


@dataclass(frozen=True)
class FormatInfo:
    """What the registry knows about a format."""

    mime: str
    extension: str
    accepted_images: frozenset[str]


FORMATS: dict[Format, FormatInfo] = {
    Format.EPUB: FormatInfo(
        mime="application/epub+zip",
        extension="epub",
        accepted_images=frozenset({"image/jpeg", "image/png", "image/gif"}),
    ),
}


def format_for_mime(mime: str) -> Format:
    """Return the format stored under `mime`; raise `ValueError` for an unknown one."""
    for fmt, info in FORMATS.items():
        if info.mime == mime:
            return fmt
    msg = f"unknown export mime type: {mime!r}"
    raise ValueError(msg)


_UNSAFE = re.compile(r'[/\\:*?"<>|]')
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
_WHITESPACE = re.compile(r"\s+")
_MAX_TITLE_BYTES = 150


def export_filename(title: str | None, start: int, end: int, ext: str) -> str:
    """Build `<title> Chapters <start>-<end>.<ext>` that is safe on common file systems."""
    cleaned = strip_xml_invalid(unicodedata.normalize("NFC", title or ""))
    cleaned = _CONTROL.sub("_", _UNSAFE.sub("_", cleaned))
    cleaned = _WHITESPACE.sub(" ", cleaned).strip(" ").lstrip(". ")
    cut = cleaned.encode("utf-8")[:_MAX_TITLE_BYTES].decode("utf-8", errors="ignore")
    part = cut.rstrip(" ") or "Untitled"
    return f"{part} Chapters {start}-{end}.{ext}"


def export_key(novel_id: str, export_id: str, filename: str) -> str:
    """Return the static storage key of an export file."""
    return f"exports/{novel_id}/{export_id}/{filename}"
