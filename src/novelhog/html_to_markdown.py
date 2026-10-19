"""HTML to Markdown converter for chapter bodies (spec section 8.1).

The converter is built on the standard library ``html.parser``: the input is parsed into a small
tree, then rendered. Only ``p``, ``br``, ``em``/``i``, ``strong``/``b``, ``img`` and the list tags
are meaningful; every other tag is dropped and its text kept.
"""

import base64
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import NamedTuple

from novelhog.domain.text import strip_xml_invalid
from novelhog.images import FetchedImage

_DROPPED = frozenset({"script", "style", "noscript", "iframe"})
_VOID = frozenset(
    {"br", "img", "hr", "wbr", "input", "meta", "link", "source", "col", "area", "embed", "base"}
)
_BLOCKISH = frozenset({"p", "ul", "ol", "dl", "li", "dt", "dd"})
_MAX_DEPTH = 200
_BREAK = "\0"  # in-flight hard line break; NUL never survives strip_xml_invalid
_IMAGE_UNAVAILABLE = "[image unavailable]"
_MAX_WARNING_URL = 200
_MAX_START = 999_999_999

_HTML_SPACE = re.compile(r"[ \t\r\n\f]+")
_METACHARS = re.compile(r"[\\*_#>\[\]`|~]")
_TAG_START = re.compile(r"<(?=[A-Za-z/!?])")
_ENTITY_START = re.compile(r"&(?=#?\w+;)")
_BREAK_SPACES = re.compile(f" ?{_BREAK}[ {_BREAK}]*")
_LINE_START = re.compile(r"^(?:([-+=])|(\d{1,9})([.)]))")


@dataclass(frozen=True)
class Conversion:
    """Result of converting one chapter body."""

    markdown: str
    has_images: bool
    warnings: list[str] = field(default_factory=list)


@dataclass
class _Element:
    tag: str
    attrs: dict[str, str]
    children: list[_Element | str] = field(default_factory=list)


class _TreeBuilder(HTMLParser):
    """Builds an element tree, closing ``p``, ``li``, ``dt`` and ``dd`` implicitly."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Element("#root", {})
        self._stack = [self.root]
        self._skip_tag: str | None = None
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._skip_tag is not None:
            if tag == self._skip_tag:
                self._skip_depth += 1
            return
        if tag in _DROPPED:
            self._skip_tag, self._skip_depth = tag, 1
            return
        self._close_implied(tag)
        element = _Element(tag, {name: value or "" for name, value in attrs})
        self._stack[-1].children.append(element)
        if tag not in _VOID and len(self._stack) < _MAX_DEPTH:
            self._stack.append(element)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        if self._skip_tag is not None:
            if tag == self._skip_tag:
                self._skip_depth -= 1
                if self._skip_depth == 0:
                    self._skip_tag = None
            return
        for index in range(len(self._stack) - 1, 0, -1):
            if self._stack[index].tag == tag:
                del self._stack[index:]
                return

    def handle_data(self, data: str) -> None:
        if self._skip_tag is None:
            self._stack[-1].children.append(strip_xml_invalid(data))

    def _close_implied(self, tag: str) -> None:
        if tag in {"p", "ul", "ol", "dl"}:
            self._close_open({"p"}, stop={"ul", "ol", "dl", "li", "dt", "dd"})
        elif tag == "li":
            self._close_open({"li"}, stop={"ul", "ol"})
        elif tag in {"dt", "dd"}:
            self._close_open({"dt", "dd"}, stop={"dl"})

    def _close_open(self, targets: set[str], stop: set[str]) -> None:
        for index in range(len(self._stack) - 1, 0, -1):
            current = self._stack[index].tag
            if current in targets:
                del self._stack[index:]
                return
            if current in stop:
                return


class _Block(NamedTuple):
    text: str
    is_list: bool
    can_interrupt_paragraph: bool = False


def _escape(text: str) -> str:
    text = _METACHARS.sub(r"\\\g<0>", text)
    text = _TAG_START.sub(r"\\<", text)
    return _ENTITY_START.sub(r"\\&", text)


def _escape_line_start(line: str) -> str:
    def repl(match: re.Match[str]) -> str:
        if match.group(1):
            return "\\" + match.group(1)
        return f"{match.group(2)}\\{match.group(3)}"

    return _LINE_START.sub(repl, line)


def _text(node: str) -> str:
    return _escape(_HTML_SPACE.sub(" ", node))


def _paragraph(raw: str) -> str | None:
    text = re.sub(r" {2,}", " ", raw)
    text = _BREAK_SPACES.sub(_BREAK, text).strip(f" {_BREAK}")
    if not text:
        return None
    return "\\\n".join(_escape_line_start(line) for line in text.split(_BREAK))


def _parse_start(value: str | None) -> int:
    if value is not None and value.strip().isascii() and value.strip().isdecimal():
        start = int(value)
        if start <= _MAX_START:
            return start
    return 1


class _Renderer:
    def __init__(self, fetch_image: Callable[[str], FetchedImage | None]) -> None:
        self._fetch_image = fetch_image
        self.has_images = False
        self.warnings: list[str] = []

    def blocks(self, nodes: list[_Element | str]) -> list[_Block]:
        out: list[_Block] = []
        buffer: list[str] = []
        self._collect(nodes, out, buffer)
        self._flush(out, buffer)
        return out

    def _flush(self, out: list[_Block], buffer: list[str]) -> None:
        paragraph = _paragraph("".join(buffer))
        buffer.clear()
        if paragraph is not None:
            out.append(_Block(paragraph, is_list=False))

    def _collect(self, nodes: list[_Element | str], out: list[_Block], buffer: list[str]) -> None:
        for node in nodes:
            if isinstance(node, str):
                buffer.append(_text(node))
            elif node.tag in {"em", "i", "strong", "b", "br", "img"}:
                buffer.append(self._inline_node(node, frozenset()))
            elif node.tag in {"ul", "ol"}:
                self._flush(out, buffer)
                block = self._list(node)
                if block is not None:
                    out.append(block)
            elif node.tag == "dt":
                self._flush(out, buffer)
                term = _paragraph(self._inline(node.children, frozenset({"strong"})))
                if term is not None:
                    out.append(_Block(f"**{term}**", is_list=False))
            elif node.tag in {"p", "dd"}:
                self._flush(out, buffer)
                self._collect(node.children, out, buffer)
                self._flush(out, buffer)
            else:
                self._collect(node.children, out, buffer)

    def _inline(self, nodes: list[_Element | str], active: frozenset[str]) -> str:
        return "".join(
            _text(node) if isinstance(node, str) else self._inline_node(node, active)
            for node in nodes
        )

    def _inline_node(self, node: _Element, active: frozenset[str]) -> str:
        tag = node.tag
        if tag == "br":
            return _BREAK
        if tag == "img":
            return self._image(node)
        if tag in {"em", "i", "strong", "b"}:
            style, marker = ("em", "*") if tag in {"em", "i"} else ("strong", "**")
            if style in active:
                return self._inline(node.children, active)
            inner = self._inline(node.children, active | {style})
            core = inner.strip(f" {_BREAK}")
            if not core:
                return inner
            start = inner.index(core)
            return f"{inner[:start]}{marker}{core}{marker}{inner[start + len(core) :]}"
        inner = self._inline(node.children, active)
        return f" {inner} " if tag in _BLOCKISH else inner

    def _image(self, node: _Element) -> str:
        source = node.attrs.get("src", "").strip()
        fetched = self._fetch_image(source) if source else None
        if fetched is None:
            shown = source[:_MAX_WARNING_URL] or "(no src)"
            self.warnings.append(f"image unavailable: {shown}")
            return _IMAGE_UNAVAILABLE
        self.has_images = True
        alt = _escape(_HTML_SPACE.sub(" ", node.attrs.get("alt", "")).strip())
        encoded = base64.b64encode(fetched.data).decode("ascii")
        return f"![{alt}](data:{fetched.mime};base64,{encoded})"

    def _list(self, node: _Element) -> _Block | None:
        ordered = node.tag == "ol"
        start = _parse_start(node.attrs.get("start")) if ordered else 1
        lines: list[str] = []
        for item in self._items(node.children):
            blocks = self.blocks(item)
            if not blocks:
                continue
            marker = (f"{start}." if not lines else "1.") if ordered else "-"
            lines.append(self._item(marker, blocks))
        if not lines:
            return None
        return _Block(
            "\n".join(lines), is_list=True, can_interrupt_paragraph=not ordered or start == 1
        )

    @staticmethod
    def _items(children: list[_Element | str]) -> list[list[_Element | str]]:
        items: list[list[_Element | str]] = []
        loose: list[_Element | str] = []
        for child in children:
            if isinstance(child, _Element) and child.tag == "li":
                if loose:
                    items.append(loose)
                    loose = []
                items.append(child.children)
            else:
                loose.append(child)
        if loose:
            items.append(loose)
        return items

    @staticmethod
    def _item(marker: str, blocks: list[_Block]) -> str:
        indent_width = max(4, len(marker) + 1)
        parts = [blocks[0].text]
        for previous, current in zip(blocks, blocks[1:], strict=False):
            tight = not previous.is_list and current.is_list and current.can_interrupt_paragraph
            parts.append(("\n" if tight else "\n\n") + current.text)
        lines = "".join(parts).split("\n")
        lead = marker.ljust(indent_width) if blocks[0].is_list else f"{marker} "
        indent = " " * indent_width
        rest = [f"{indent}{line}" if line else "" for line in lines[1:]]
        return "\n".join([lead + lines[0], *rest])


def convert(body_html: str, fetch_image: Callable[[str], FetchedImage | None]) -> Conversion:
    """Convert a chapter body to Markdown, embedding images as data URIs.

    ``fetch_image`` is called with each ``img`` source; returning ``None`` makes the image the
    literal text ``[image unavailable]`` plus a warning.
    """
    builder = _TreeBuilder()
    builder.feed(strip_xml_invalid(body_html))
    builder.close()
    renderer = _Renderer(fetch_image)
    blocks = renderer.blocks(builder.root.children)
    markdown = strip_xml_invalid("\n\n".join(block.text for block in blocks))
    return Conversion(markdown=markdown, has_images=renderer.has_images, warnings=renderer.warnings)
