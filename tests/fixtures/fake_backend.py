"""A self-contained novelhog for the Playwright smoke tests (spec section 13).

Runs the real API (`create_app`) on a temporary SQLite database, serves the built SPA with a
fallback to `index.html` and `/static` from a temporary directory on the same origin, and runs a
real `Worker` in a thread with a fake scraper adapter: a deterministic table of contents in which
`fetch_chapter` fails for one known chapter. Nothing touches the network.

    uv run python -m tests.fixtures.fake_backend --dist web/dist --port 8765
"""

import argparse
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
COVER_URL = f"https://{HOST}/cover.png"
SSE_POLL_S = 0.2
GRACEFUL_S = 3
STEP_DELAY_S = 0.4  # keeps the "Importing…" and "Pending" states on screen long enough to see
READY_TIMEOUT_S = 15.0


def chapter_url(toc_url: str, number: int) -> str:
    """The URL of chapter `number` of the novel whose TOC is at `toc_url`."""
    return f"{toc_url.rstrip('/')}/chapter-{number}"


def _png() -> bytes:
    """A valid 1x1 PNG, built in place so no binary fixture is needed."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    header = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    pixel = zlib.compress(b"\x00\x2b\x6c\xb0")
    return (
        b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", pixel) + chunk(b"IEND", b"")
    )


class FakeAdapter:
    """The slice of `ScraperAdapter` the worker uses, with canned data."""

    def supported_hosts(self) -> list[str]:
        return [HOST]

    def sync_toc(self, novel_id: str, host: str, toc_url: str) -> TocResult:
        time.sleep(STEP_DELAY_S)
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
        return FetchedChapter(title=f"Chapter {number}: Fake", body_html=body)

    def request_delay(self, host: str) -> float:
        return 0.0


class FakeImages:
    """An image fetcher that returns the same tiny PNG for any URL."""

    def fetch(self, url: str) -> FetchedImage | None:
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
