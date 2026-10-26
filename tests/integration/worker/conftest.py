"""Fixtures shared by the worker download and loop tests."""

import threading
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import insert, select, update

from novelhog import config
from novelhog.config import Settings
from novelhog.domain.tasks import PRIORITY_USER, TaskKind
from novelhog.images import FetchedImage
from novelhog.repo import Repo, db
from novelhog.repo.novels import lock_novel
from novelhog.repo.schema import chapters, tasks
from novelhog.scraper_adapter.adapter import FetchedChapter, TocResult
from tests.conftest import FakeClock
from tests.integration.helpers import chapter_row

HOST = "example.com"
URL = "https://example.com/novel"
ME = "worker-a"
DELAY = 0.25
IDLE = 30


class RecordingStop(threading.Event):
    """A stop event whose waits never sleep: they record the timeout and return immediately.

    `stop_after_waits` sets the event on that many waits so a loop under test ends. With `clock`,
    each wait moves the fake clock forward by its timeout, as a real wait would.
    """

    def __init__(self, stop_after_waits: int | None = None, clock: FakeClock | None = None) -> None:
        super().__init__()
        self.waits: list[float | None] = []
        self._stop_after = stop_after_waits
        self._clock = clock

    def wait(self, timeout: float | None = None) -> bool:
        self.waits.append(timeout)
        if self._clock is not None and timeout is not None and not self.is_set():
            self._clock.advance(seconds=timeout)  # the wait took that long
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
        self.toc_urls = [f"{URL}/1"]

    def supported_hosts(self) -> list[str]:
        return [HOST]

    def request_delay(self, host: str) -> float:
        return self.delays.get(host, 0.0)

    def sync_toc(self, novel_id: str, host: str, toc_url: str) -> TocResult:
        if self.toc_error is not None:
            raise self.toc_error
        return TocResult("Lantern", "en", None, list(self.toc_urls), "<html></html>")

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
    """Insert a chapter at position `at`, `pending` unless overridden, as a worker would see it.

    A positioned `pending` chapter gets its queued download task, due now at user priority. A
    `downloading` one gets that task running under `claimed_by` until `claimed_until`.
    """
    claimed_by = overrides.pop("claimed_by", None)
    claimed_until = overrides.pop("claimed_until", None)
    values: dict[str, Any] = {"state": "pending"} | overrides
    row = chapter_row(id=f"CH{at:012d}", novel_id=novel_id, url=f"{URL}/{at}", position=at)
    row |= values
    now = repo.clock()
    queue = repo.tasks._queue  # noqa: SLF001  (tests set up rows through the internal queue)
    with db.write(repo.engine) as conn:
        assert lock_novel(conn, novel_id)
        conn.execute(insert(chapters).values(**row))
        if row["state"] in ("pending", "downloading") and row["position"] is not None:
            task = queue.enqueue(
                conn,
                TaskKind.CHAPTER_DOWNLOAD,
                row["id"],
                novel_id,
                priority=PRIORITY_USER,
                ordinal=row["position"],
                run_after=now,
            )
            if row["state"] == "downloading":
                conn.execute(
                    update(tasks)
                    .where(tasks.c.id == task.id)
                    .values(
                        state="running",
                        attempts=1,
                        started_at=now,
                        claimed_by=claimed_by,
                        claimed_until=claimed_until,
                    )
                )
        queue.stamp(conn)
    return str(row["id"])


def chapter_of(repo: Repo, chapter_id: str) -> Any:
    with db.read(repo.engine) as conn:
        return conn.execute(select(chapters).where(chapters.c.id == chapter_id)).one()
