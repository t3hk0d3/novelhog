"""Title and cover finders for a table-of-contents page (spec section 6.7)."""

import json
import re
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from novelhog.domain.text import strip_xml_invalid

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
        src = (values.get("src") or values.get("data-src", "")).strip()
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
                self.h1 = text
            else:
                self.json_ld.append(self._buffer)
            self._text_target = None


def _parse(html: str) -> _Page:
    page = _Page()
    page.feed(html)
    page.close()
    return page


def _clean(value: str | None) -> str | None:
    cleaned = strip_xml_invalid(value or "").strip()
    return cleaned or None


def find_title(toc_html: str) -> str | None:
    """Detect the novel title: og:title, then `<title>` without its site suffix, then `<h1>`."""
    page = _parse(toc_html)
    return (
        _clean(page.meta.get("og:title"))
        or _clean(_SITE_SUFFIX.sub("", strip_xml_invalid(page.title).strip()))
        or _clean(page.h1)
    )


def _json_ld_image(blocks: list[str]) -> str | None:
    for block in blocks:
        try:
            data = json.loads(block)
        except ValueError:
            continue
        found = _image_in(data)
        if found:
            return found
    return None


def _image_in(data: object) -> str | None:
    if isinstance(data, list):
        return next((found for item in data if (found := _image_in(item))), None)
    if not isinstance(data, dict):
        return None
    image = data.get("image")
    if isinstance(image, str) and image.strip():
        return image.strip()
    if isinstance(image, dict | list):
        return _image_in({"image": image.get("url")} if isinstance(image, dict) else image[:1])
    return _image_in(data.get("@graph"))


def find_cover_url(toc_html: str, toc_url: str, title: str | None) -> str | None:
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
