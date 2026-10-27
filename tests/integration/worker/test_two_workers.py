"""RF-5: two worker processes on one database (tasks spec sections 4 and 6).

Each worker is a `Worker` on its own thread with its own `Repo` (its own engine), as two processes
would be. The adapter records what runs at the same time; the drain also samples the tasks table
for two `running` rows of one `(kind, entity_id)` and ends with a build of a waiting export.
"""

import threading
import time
from collections.abc import Iterator
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select
from structlog.testing import capture_logs

from novelhog.config import Settings
from novelhog.domain.chapters import ChapterState
from novelhog.domain.exports import ExportState, Format
from novelhog.domain.tasks import TaskKind, TaskState
from novelhog.repo import Repo, db
from novelhog.repo.schema import hosts, tasks
from novelhog.repo.tasks import TaskRow
from novelhog.scraper_adapter.adapter import FetchedChapter, TocResult
from novelhog.scraper_adapter.errors import SiteThrottled
from novelhog.storage import LocalStaticStorage
from novelhog.worker.loop import Worker
from tests.conftest import FakeClock
from tests.integration.task_fakes import force_claim, live_task, tasks_of
from tests.integration.worker.conftest import HOST, URL, FakeAdapter, FakeFetcher

CHAPTERS = 12
WAIT_S = 30.0


class OverlapAdapter(FakeAdapter):
    """Records every `(kind, entity)` it works on and fails the test on a concurrent duplicate.

    `toc_gate`, when set, holds every TOC sync until it is opened.
    """

    def __init__(self) -> None:
        super().__init__()
        self.urls = [f"{URL}/{n}" for n in range(1, CHAPTERS + 1)]
        self.toc_urls = list(self.urls)
        self._lock = threading.Lock()
        self.active: set[str] = set()
        self.duplicates: list[str] = []
        self.toc_syncs = 0
        self.toc_started = threading.Event()
        self.toc_gate: threading.Event | None = None

    def _enter(self, key: str) -> None:
        with self._lock:
            if key in self.active:
                self.duplicates.append(key)
            self.active.add(key)

    def _leave(self, key: str) -> None:
        with self._lock:
            self.active.discard(key)

    def sync_toc(self, novel_id: str, host: str, toc_url: str) -> TocResult:
        self._enter(novel_id)
        try:
            with self._lock:
                self.toc_syncs += 1
            self.toc_started.set()
            if self.toc_gate is not None:
                assert self.toc_gate.wait(WAIT_S)
            time.sleep(0.01)
            return TocResult("Lantern", "en", None, list(self.urls), "<html></html>")
        finally:
            self._leave(novel_id)

    def fetch_chapter(self, novel_id: str, host: str, toc_url: str, url: str) -> FetchedChapter:
        self._enter(url)
        try:
            with self._lock:
                self.fetched.append(url)
            time.sleep(0.005)
            return FetchedChapter("A title", "<p>Hi</p>")
        finally:
            self._leave(url)


@pytest.fixture
def other_repo(repo: Repo, migrated_database: str, clock: FakeClock) -> Iterator[Repo]:
    """A second `Repo` on the database `repo` emptied, as a second process would open it."""
    other = Repo(migrated_database, clock=clock)
    yield other
    other.engine.dispose()


@pytest.fixture
def fast(settings: Settings) -> Settings:
    """Settings without a request delay, so the workers never wait for politeness."""
    return replace(settings, request_delay=0.0)


@pytest.fixture
def adapter() -> OverlapAdapter:
    return OverlapAdapter()


def make_worker(
    repo: Repo, adapter: OverlapAdapter, settings: Settings, worker_id: str, tmp_path: Path
) -> Worker:
    storage = LocalStaticStorage(tmp_path / "static", "/static")
    own = replace(settings, worker_id=worker_id)
    return Worker(repo, adapter, FakeFetcher(), storage, {}, own, stop=threading.Event())


def run_both(workers: list[Worker], done: Any) -> None:
    """Run the workers on threads until `done()` is true, then stop them."""
    threads = [threading.Thread(target=worker.run, daemon=True) for worker in workers]
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + WAIT_S
    try:
        while not done() and time.monotonic() < deadline:
            time.sleep(0.02)
    finally:
        for worker in workers:
            worker.request_stop()
        for thread in threads:
            thread.join(WAIT_S)
    assert done()


def downloaded(repo: Repo, novel_id: str) -> bool:
    chapters, _ = repo.chapters.list(novel_id)
    return len(chapters) == CHAPTERS and all(
        chapter.state is ChapterState.DOWNLOADED for chapter in chapters
    )


def running_twice(repo: Repo) -> list[tuple[str, str]]:
    """Every `(kind, entity_id)` with more than one `running` task row right now."""
    with db.read(repo.engine) as conn:
        rows = conn.execute(
            select(tasks.c.kind, tasks.c.entity_id)
            .where(tasks.c.state == TaskState.RUNNING.value)
            .group_by(tasks.c.kind, tasks.c.entity_id)
            .having(func.count() > 1)
        ).all()
    return [(row.kind, row.entity_id) for row in rows]


def running(repo: Repo, entity_id: str) -> bool:
    return any(task.state is TaskState.RUNNING for task in tasks_of(repo, entity_id))


class TestTwoWorkers__DrainingOneQueue:
    @pytest.fixture
    def drained(
        self, repo: Repo, other_repo: Repo, adapter: OverlapAdapter, fast: Settings,
        tmp_path: Path,
    ) -> str:  # fmt: skip
        repo.hosts.publish([HOST])
        novel_id = repo.novels.create(URL, followed=True).id
        workers = [
            make_worker(repo, adapter, fast, "worker-a", tmp_path),
            make_worker(other_repo, adapter, fast, "worker-b", tmp_path),
        ]
        assert workers[0].step() == "task"  # the first import
        repo.chapters.patch(novel_id, ChapterState.PENDING, list(range(1, CHAPTERS + 1)))
        view, _ = repo.exports.create(novel_id, 1, CHAPTERS, Format.EPUB)  # waits for them all

        def done() -> bool:
            self.doubles.extend(running_twice(repo))
            ready = repo.exports.get(novel_id, view.id).state is ExportState.READY
            return downloaded(repo, novel_id) and ready

        run_both(workers, done)
        self.doubles.extend(running_twice(repo))
        self.export_id = view.id
        return novel_id

    doubles: list[tuple[str, str]]
    export_id: str

    @pytest.fixture(autouse=True)
    def _samples(self) -> None:
        self.doubles = []

    def test_workers__never_run_one_kind_and_entity_twice_at_once(
        self, adapter: OverlapAdapter, drained: str
    ) -> None:
        assert adapter.duplicates == []

    def test_workers__download_every_chapter_exactly_once(
        self, repo: Repo, adapter: OverlapAdapter, drained: str
    ) -> None:
        assert sorted(adapter.fetched) == sorted(adapter.urls)
        for chapter in repo.chapters.list(drained)[0]:
            [task] = tasks_of(repo, chapter.id)
            assert (task.kind, task.state, task.attempts) == (
                TaskKind.CHAPTER_DOWNLOAD, TaskState.DONE, 1,
            )  # fmt: skip

    def test_workers__sync_the_novel_once(self, adapter: OverlapAdapter, drained: str) -> None:
        assert adapter.toc_syncs == 1

    def test_workers__build_the_waiting_export_once(self, repo: Repo, drained: str) -> None:
        [task] = tasks_of(repo, self.export_id)
        assert (task.kind, task.state, task.attempts) == (
            TaskKind.EXPORT_BUILD, TaskState.DONE, 1,
        )  # fmt: skip

    def test_workers__never_hold_two_running_rows_for_one_kind_and_entity(
        self, drained: str
    ) -> None:
        assert self.doubles == []


class ThrottlingAdapter(OverlapAdapter):
    """Answers the first `throttles` chapter fetches with 429 and `Retry-After: 60`.

    Every attempt is recorded with the fake clock's time in `attempts`; a successful fetch also
    lands in `fetched`.
    """

    def __init__(self, clock: FakeClock, throttles: int) -> None:
        super().__init__()
        self._clock = clock
        self._throttles = throttles
        self.attempts: list[tuple[str, datetime]] = []

    def fetch_chapter(self, novel_id: str, host: str, toc_url: str, url: str) -> FetchedChapter:
        with self._lock:
            self.attempts.append((url, self._clock()))
            throttle = self._throttles > 0
            self._throttles -= 1
        if throttle:
            raise SiteThrottled("429", retry_after=60)
        return super().fetch_chapter(novel_id, host, toc_url, url)


def reach_after(repo: Repo) -> datetime | None:
    with db.read(repo.engine) as conn:
        value: datetime | None = conn.execute(
            select(hosts.c.reach_after).where(hosts.c.host == HOST)
        ).scalar_one()
    return value


class TestTwoWorkers__WhenTheHostThrottles:
    def test_workers__pause_together_and_download_every_chapter_once_afterwards(
        self, repo: Repo, other_repo: Repo, fast: Settings, tmp_path: Path, clock: FakeClock
    ) -> None:
        adapter = ThrottlingAdapter(clock, throttles=1)
        awake = replace(fast, idle_sleep=0)  # idle workers must notice the fake clock moving on
        repo.hosts.publish([HOST])
        novel_id = repo.novels.create(URL, followed=True).id
        workers = [
            make_worker(repo, adapter, awake, "worker-a", tmp_path),
            make_worker(other_repo, adapter, awake, "worker-b", tmp_path),
        ]
        assert workers[0].step() == "task"  # the first import
        repo.chapters.patch(novel_id, ChapterState.PENDING, list(range(1, CHAPTERS + 1)))
        start = clock()
        paused_until = start + timedelta(seconds=60)
        resumed = threading.Event()

        def done() -> bool:
            if not resumed.is_set():
                pending = [t for t in tasks_of_novel(repo) if t.state is TaskState.QUEUED]
                if len(pending) == CHAPTERS and all(t.run_after == paused_until for t in pending):
                    attempts = len(adapter.attempts)
                    time.sleep(0.2)  # both workers keep going; none may contact the host
                    assert len(adapter.attempts) == attempts
                    clock.advance(seconds=61)
                    resumed.set()
                return False
            return downloaded(repo, novel_id)

        run_both(workers, done)

        assert resumed.is_set()
        assert reach_after(repo) is not None
        assert sorted(adapter.fetched) == sorted(adapter.urls)  # each chapter once
        assert len(adapter.attempts) == CHAPTERS + 1  # plus the one that was throttled
        assert all(when >= paused_until for _, when in adapter.attempts[1:])


def tasks_of_novel(repo: Repo) -> list[Any]:
    with db.read(repo.engine) as conn:
        rows = conn.execute(
            select(tasks).where(tasks.c.kind == TaskKind.CHAPTER_DOWNLOAD.value)
        ).all()
    return [TaskRow.from_row(row) for row in rows]


class TestTwoWorkers__WhenARefreshIsQueuedDuringARunningSync:
    @pytest.fixture
    def syncing(
        self, repo: Repo, other_repo: Repo, adapter: OverlapAdapter, fast: Settings,
        tmp_path: Path,
    ) -> Iterator[tuple[str, Worker, Worker, threading.Event]]:  # fmt: skip
        """Worker A runs the novel's sync, held open; a manual sync then queues a successor."""
        repo.hosts.publish([HOST])
        novel_id = repo.novels.create(URL, followed=True).id
        adapter.toc_gate = threading.Event()
        first = make_worker(repo, adapter, fast, "worker-a", tmp_path)
        second = make_worker(other_repo, adapter, fast, "worker-b", tmp_path)
        thread = threading.Thread(target=first.step, daemon=True)
        thread.start()
        assert adapter.toc_started.wait(WAIT_S)
        repo.novels.request_sync(novel_id)
        try:
            yield novel_id, first, second, adapter.toc_gate
        finally:
            adapter.toc_gate.set()
            thread.join(WAIT_S)

    def test_step__the_other_worker_does_not_claim_it(
        self, other_repo: Repo,
        syncing: tuple[str, Worker, Worker, threading.Event],
    ) -> None:  # fmt: skip
        _, _, second, _ = syncing

        assert second.step() == "idle"

    def test_run_task__a_racing_claim_of_it_is_deferred_and_released(
        self, repo: Repo, other_repo: Repo, clock: FakeClock,
        syncing: tuple[str, Worker, Worker, threading.Event],
    ) -> None:  # fmt: skip
        novel_id, _, second, _ = syncing
        successor = live_task(repo, novel_id)
        # The claim's NOT EXISTS ran before the sync started: the race start() must handle.
        claimed = force_claim(repo, successor.id, "worker-b", clock().replace(year=2100))

        with capture_logs() as logs:
            second._runner.run_task(claimed)  # noqa: SLF001

        assert [line["event"] for line in logs] == ["task_deferred"]
        row = live_task(repo, novel_id)
        assert (row.id, row.state, row.claimed_by, row.attempts) == (
            successor.id, TaskState.QUEUED, None, 0,
        )  # fmt: skip

    def test_step__the_successor_runs_after_the_sync_finishes(
        self, repo: Repo, adapter: OverlapAdapter,
        syncing: tuple[str, Worker, Worker, threading.Event],
    ) -> None:  # fmt: skip
        novel_id, _, second, gate = syncing
        successor = live_task(repo, novel_id)
        gate.set()
        deadline = time.monotonic() + WAIT_S
        while running(repo, novel_id) and time.monotonic() < deadline:
            time.sleep(0.01)

        assert second.step() == "task"

        assert [t.state for t in tasks_of(repo, novel_id) if t.id == successor.id] == [
            TaskState.DONE
        ]
        assert adapter.toc_syncs == 2
