"""Export pipeline: the exporter registry and `build` dispatch (spec section 8.5)."""

from collections.abc import Mapping
from pathlib import Path

from novelhog.domain.exports import Format
from novelhog.exporter.base import BookChapter, BookInput, Exporter
from novelhog.exporter.epub import EpubExporter

REGISTRY: Mapping[Format, Exporter] = {Format.EPUB: EpubExporter()}


def build(fmt: Format, book: BookInput, out: Path) -> None:
    """Build `book` into `out` with the exporter registered for `fmt`."""
    exporter = REGISTRY.get(fmt)
    if exporter is None:
        msg = f"no exporter registered for format {fmt!r}"
        raise ValueError(msg)
    exporter.build(book, out)


__all__ = ["REGISTRY", "BookChapter", "BookInput", "Exporter", "build"]
