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
_WHITESPACE_CONTROLS = "\t\n\r"
_MAX_TITLE_BYTES = 150


def _classify(char: str) -> str:
    """Map whitespace (tab, LF, CR, Zs) to a space and other Cc/Cf controls to `_`."""
    category = unicodedata.category(char)
    if char in _WHITESPACE_CONTROLS or category == "Zs":
        return " "
    if category in {"Cc", "Cf"}:
        return "_"
    return char


def export_filename(title: str | None, start: int, end: int, ext: str) -> str:
    """Build `<title> Chapters <start>-<end>.<ext>` that is safe on common file systems."""
    cleaned = strip_xml_invalid(unicodedata.normalize("NFC", title or ""))
    cleaned = "".join(_classify(c) for c in _UNSAFE.sub("_", cleaned))
    cleaned = re.sub(" +", " ", cleaned).strip(" ").lstrip(". ")
    cut = cleaned.encode("utf-8")[:_MAX_TITLE_BYTES].decode("utf-8", errors="ignore")
    part = cut.rstrip(" ") or "Untitled"
    return f"{part} Chapters {start}-{end}.{ext}"


def export_key(novel_id: str, export_id: str, filename: str) -> str:
    """Return the static storage key of an export file."""
    return f"exports/{novel_id}/{export_id}/{filename}"
