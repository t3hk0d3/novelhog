"""Format-neutral export types (spec section 8.5)."""

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from novelhog.domain.exports import Format


@dataclass(frozen=True)
class BookChapter:
    """One chapter of a book: its position, title and Markdown body."""

    position: int
    title: str
    markdown: str


@dataclass(frozen=True)
class BookInput:
    """Everything an exporter needs; `cover` is (bytes, mime) when present."""

    identifier: str
    title: str
    language: str
    cover: tuple[bytes, str] | None
    chapters: list[BookChapter]


class Exporter(Protocol):
    """Builds one export format."""

    format: Format

    def build(self, book: BookInput, out: Path) -> None:
        """Write the book to `out`."""
        ...
