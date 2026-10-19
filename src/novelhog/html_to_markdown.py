"""HTML to Markdown converter for chapter bodies (spec section 8.1).

The converter is built on the standard library ``html.parser``: the input is parsed into a small
tree, then rendered. Only ``p``, ``br``, ``em``/``i``, ``strong``/``b``, ``img`` and the list tags
are meaningful; every other tag is dropped and its text kept.
"""

import base64
import re
import string
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from html.parser import HTMLParser
from typing import NamedTuple

from novelhog.domain.text import strip_xml_invalid
from novelhog.images import FetchedImage

_DROPPED = frozenset({"script", "style", "noscript", "iframe"})
_VOID = frozenset(
    {"br", "img", "hr", "wbr", "input", "meta", "link", "source", "col", "area", "embed", "base"}
)
# Tags that end the current paragraph: the allowed block tags plus dropped block-level HTML.
_BLOCK_TAGS = frozenset(
    {
        "p",
        "dd",
        "dl",
        "li",
        "div",
        "blockquote",
        "section",
        "article",
        "aside",
        "header",
        "footer",
        "nav",
        "main",
        "pre",
        "center",
        "figure",
        "figcaption",
        "address",
        "form",
        "fieldset",
        "details",
        "summary",
        "body",
        "html",
        "hr",
        "table",
        "thead",
        "tbody",
        "tfoot",
        "tr",
        "td",
        "th",
        "caption",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
    }
)
_MAX_DEPTH = 200
_BREAK = "\0"  # in-flight hard line break; NUL never survives strip_xml_invalid
_IMAGE_UNAVAILABLE = "[image unavailable]"
_MAX_WARNING_URL = 200
_MAX_START = 999_999_999

_HTML_SPACE = re.compile(r"[ \t\r\n\f]+")
_METACHARS = re.compile(r"[\\*_#>\[\]`|~<&]")
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


@dataclass(frozen=True)
class _Seg:
    """A run of inline content: raw text (``_BREAK`` is a hard break) or rendered Markdown."""

    text: str
    styles: frozenset[str] = frozenset()
    rendered: bool = False


def _escape(text: str) -> str:
    return _METACHARS.sub(r"\\\g<0>", text)


def _escape_line_start(line: str) -> str:
    def repl(match: re.Match[str]) -> str:
        if match.group(1):
            return "\\" + match.group(1)
        return f"{match.group(2)}\\{match.group(3)}"

    return _LINE_START.sub(repl, line)


def _is_blank_char(char: str) -> bool:
    return char.isspace() or char == _BREAK or unicodedata.category(char) == "Cf"


def _is_outside_char(char: str) -> bool:
    """Characters that must not sit just inside an emphasis marker."""
    return char == _BREAK or unicodedata.category(char)[0] in "PZC" or char.isspace()


def _is_symbol(char: str) -> bool:
    return unicodedata.category(char)[0] == "S"


def _is_flank_neutral(char: str | None) -> bool:
    """True at the paragraph edge, for whitespace and for punctuation (ASCII punctuation too)."""
    return (
        char is None
        or char == _BREAK
        or char.isspace()
        or char in string.punctuation
        or unicodedata.category(char)[0] == "P"
    )


def _char_beside(segs: list[_Seg], *, last: bool) -> str | None:
    """The last (or first) character of the nearest non-empty segment."""
    for seg in reversed(segs) if last else segs:
        if seg.text:
            return seg.text[-1] if last else seg.text[0]
    return None


def _symbol_edges_flank(before: str | None, core: list[_Seg], after: str | None) -> bool:
    """A symbol just inside a marker counts as punctuation, so the outside must be neutral too."""
    opening, closing = core[0].text[0], core[-1].text[-1]
    return (not _is_symbol(opening) or _is_flank_neutral(before)) and (
        not _is_symbol(closing) or _is_flank_neutral(after)
    )


def _peel(run: list[_Seg]) -> tuple[list[_Seg], list[_Seg], list[_Seg]]:
    """Split a run into (leading, core, trailing), the outer parts being whitespace/punctuation."""
    core = list(run)
    lead: list[_Seg] = []
    while core and not core[0].rendered:
        text = core[0].text
        size = 0
        while size < len(text) and _is_outside_char(text[size]):
            size += 1
        if size == 0:
            break
        lead.append(replace(core[0], text=text[:size]))
        if size == len(text):
            core.pop(0)
            continue
        core[0] = replace(core[0], text=text[size:])
        break
    trail: list[_Seg] = []
    while core and not core[-1].rendered:
        text = core[-1].text
        size = len(text)
        while size > 0 and _is_outside_char(text[size - 1]):
            size -= 1
        if size == len(text):
            break
        trail.insert(0, replace(core[-1], text=text[size:]))
        if size == 0:
            core.pop()
            continue
        core[-1] = replace(core[-1], text=text[:size])
        break
    return lead, core, trail


def _merge(segs: list[_Seg]) -> list[_Seg]:
    merged: list[_Seg] = []
    for seg in segs:
        if (
            merged
            and not seg.rendered
            and not merged[-1].rendered
            and merged[-1].styles == seg.styles
        ):
            merged[-1] = replace(seg, text=merged[-1].text + seg.text)
        else:
            merged.append(seg)
    return merged


_STYLE_MARKERS = (("strong", "**"), ("em", "*"))


def _render_inline(segs: list[_Seg], level: int = 0) -> str:
    """Render segments to Markdown, wrapping each run of an emphasis style once."""
    if level == len(_STYLE_MARKERS):
        return "".join(seg.text if seg.rendered else _escape(seg.text) for seg in _merge(segs))
    style, marker = _STYLE_MARKERS[level]
    out: list[str] = []
    index = 0
    while index < len(segs):
        first = end = index
        has_style = style in segs[index].styles
        while end < len(segs) and (style in segs[end].styles) == has_style:
            end += 1
        run = segs[index:end]
        index = end
        if not has_style:
            out.append(_render_inline(run, level + 1))
            continue
        lead, core, trail = _peel(run)
        before = _char_beside(lead, last=True) or _char_beside(segs[:first], last=True)
        after = _char_beside(trail, last=False) or _char_beside(segs[end:], last=False)
        if (
            not core
            or core[0].rendered
            or core[-1].rendered
            or not _symbol_edges_flank(before, core, after)
        ):
            out.append(_render_inline(run, level + 1))
            continue
        out.append(_render_inline(lead, level + 1))
        out.append(f"{marker}{_render_inline(core, level + 1)}{marker}")
        out.append(_render_inline(trail, level + 1))
    return "".join(out)


def _split_paragraphs(segs: list[_Seg]) -> list[list[_Seg]]:
    """Split at runs of two or more hard breaks (whitespace between them is ignored)."""
    groups: list[list[_Seg]] = []
    current: list[_Seg] = []
    gap: list[_Seg] = []
    breaks = 0
    for seg in segs:
        if not seg.rendered and seg.text == _BREAK:
            gap.append(seg)
            breaks += 1
        elif not seg.rendered and all(_is_blank_char(char) for char in seg.text):
            gap.append(seg)
        else:
            if breaks >= 2:
                groups.append(current)
                current = []
            else:
                current.extend(gap)
            gap, breaks = [], 0
            current.append(seg)
    groups.append(current)
    return groups


def _paragraph(segs: list[_Seg]) -> str | None:
    if all(not seg.rendered and all(_is_blank_char(char) for char in seg.text) for seg in segs):
        return None
    text = re.sub(r" {2,}", " ", _render_inline(segs))
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

    def blocks(
        self, nodes: list[_Element | str], styles: frozenset[str] = frozenset()
    ) -> list[_Block]:
        out: list[_Block] = []
        buffer: list[_Seg] = []
        self._collect(nodes, out, buffer, styles)
        self._flush(out, buffer)
        return out

    def _flush(self, out: list[_Block], buffer: list[_Seg]) -> None:
        for group in _split_paragraphs(buffer):
            paragraph = _paragraph(group)
            if paragraph is not None:
                out.append(_Block(paragraph, is_list=False))
        buffer.clear()

    def _collect(
        self,
        nodes: list[_Element | str],
        out: list[_Block],
        buffer: list[_Seg],
        styles: frozenset[str],
    ) -> None:
        for node in nodes:
            if isinstance(node, str):
                buffer.append(_Seg(_HTML_SPACE.sub(" ", node), styles))
                continue
            tag = node.tag
            if tag in {"em", "i"}:
                self._collect(node.children, out, buffer, styles | {"em"})
            elif tag in {"strong", "b"}:
                self._collect(node.children, out, buffer, styles | {"strong"})
            elif tag == "br":
                buffer.append(_Seg(_BREAK, styles))
            elif tag == "img":
                buffer.append(_Seg(self._image(node), styles, rendered=True))
            elif tag in {"ul", "ol"}:
                self._flush(out, buffer)
                block = self._list(node, styles)
                if block is not None:
                    out.append(block)
            elif tag == "dt":
                self._flush(out, buffer)
                self._collect(node.children, out, buffer, styles | {"strong"})
                self._flush(out, buffer)
            elif tag in _BLOCK_TAGS:
                self._flush(out, buffer)
                self._collect(node.children, out, buffer, styles)
                self._flush(out, buffer)
            else:
                self._collect(node.children, out, buffer, styles)

    def _fetch(self, source: str) -> FetchedImage | None:
        try:
            return self._fetch_image(source)
        except Exception:  # noqa: BLE001 - a failing fetcher must not fail the chapter
            return None

    def _image(self, node: _Element) -> str:
        source = node.attrs.get("src", "").strip()
        fetched = self._fetch(source) if source else None
        if fetched is None:
            shown = source[:_MAX_WARNING_URL] or "(no src)"
            self.warnings.append(f"image unavailable: {shown}")
            return _IMAGE_UNAVAILABLE
        self.has_images = True
        alt = _escape(_HTML_SPACE.sub(" ", node.attrs.get("alt", "")).strip())
        encoded = base64.b64encode(fetched.data).decode("ascii")
        return f"![{alt}](data:{fetched.mime};base64,{encoded})"

    def _list(self, node: _Element, styles: frozenset[str]) -> _Block | None:
        ordered = node.tag == "ol"
        start = _parse_start(node.attrs.get("start")) if ordered else 1
        lines: list[str] = []
        for item in self._items(node.children):
            blocks = self.blocks(item, styles)
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
