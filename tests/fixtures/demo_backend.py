"""A novelhog with a small library of public-domain novels, for the documentation screenshots.

Like `fake_backend` (the real API and worker on a temporary database, nothing touches the network),
but the "site" is a catalogue of real novels from Project Gutenberg in `tests/fixtures/demo/`: each
has a cover, the title of every chapter, and the text of its first chapters (see `SOURCES.md`
under `docs/assets/screenshots/`). `web/e2e/screenshots.ts` drives it.

    uv run python -m tests.fixtures.demo_backend --dist web/dist --port 8766
"""

import argparse
import functools
import html
import json
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from novelhog.images import FetchedImage
from novelhog.scraper_adapter.adapter import FetchedChapter, TocResult
from tests.fixtures.fake_backend import chapter_url, serve_backend

DEMO_HOST = "gutenberg.org"  # where the texts come from, and what a library card shows
CATALOGUE_DIR = Path(__file__).parent / "demo"
IMPORT_DELAY_S = 0.5  # long enough to see "Importing…", short enough not to slow a capture run
STEP_DELAY_S = 0.3


def toc_url(slug: str) -> str:
    """The table-of-contents URL of the novel `slug`."""
    return f"https://{DEMO_HOST}/{slug}"


def cover_url(slug: str) -> str:
    """The cover URL of the novel `slug`."""
    return f"https://{DEMO_HOST}/covers/{slug}.jpg"


@functools.cache
def _novel(slug: str) -> dict[str, Any]:
    path = CATALOGUE_DIR / f"{slug}.json"
    if not path.is_file():
        msg = f"{slug!r} is not in the demo library"
        raise ValueError(msg)
    novel: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return novel


class DemoAdapter:
    """The slice of `ScraperAdapter` the worker uses, answering from the demo catalogue."""

    def supported_hosts(self) -> list[str]:
        return [DEMO_HOST]

    def sync_toc(self, novel_id: str, host: str, toc_url: str) -> TocResult:
        time.sleep(IMPORT_DELAY_S)
        slug = toc_url.rstrip("/").rsplit("/", 1)[1]
        novel = _novel(slug)
        urls = [chapter_url(toc_url, n) for n in range(1, len(novel["chapters"]) + 1)]
        titles = {
            url: chapter["title"] for url, chapter in zip(urls, novel["chapters"], strict=True)
        }
        return TocResult(
            title=novel["title"],
            language="en",
            cover_url=cover_url(slug),
            urls=urls,
            toc_html="<html></html>",
            chapter_titles=titles,
        )

    def fetch_chapter(self, novel_id: str, host: str, toc_url: str, url: str) -> FetchedChapter:
        time.sleep(STEP_DELAY_S)
        slug = toc_url.rstrip("/").rsplit("/", 1)[1]
        chapter = _novel(slug)["chapters"][int(url.rsplit("-", 1)[1]) - 1]
        if chapter["paragraphs"] is None:
            msg = f"{chapter['title']} has no text in the demo library"
            raise ValueError(msg)
        body = "".join(f"<p>{paragraph}</p>" for paragraph in chapter["paragraphs"])
        return FetchedChapter(title=html.unescape(chapter["title"]), body_html=body)

    def request_delay(self, host: str) -> float:
        return 0.0


class DemoImages:
    """An image fetcher that serves the demo covers and nothing else."""

    def fetch(self, url: str) -> FetchedImage | None:
        prefix = f"https://{DEMO_HOST}/covers/"
        if not url.startswith(prefix):
            return None
        cover = CATALOGUE_DIR / url.removeprefix(prefix)
        if not cover.is_file():
            return None
        return FetchedImage(data=cover.read_bytes(), mime="image/jpeg")


def main(argv: Sequence[str] | None = None) -> int:
    """Serve the demo backend until stopped; the data lives in a temporary directory."""
    parser = argparse.ArgumentParser(prog="demo_backend")
    parser.add_argument("--dist", type=Path, required=True, help="the built SPA (web/dist)")
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args(argv)
    serve_backend(args.dist, args.port, DemoAdapter(), DemoImages(), DEMO_HOST)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
