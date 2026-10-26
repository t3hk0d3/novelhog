"""EPUB 3 exporter (spec sections 8.4 and 8.5)."""

import base64
import binascii
import io
import re
from html import escape
from pathlib import Path

from ebooklib import epub
from markdown_it import MarkdownIt
from markdown_it.token import Token
from PIL import Image

from novelhog.domain.exports import FORMATS, Format
from novelhog.domain.text import strip_xml_invalid
from novelhog.exporter.base import BookInput
from novelhog.images import convert_for, sniff

_DATA_URI = re.compile(r"^data:([^;,]+);base64,(.*)$", re.DOTALL)
_EXTENSIONS = {"image/jpeg": "jpg", "image/png": "png", "image/gif": "gif"}
_ACCEPTED = FORMATS[Format.EPUB].accepted_images


def _prepare_image(raw: bytes) -> tuple[bytes, str] | None:
    """Return the image in an EPUB-accepted type, judged by its bytes; None when undecodable."""
    mime = sniff(raw)
    if mime is None:
        return None
    try:
        with Image.open(io.BytesIO(raw)) as image:
            image.load()
        return convert_for(raw, mime, _ACCEPTED)
    except OSError, EOFError, ValueError, SyntaxError, Image.DecompressionBombError:
        return None


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
        front: list[str] = []
        if book.cover is not None:
            cover = _prepare_image(book.cover[0])
            if cover is not None:
                # Also writes the cover page, `cover.xhtml` with id "cover". Readers that open a
                # book at its first spine item show it only when it leads the spine and is linear
                # (ebooklib marks it non-linear).
                epub_book.set_cover(f"cover.{_EXTENSIONS[cover[1]]}", cover[0])
                epub_book.get_item_with_id("cover").is_linear = True
                epub_book.guide.append({"type": "cover", "href": "cover.xhtml", "title": "Cover"})
                front.append("cover")

        images = 0
        documents: list[epub.EpubHtml] = []
        for chapter in sorted(book.chapters, key=lambda c: c.position):
            chapter_title = (
                strip_xml_invalid(chapter.title).strip() or f"Chapter {chapter.position}"
            )
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
        epub_book.spine = [*front, "nav", *documents]
        epub.write_epub(str(out), epub_book)

    def _place_image(self, epub_book: epub.EpubBook, image: Token, count: int) -> int:
        """Replace a `data:` image source with an EPUB item, or turn the image into its alt text.

        Returns the updated image counter.
        """
        match = _DATA_URI.match(str(image.attrs.get("src", "")))
        prepared = None
        if match is not None:
            try:
                prepared = _prepare_image(base64.b64decode(match.group(2), validate=True))
            except binascii.Error:
                prepared = None
        if prepared is None:
            image.type = "text"
            image.children = None
            return count
        data, mime = prepared
        count += 1
        name = f"images/img_{count:05d}.{_EXTENSIONS[mime]}"
        epub_book.add_item(
            epub.EpubItem(uid=f"img_{count:05d}", file_name=name, media_type=mime, content=data)
        )
        image.attrs["src"] = name
        return count
