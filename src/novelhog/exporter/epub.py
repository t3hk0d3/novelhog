"""EPUB 3 exporter (spec sections 8.4 and 8.5)."""

import base64
import binascii
import re
from html import escape
from pathlib import Path

from ebooklib import epub
from markdown_it import MarkdownIt
from markdown_it.token import Token

from novelhog.domain.exports import FORMATS, Format
from novelhog.domain.text import strip_xml_invalid
from novelhog.exporter.base import BookInput
from novelhog.images import convert_for

_DATA_URI = re.compile(r"^data:([^;,]+);base64,(.*)$", re.DOTALL)
_EXTENSIONS = {"image/jpeg": "jpg", "image/png": "png", "image/gif": "gif"}
_ACCEPTED = FORMATS[Format.EPUB].accepted_images


class EpubExporter:
    """Turns a `BookInput` into an EPUB 3 file."""

    format = Format.EPUB

    def __init__(self) -> None:
        # Raw HTML disabled: tags in the Markdown stay literal, escaped text.
        self._markdown = MarkdownIt("commonmark", {"html": False, "xhtmlOut": True})

    def build(self, book: BookInput, out: Path) -> None:
        """Write the EPUB to `out`; raise `ValueError` when the book has no chapters."""
        if not book.chapters:
            msg = "cannot build an EPUB without chapters"
            raise ValueError(msg)
        title = strip_xml_invalid(book.title)
        epub_book = epub.EpubBook()
        epub_book.set_identifier(strip_xml_invalid(book.identifier))
        epub_book.set_title(title)
        epub_book.set_language(strip_xml_invalid(book.language))
        if book.cover is not None:
            data, mime = convert_for(book.cover[0], book.cover[1], _ACCEPTED)
            epub_book.set_cover(f"cover.{_EXTENSIONS[mime]}", data)

        images = 0
        documents: list[epub.EpubHtml] = []
        for chapter in sorted(book.chapters, key=lambda c: c.position):
            chapter_title = strip_xml_invalid(chapter.title)
            tokens = self._markdown.parse(strip_xml_invalid(chapter.markdown))
            for token in tokens:
                for child in token.children or []:
                    if child.type == "image":
                        images = self._place_image(epub_book, child, images)
            body = self._markdown.renderer.render(tokens, self._markdown.options, {})
            document = epub.EpubHtml(
                title=chapter_title,
                file_name=f"chapter_{chapter.position:05d}.xhtml",
                lang=strip_xml_invalid(book.language),
            )
            document.content = f"<h1>{escape(chapter_title)}</h1>\n{body}"
            epub_book.add_item(document)
            documents.append(document)

        epub_book.toc = tuple(documents)
        epub_book.add_item(epub.EpubNcx())
        epub_book.add_item(epub.EpubNav())
        epub_book.spine = ["nav", *documents]
        epub.write_epub(str(out), epub_book)

    def _place_image(self, epub_book: epub.EpubBook, image: Token, count: int) -> int:
        """Replace a `data:` image source with an EPUB item, or turn the image into its alt text.

        Returns the updated image counter.
        """
        match = _DATA_URI.match(str(image.attrs.get("src", "")))
        if match is not None:
            try:
                raw = base64.b64decode(match.group(2), validate=True)
                data, mime = convert_for(raw, match.group(1).lower(), _ACCEPTED)
            except binascii.Error, OSError, ValueError:
                match = None
        if match is None:
            image.type = "text"
            image.content = image.content or ""
            image.children = None
            return count
        count += 1
        name = f"images/img_{count:05d}.{_EXTENSIONS[mime]}"
        epub_book.add_item(
            epub.EpubItem(uid=f"img_{count:05d}", file_name=name, media_type=mime, content=data)
        )
        image.attrs["src"] = name
        return count
