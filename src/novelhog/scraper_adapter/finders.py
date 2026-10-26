"""Title, chapter title and cover finders for table-of-contents pages (spec section 6.7)."""

import html
import json
import re
from collections.abc import Sequence
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from novelhog import log
from novelhog.domain.errors import ValidationFailed
from novelhog.domain.text import strip_xml_invalid
from novelhog.domain.urls import normalize_url

_VOID_TAGS = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "wbr"}
)
_SITE_SUFFIX = re.compile(r"\s+[-|]\s+[^-|]+$")
# Container selectors, in priority order (`figure` matches the tag, the rest match a class).
_CONTAINERS = ("cover", "book", "novel-cover", "figure")


class _Page(HTMLParser):
    """Collects everything the finders look at in one pass."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, str] = {}
        self.title = ""
        self.h1: str | None = None
        self.itemprop_image: str | None = None
        self.json_ld: list[str] = []
        self.container_img: dict[str, str] = {}
        self.images: list[tuple[str, str]] = []  # (src, alt)
        self._stack: list[tuple[str, frozenset[str]]] = []
        self._text_target: str | None = None
        self._buffer = ""
        self._script_is_ld = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {k: (v or "") for k, v in attrs}
        if tag == "meta":
            key = (
                values.get("property") or values.get("name") or values.get("itemprop", "")
            ).lower()
            content = values.get("content", "").strip()
            if key and content:
                self.meta.setdefault(key, content)
            if key == "image" and content and self.itemprop_image is None:
                self.itemprop_image = content
        elif tag == "img":
            self._handle_img(values)
        elif tag == "link" and values.get("itemprop") == "image" and values.get("href"):
            self.itemprop_image = self.itemprop_image or values["href"].strip()
        if tag not in _VOID_TAGS:
            self._stack.append((tag, frozenset(values.get("class", "").split())))
            self._start_text(tag, values)

    def _handle_img(self, values: dict[str, str]) -> None:
        src = values.get("src", "").strip()
        if not src or src.startswith("data:"):
            src = values.get("data-src", "").strip()
        if not src:
            return
        self.images.append((src, values.get("alt", "")))
        if values.get("itemprop") == "image":
            self.itemprop_image = self.itemprop_image or src
        open_tags = {tag for tag, _ in self._stack}
        open_classes = set().union(*(classes for _, classes in self._stack))
        for container in _CONTAINERS:
            inside = container in (open_tags if container == "figure" else open_classes)
            if inside:
                self.container_img.setdefault(container, src)

    def _start_text(self, tag: str, values: dict[str, str]) -> None:
        if tag == "title" and not self.title:
            self._text_target, self._buffer = "title", ""
        elif tag == "h1" and self.h1 is None:
            self._text_target, self._buffer = "h1", ""
        elif tag == "script" and values.get("type", "").lower() == "application/ld+json":
            self._text_target, self._buffer = "ld", ""

    def handle_data(self, data: str) -> None:
        if self._text_target:
            self._buffer += data

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self._stack) - 1, -1, -1):
            if self._stack[index][0] == tag:
                del self._stack[index:]
                break
        if self._text_target and tag in {"title", "h1", "script"}:
            text = " ".join(self._buffer.split())
            if self._text_target == "title":
                self.title = text
            elif self._text_target == "h1":
                self.h1 = text or None
            else:
                self.json_ld.append(self._buffer)
            self._text_target = None


class _ChapterLinks(HTMLParser):
    """Collects each link's href and title: the text of an element inside it whose class
    mentions "title" (`<strong class="chapter-title">`), else the text of a link holding text only.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str | None, bool]] = []  # (href, title, from an element)
        self._href: str | None = None
        self._inside: list[tuple[str, bool]] = []  # open tags in the link: (tag, is a title)
        self._nested = False
        self._text = ""
        self._title = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {k: (v or "") for k, v in attrs}
        if tag == "a":
            self._href = values.get("href", "").strip() or None
            self._inside, self._nested, self._text, self._title = [], False, "", ""
        elif self._href is not None and tag not in _VOID_TAGS:
            self._nested = True
            self._inside.append((tag, "title" in values.get("class", "").lower()))

    def handle_data(self, data: str) -> None:
        if self._href is None:
            return
        self._text += data
        if any(is_title for _, is_title in self._inside):
            self._title += data

    def handle_endtag(self, tag: str) -> None:
        if self._href is None:
            return
        if tag == "a":
            if self._title.strip():
                self.links.append((self._href, self._title, True))
            else:
                self.links.append((self._href, None if self._nested else self._text, False))
            self._href = None
            return
        for index in range(len(self._inside) - 1, -1, -1):
            if self._inside[index][0] == tag:
                del self._inside[index:]
                break


def _parse(html: str) -> _Page:
    page = _Page()
    page.feed(html)
    page.close()
    return page


def clean_chapter_title(value: str | None) -> str | None:
    """A chapter title as plain text: entities a site escaped twice (`&amp;#039;`) decoded,
    whitespace collapsed, XML-invalid characters removed; None when nothing is left.
    """
    return _clean(" ".join(html.unescape(value or "").split()))


def _clean(value: str | None) -> str | None:
    cleaned = strip_xml_invalid(value or "").strip()
    return cleaned or None


def _without_trailing_words(title: str | None, h1: str | None) -> str | None:
    """The `<h1>` when `title` is the `<h1>` followed by more words ("X Novel Read Online")."""
    if title is None or h1 is None or not title.startswith(h1) or title == h1:
        return title
    return h1 if title[len(h1)].isspace() else title


def _find_title(toc_html: str) -> str | None:
    """Detect the novel title: og:title, then `<title>` without its site suffix, then `<h1>`.

    A `<title>` that is the `<h1>` followed by more words gives the `<h1>`.
    """
    page = _parse(toc_html)
    h1 = _clean(page.h1)
    title = _clean(_SITE_SUFFIX.sub("", strip_xml_invalid(page.title).strip()))
    return (
        _clean(" ".join((page.meta.get("og:title") or "").split()))
        or _without_trailing_words(title, h1)
        or h1
    )


_MAX_JSON_LD_DEPTH = 4  # document, list or @graph item, and one more level of nesting


def _json_ld_image(blocks: list[str]) -> str | None:
    for block in blocks:
        try:
            data = json.loads(block)
        except ValueError, RecursionError:
            continue
        found = _image_in(data, _MAX_JSON_LD_DEPTH)
        if found:
            return found
    return None


def _image_in(data: object, depth: int) -> str | None:
    """The first image of a JSON-LD node, looking through lists and `@graph` to a bounded depth."""
    if depth <= 0:
        return None
    if isinstance(data, list):
        return next((found for item in data if (found := _image_in(item, depth - 1))), None)
    if not isinstance(data, dict):
        return None
    return _image_value(data.get("image")) or _image_in(data.get("@graph"), depth - 1)


def _image_value(image: object) -> str | None:
    """A schema.org `image`: a URL string, an ImageObject with `url`, or a list of those."""
    candidates = image if isinstance(image, list) else [image]
    for candidate in candidates:
        if isinstance(candidate, dict):
            candidate = candidate.get("url")  # noqa: PLW2901
        if isinstance(candidate, str) and candidate.strip() and not candidate.startswith("data:"):
            return candidate.strip()
    return None


def _find_cover_url(toc_html: str, toc_url: str, title: str | None) -> str | None:
    """Detect the cover image URL, resolved against `toc_url`; None when no heuristic matches."""
    page = _parse(toc_html)
    candidates = [
        page.meta.get("og:image"),
        page.meta.get("twitter:image"),
        page.itemprop_image,
        _json_ld_image(page.json_ld),
        *(page.container_img.get(container) for container in _CONTAINERS),
        next((src for src, alt in page.images if title and alt.strip() == title), None),
    ]
    for candidate in candidates:
        resolved = _resolve(candidate, toc_url)
        if resolved:
            return resolved
    return None


def _resolve(candidate: str | None, toc_url: str) -> str | None:
    if not candidate or not candidate.strip():
        return None
    url = urljoin(toc_url, candidate.strip())
    return url if urlsplit(url).scheme in {"http", "https"} else None


def find_title(toc_html: str) -> str | None:
    """Detect the title; a failing heuristic is logged and gives None, never an exception."""
    try:
        return _find_title(toc_html)
    except Exception as exc:  # noqa: BLE001 - metadata is best effort
        log.get().warning("find_title_failed", error=f"{type(exc).__name__}: {exc}")
        return None


def _find_chapter_titles(toc_pages: Sequence[str], toc_url: str) -> dict[str, str]:
    titles: dict[str, tuple[str, bool]] = {}
    for page in toc_pages:
        parser = _ChapterLinks()
        parser.feed(page)
        parser.close()
        for href, raw, from_element in parser.links:
            title = clean_chapter_title(raw)
            if title is None:
                continue
            try:
                url = normalize_url(urljoin(toc_url, href))
            except ValidationFailed:
                continue
            if url not in titles or (from_element and not titles[url][1]):
                titles[url] = (title, from_element)
    return {url: title for url, (title, _) in titles.items()}


def find_chapter_titles(toc_pages: Sequence[str], toc_url: str) -> dict[str, str]:
    """Each linked chapter's title by normalised URL.

    A title element beats a link's plain text (fanmtl's "Latest Release: Chapter 136" links come
    before its chapter list); otherwise the first link to a URL wins. Every link is collected;
    the caller keeps the ones that are chapters. A failing heuristic is logged and gives no
    titles, never an exception.
    """
    try:
        return _find_chapter_titles(toc_pages, toc_url)
    except Exception as exc:  # noqa: BLE001 - metadata is best effort
        log.get().warning("find_chapter_titles_failed", error=f"{type(exc).__name__}: {exc}")
        return {}


def find_cover_url(toc_html: str, toc_url: str, title: str | None) -> str | None:
    """Detect the cover URL; a failing heuristic is logged and gives None, never an exception."""
    try:
        return _find_cover_url(toc_html, toc_url, title)
    except Exception as exc:  # noqa: BLE001 - metadata is best effort
        log.get().warning("find_cover_url_failed", error=f"{type(exc).__name__}: {exc}")
        return None
