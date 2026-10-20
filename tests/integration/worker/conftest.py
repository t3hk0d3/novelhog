"""Fixtures shared by the worker download and loop tests."""

import threading
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import insert, select

from novelhog import config
from novelhog.config import Settings
from novelhog.images import FetchedImage
from novelhog.repo import Repo, db
from novelhog.repo.schema import chapters
from novelhog.scraper_adapter.adapter import FetchedChapter, TocResult
from tests.integration.helpers import chapter_row

HOST = "example.com"
URL = "https://example.com/novel"
ME = "worker-a"
DELAY = 0.25
IDLE = 30


class RecordingStop(threading.Event):
    """A stop event whose waits never sleep: they record the timeout and return immediately.

    `stop_after_waits` sets the event on that many waits so a loop under test ends.
    """

    def __init__(self, stop_after_waits: int | None = None) -> None:
        super().__init__()
        self.waits: list[float | None] = []
        self._stop_after = stop_after_waits

    def wait(self, timeout: float | None = None) -> bool:
        self.waits.append(timeout)
        if self._stop_after is not None and len(self.waits) >= self._stop_after:
            self.set()
        return self.is_set()


class FakeAdapter:
    """Serves prepared chapters and TOCs; `errors` maps a chapter url to an exception."""

    def __init__(self) -> None:
        self.titles: dict[str, str | None] = {}
        self.bodies: dict[str, str] = {}
        self.errors: dict[str, Exception] = {}
        self.toc_error: Exception | None = None
        self.hook: Any = None
        self.fetched: list[str] = []
        self.delays: dict[str, float] = {}

    def supported_hosts(self) -> list[str]:
        return [HOST]

    def request_delay(self, host: str) -> float:
        return self.delays.get(host, 0.0)

    def sync_toc(self, novel_id: str, host: str, toc_url: str) -> TocResult:
        if self.toc_error is not None:
            raise self.toc_error
        return TocResult("Lantern", "en", None, [f"{URL}/1"], "<html></html>")

    def fetch_chapter(self, novel_id: str, host: str, toc_url: str, url: str) -> FetchedChapter:
        self.fetched.append(url)
        if self.hook is not None:
            self.hook(url)
        if url in self.errors:
            raise self.errors[url]
        return FetchedChapter(self.titles.get(url, "A title"), self.bodies.get(url, "<p>Hi</p>"))


class FakeFetcher:
    def __init__(self) -> None:
        self.image: FetchedImage | None = FetchedImage(b"\x89PNG fake", "image/png")

    def fetch(self, url: str) -> FetchedImage | None:
        return self.image


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return config.load(
        {
            "NOVELHOG_DATA_DIR": str(tmp_path),
            "NOVELHOG_WORKER_ID": ME,
            "NOVELHOG_CLAIM_TTL": "300",
            "NOVELHOG_REQUEST_DELAY": str(DELAY),
            "NOVELHOG_IDLE_SLEEP": str(IDLE),
            "NOVELHOG_BATCH_SIZE": "10",
        }
    )


@pytest.fixture
def adapter() -> FakeAdapter:
    return FakeAdapter()


@pytest.fixture
def fetcher() -> FakeFetcher:
    return FakeFetcher()


@pytest.fixture
def novel_id(repo: Repo) -> str:
    repo.hosts.publish([HOST])
    return repo.novels.create(URL, followed=True).id


def add_chapter(repo: Repo, novel_id: str, at: int, **overrides: Any) -> str:
    """Insert a `pending` chapter at position `at` (queued now) unless overridden."""
    values: dict[str, Any] = {"state": "pending", "queued_at": repo.clock()} | overrides
    row = chapter_row(id=f"CH{at:012d}", novel_id=novel_id, url=f"{URL}/{at}", position=at)
    with db.write(repo.engine) as conn:
        conn.execute(insert(chapters).values(**(row | values)))
    return str(row["id"])


def chapter_of(repo: Repo, chapter_id: str) -> Any:
    with db.read(repo.engine) as conn:
        return conn.execute(select(chapters).where(chapters.c.id == chapter_id)).one()
