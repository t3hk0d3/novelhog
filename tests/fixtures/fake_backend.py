"""A self-contained novelhog for the Playwright smoke tests (spec section 13).

Runs the real API (`create_app`) on a temporary SQLite database, serves the built SPA with a
fallback to `index.html` and `/static` from a temporary directory on the same origin, and runs a
real `Worker` in a thread with a fake scraper adapter: a deterministic table of contents in which
`fetch_chapter` fails for one known chapter. Nothing touches the network.

    uv run python -m tests.fixtures.fake_backend --dist web/dist --port 8765
"""

import argparse
import math
import struct
import tempfile
import threading
import time
import zlib
from collections.abc import Sequence
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from novelhog.api import serve
from novelhog.api.app import create_app
from novelhog.config import Settings, load
from novelhog.images import FetchedImage
from novelhog.repo import Repo
from novelhog.scraper_adapter.adapter import FetchedChapter, TocResult
from novelhog.storage import LocalStaticStorage
from novelhog.worker.loop import Worker

HOST = "fake.test"
TITLE = "The Fake Chronicle"
CHAPTER_COUNT = 5
FAILING_CHAPTER = 2
FAILURE_MESSAGE = "chapter 2 is unreadable"
IMAGE_CHAPTER = 5  # carries two images and a long unbroken word (the reader image e2e)
COVER_URL = f"https://{HOST}/cover.png"
SSE_POLL_S = 0.2
GRACEFUL_S = 3
STEP_DELAY_S = 0.5  # keeps the "Pending" and waiting-export states on screen long enough to see
IMPORT_DELAY_S = 2.0  # keeps "Importing…" on screen long enough to see
READY_TIMEOUT_S = 15.0


def chapter_url(toc_url: str, number: int) -> str:
    """The URL of chapter `number` of the novel whose TOC is at `toc_url`."""
    return f"{toc_url.rstrip('/')}/chapter-{number}"


def _png_of_rows(width: int, height: int, rows: list[bytes]) -> bytes:
    """A valid RGB PNG from `height` rows of `width` * 3 bytes, built in place (no fixture file)."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + row for row in rows)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def _png(width: int = 1, height: int = 1, rgb: tuple[int, int, int] = (0x2B, 0x6C, 0xB0)) -> bytes:
    """A valid solid-colour PNG."""
    return _png_of_rows(width, height, [bytes(rgb) * width] * height)


def _map_png(width: int, height: int) -> bytes:
    """A parchment "map": a faded gradient, a grid and a winding river (rows are built in bulk)."""
    grid = 160
    rows: list[bytes] = []
    for y in range(height):
        shade = 232 - 28 * y // height
        row = bytearray(bytes((shade, shade - 12, shade - 44)) * width)
        if y % grid < 3:
            row[:] = bytes((150, 120, 84)) * width
        else:
            for x in range(0, width, grid):
                row[x * 3 : (x + 3) * 3] = bytes((150, 120, 84)) * 3
            bend = int(width * 0.3 + width * 0.12 * math.sin(y / height * 5))
            row[bend * 3 : (bend + 46) * 3] = bytes((84, 134, 170)) * 46
        rows.append(bytes(row))
    return _png_of_rows(width, height, rows)


class FakeAdapter:
    """The slice of `ScraperAdapter` the worker uses, with canned data."""

    def supported_hosts(self) -> list[str]:
        return [HOST]

    def sync_toc(self, novel_id: str, host: str, toc_url: str) -> TocResult:
        time.sleep(IMPORT_DELAY_S)
        urls = [chapter_url(toc_url, n) for n in range(1, CHAPTER_COUNT + 1)]
        return TocResult(
            title=TITLE, language="en", cover_url=COVER_URL, urls=urls, toc_html="<html></html>"
        )

    def fetch_chapter(self, novel_id: str, host: str, toc_url: str, url: str) -> FetchedChapter:
        time.sleep(STEP_DELAY_S)
        number = int(url.rsplit("-", 1)[1])
        if number == FAILING_CHAPTER:
            raise ValueError(FAILURE_MESSAGE)
        body = f"<p>Text of chapter {number}.</p>"
        if number == IMAGE_CHAPTER:
            body = (
                "<p>Before the map.</p>"
                "<p>Mira unrolled the parchment across the cartographer's table and weighted the "
                "corners with whatever was nearest: a candle stub, a cracked inkwell, two of her "
                "own boots. The room smelled of lamp oil and old rain.</p>"
                "<p>The river was wrong. She had walked its banks every autumn since she was nine, "
                "and it had never once bent east at the old mill.</p>"
                f'<p><img src="https://{HOST}/wide.png" alt="A wide map"></p>'
                "<p>Someone had redrawn it, and not badly. Whoever it was had even copied the "
                "small sigil in the corner, the one her teacher had always said was a joke.</p>"
                f'<p><img src="https://{HOST}/sigil.png" alt="A tiny sigil"></p>'
                "<p>After the images.</p>"
                f"<p>A very long word: {'x' * 200}</p>"
            )
        return FetchedChapter(title=f"Chapter {number}: Fake", body_html=body)

    def request_delay(self, host: str) -> float:
        return 0.0


class FakeImages:
    """An image fetcher: a 2400x800 PNG for "wide" URLs, a 64x64 one for "sigil" ones, else 1x1."""

    def fetch(self, url: str) -> FetchedImage | None:
        if "wide" in url:
            return FetchedImage(data=_map_png(2400, 800), mime="image/png")
        if "sigil" in url:
            return FetchedImage(data=_png(64, 64, (0xB0, 0x2B, 0x6C)), mime="image/png")
        return FetchedImage(data=_png(), mime="image/png")


def _settings(data_dir: Path) -> Settings:
    return load(
        {
            "NOVELHOG_DATA_DIR": str(data_dir),
            "NOVELHOG_IDLE_SLEEP": "1",
            "NOVELHOG_REQUEST_DELAY": "0",
            "NOVELHOG_WORKER_ID": "e2e-worker",
        }
    )


def _serve_spa(app: FastAPI, dist: Path, static_dir: Path) -> None:
    """Add `/static` and the SPA (with `index.html` fallback) to `app`; the API stays first."""
    root = dist.resolve()
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str) -> FileResponse:
        if path.startswith(("api/", "static/")):
            raise HTTPException(status_code=404)
        candidate = (root / path).resolve()
        if candidate.is_file() and candidate.is_relative_to(root):
            return FileResponse(candidate)
        return FileResponse(root / "index.html")


def _wait_for_hosts(repo: Repo, worker_thread: threading.Thread) -> None:
    """Block until the worker has published its hosts, so the first Add cannot hit a 503."""
    deadline = time.monotonic() + READY_TIMEOUT_S
    while not repo.hosts.is_supported(HOST):
        if not worker_thread.is_alive() or time.monotonic() > deadline:
            msg = "the worker did not publish its hosts"
            raise RuntimeError(msg)
        time.sleep(0.05)


def main(argv: Sequence[str] | None = None) -> int:
    """Serve the fake backend until stopped; the data lives in a temporary directory."""
    parser = argparse.ArgumentParser(prog="fake_backend")
    parser.add_argument("--dist", type=Path, required=True, help="the built SPA (web/dist)")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    with tempfile.TemporaryDirectory(prefix="novelhog-e2e-") as tmp:
        settings = _settings(Path(tmp))
        settings.static_dir.mkdir(parents=True)
        (settings.data_dir / "db").mkdir()
        repo = Repo(
            settings.database_url,
            static_base_url=settings.static_base_url,
            refresh_min_interval=settings.refresh_min_interval,
        )
        repo.migrate()
        worker = Worker(
            repo,
            FakeAdapter(),
            FakeImages(),
            LocalStaticStorage(settings.static_dir, settings.static_base_url),
            {},
            settings,
        )
        worker_thread = threading.Thread(target=worker.run, name="e2e-worker")
        worker_thread.start()
        try:
            _wait_for_hosts(repo, worker_thread)
            app = create_app(settings, repo=repo, sse_poll_s=SSE_POLL_S)
            _serve_spa(app, args.dist, settings.static_dir)
            serve.run(app, host="127.0.0.1", port=args.port, graceful_s=GRACEFUL_S)
        finally:
            worker.request_stop()
            worker_thread.join()
            repo.engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
