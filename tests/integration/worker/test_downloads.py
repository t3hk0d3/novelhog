"""Chapter downloads (spec section 6.4) as `chapter_download` tasks, run through the runner.

The v1 batch scenarios run one task per claim (tasks spec 1.1): what v1 did to "the rest of the
batch" now applies to the chapters still queued. Politeness is per host (tasks spec section 6).
"""

import random
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import update
from structlog.testing import capture_logs

from novelhog import html_to_markdown
from novelhog.chapter_store import ChapterStore
from novelhog.config import Settings
from novelhog.domain.chapters import ChapterState
from novelhog.domain.tasks import TaskState
from novelhog.repo import Repo, db
from novelhog.repo.schema import hosts, tasks
from novelhog.repo.tasks import TaskRow
from novelhog.scraper_adapter.errors import InfrastructureError, SiteError, SiteUnreachable
from novelhog.storage import LocalStaticStorage
from novelhog.worker.reachability import SITE_UNREACHABLE_RETRIES, Reachability
from novelhog.worker.runner import Runner, RunnerBackoff
from novelhog.worker.tasks import default_processors
from novelhog.worker.tasks.base import TaskContext
from tests.conftest import FakeClock
from tests.integration.task_fakes import claim_task, live_task, tasks_of
from tests.integration.worker.conftest import (
    DELAY,
    HOST,
    ME,
    URL,
    FakeAdapter,
    FakeFetcher,
    RecordingStop,
    add_chapter,
    chapter_of,
)

TTL = 300
OTHER_HOST = "other.example"
OTHER_URL = f"https://{OTHER_HOST}/novel"


class StubHeartbeat:
    """Replaces `Heartbeat`: captures `renew` and lets a test set `lost`."""

    def __init__(self, renew: Callable[[], bool], interval_s: float) -> None:
        self.renew = renew
        self.interval_s = interval_s
        self.lost = False

    def __enter__(self) -> StubHeartbeat:
        return self

    def __exit__(self, *_: object) -> None:
        return None


@pytest.fixture
def novel_id(repo: Repo) -> str:
    """A novel whose first refresh is claimed elsewhere, so only downloads run here."""
    repo.hosts.publish([HOST, OTHER_HOST])
    novel = repo.novels.create(URL, followed=True).id
    assert repo.tasks.claim("worker-parked", 86400) is not None
    return novel


@pytest.fixture
def stop(clock: FakeClock) -> RecordingStop:
    return RecordingStop(clock=clock)


@pytest.fixture
def reachability() -> Reachability:
    return Reachability()


@pytest.fixture
def beats() -> list[StubHeartbeat]:
    return []


@pytest.fixture
def runner(  # noqa: PLR0913
    repo: Repo,
    adapter: FakeAdapter,
    fetcher: FakeFetcher,
    settings: Settings,
    tmp_path: Path,
    stop: RecordingStop,
    reachability: Reachability,
    beats: list[StubHeartbeat],
) -> Runner:
    def heartbeat(renew: Callable[[], bool], interval_s: float) -> StubHeartbeat:
        beats.append(StubHeartbeat(renew, interval_s))
        return beats[-1]

    ctx = TaskContext(
        adapter=adapter,
        fetcher=fetcher,
        storage=LocalStaticStorage(tmp_path / "static", "/static"),
        chapter_store=ChapterStore(repo),
        settings=settings,
        stop=stop,
        reachability=reachability,
        rng=random.Random(0),  # noqa: S311 - refresh jitter, not security
        heartbeat=heartbeat,
        clock=repo.clock,
    )
    return Runner(repo, default_processors(), ctx)


def queue(repo: Repo, novel_id: str, count: int) -> list[str]:
    """Queue `count` chapters of the novel (positions 1..count)."""
    return [add_chapter(repo, novel_id, at) for at in range(1, count + 1)]


def run_all(runner: Runner) -> int:
    """Run tasks until none is claimable; return how many ran."""
    ran = 0
    while runner.step():
        ran += 1
    return ran


def download_task(repo: Repo, chapter_id: str) -> TaskRow:
    [task] = tasks_of(repo, chapter_id)
    return task


class TestDownload__ForEachQueuedChapter:
    def test_download__completes_with_title_and_has_images(
        self, repo: Repo, novel_id: str, adapter: FakeAdapter, runner: Runner
    ) -> None:
        [chapter_id] = queue(repo, novel_id, 1)
        adapter.titles[f"{URL}/1"] = "The Start"
        adapter.bodies[f"{URL}/1"] = '<p>Hi</p><p><img src="https://example.com/a.png"></p>'

        assert runner.step() is True

        row = chapter_of(repo, chapter_id)
        assert (row.state, row.title, row.has_images) == ("downloaded", "The Start", True)
        task = download_task(repo, chapter_id)
        assert (task.state, task.claimed_by) == (TaskState.DONE, None)

    def test_download__stores_markdown_with_the_image_inlined(
        self, repo: Repo, novel_id: str, adapter: FakeAdapter, runner: Runner
    ) -> None:
        [chapter_id] = queue(repo, novel_id, 1)
        adapter.bodies[f"{URL}/1"] = '<p>Hi</p><p><img src="https://example.com/a.png"></p>'

        runner.step()

        assert "data:image/png;base64," in repo.contents.get(chapter_id)

    def test_download__titles_a_chapter_without_one_by_its_position(
        self, repo: Repo, novel_id: str, adapter: FakeAdapter, runner: Runner
    ) -> None:
        [chapter_id] = queue(repo, novel_id, 1)
        adapter.titles[f"{URL}/1"] = None

        runner.step()

        assert chapter_of(repo, chapter_id).title == "Chapter 1"

    def test_download__runs_one_task_per_claim_in_position_order(
        self, repo: Repo, novel_id: str, adapter: FakeAdapter, runner: Runner
    ) -> None:
        queue(repo, novel_id, 3)

        assert run_all(runner) == 3

        assert adapter.fetched == [f"{URL}/1", f"{URL}/2", f"{URL}/3"]

    def test_download__waits_request_delay_between_chapters_through_stop_wait(
        self, repo: Repo, novel_id: str, runner: Runner, stop: RecordingStop
    ) -> None:
        queue(repo, novel_id, 3)

        run_all(runner)

        assert stop.waits == [DELAY, DELAY]

    def test_download__renews_the_task_claim_through_a_heartbeat(
        self, repo: Repo, novel_id: str, adapter: FakeAdapter, runner: Runner,
        beats: list[StubHeartbeat], clock: FakeClock,
    ) -> None:  # fmt: skip
        [chapter_id] = queue(repo, novel_id, 1)
        renewed: list[bool] = []

        def hook(url: str) -> None:
            clock.advance(seconds=100)
            renewed.append(beats[0].renew())

        adapter.hook = hook

        runner.step()

        assert renewed == [True]
        assert beats[0].interval_s == TTL / 3

    def test_download__heartbeat_reports_false_when_the_claim_is_gone(
        self, repo: Repo, novel_id: str, adapter: FakeAdapter, runner: Runner,
        beats: list[StubHeartbeat],
    ) -> None:  # fmt: skip
        [chapter_id] = queue(repo, novel_id, 1)
        results: list[bool] = []

        def hook(url: str) -> None:
            with db.write(repo.engine) as conn:
                conn.execute(update(tasks).values(claimed_by="someone-else"))
            results.append(beats[0].renew())

        adapter.hook = hook

        runner.step()

        assert results == [False]


class TestDownload__WhenDequeuedAfterTheClaim:
    def test_download__skips_the_chapter_and_the_next_claim_goes_on(
        self, repo: Repo, novel_id: str, adapter: FakeAdapter, runner: Runner
    ) -> None:
        first, _ = queue(repo, novel_id, 2)
        claimed = claim_task(repo, first, ME)
        repo.chapters.patch(novel_id, ChapterState.NOT_REQUESTED, [1])

        with capture_logs() as logs:
            runner.run_task(claimed)
        runner.step()

        assert "task_cancelled" in [line["event"] for line in logs]
        assert adapter.fetched == [f"{URL}/2"]


class TestDownload__WhenFetchingOrConvertingRaises:
    @pytest.mark.parametrize(
        ("error", "message"),
        [
            (SiteError("page gone"), "SiteError: page gone"),
            (ValueError("bad\nhtml"), "ValueError: bad html"),
        ],
    )
    def test_download__fails_the_chapter_with_type_and_summary_and_continues(
        self, repo: Repo, novel_id: str, adapter: FakeAdapter, runner: Runner,
        error: Exception, message: str,
    ) -> None:  # fmt: skip
        first, second = queue(repo, novel_id, 2)
        adapter.errors[f"{URL}/1"] = error

        assert run_all(runner) == 2

        assert (chapter_of(repo, first).state, repo.chapters.get(first).error) == ("error", message)
        assert download_task(repo, first).state is TaskState.FAILED
        assert chapter_of(repo, second).state == "downloaded"

    def test_download__fails_the_chapter_when_conversion_raises(
        self, repo: Repo, novel_id: str, runner: Runner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        [chapter_id] = queue(repo, novel_id, 1)

        def boom(*_: Any) -> None:
            msg = "no parse"
            raise RuntimeError(msg)

        monkeypatch.setattr(html_to_markdown, "convert", boom)

        runner.step()

        assert repo.chapters.get(chapter_id).error == "RuntimeError: no parse"


class TestDownload__WhenFetchingRaisesInfrastructureError:
    def test_download__requeues_the_chapter_keeps_the_others_queued_and_backs_off(
        self, repo: Repo, novel_id: str, adapter: FakeAdapter, runner: Runner
    ) -> None:
        chapter_ids = queue(repo, novel_id, 3)
        queued_at = repo.chapters.get(chapter_ids[0]).queued_at
        adapter.errors[f"{URL}/1"] = InfrastructureError("connection refused")

        with pytest.raises(RunnerBackoff):
            runner.step()

        for chapter_id in chapter_ids:
            view = repo.chapters.get(chapter_id)
            assert (view.state, view.queued_at) == (ChapterState.PENDING, queued_at)
            assert download_task(repo, chapter_id).claimed_by is None
        assert adapter.fetched == [f"{URL}/1"]


class TestDownload__WhenTheSiteIsUnreachable:
    def test_download__below_the_threshold_requeues_and_backs_off(
        self, repo: Repo, novel_id: str, adapter: FakeAdapter, runner: Runner
    ) -> None:
        chapter_ids = queue(repo, novel_id, 2)
        queued_at = repo.chapters.get(chapter_ids[0]).queued_at
        adapter.errors[f"{URL}/1"] = SiteUnreachable("refused")

        for _ in range(SITE_UNREACHABLE_RETRIES - 1):
            with pytest.raises(RunnerBackoff):
                runner.step()
            for chapter_id in chapter_ids:
                view = repo.chapters.get(chapter_id)
                assert (view.state, view.queued_at) == (ChapterState.PENDING, queued_at)

    def test_download__at_the_threshold_fails_the_chapter_and_goes_on_with_the_rest(
        self, repo: Repo, novel_id: str, adapter: FakeAdapter, runner: Runner,
        reachability: Reachability,
    ) -> None:  # fmt: skip
        chapter_ids = queue(repo, novel_id, 3)
        for url in (f"{URL}/1", f"{URL}/2", f"{URL}/3"):
            adapter.errors[url] = SiteUnreachable("refused")
        for _ in range(SITE_UNREACHABLE_RETRIES - 1):
            reachability.failed(HOST)

        assert run_all(runner) == 3

        for chapter_id in chapter_ids:
            assert chapter_of(repo, chapter_id).state == "error"
            assert repo.chapters.get(chapter_id).error == "Site unreachable: refused"

    def test_download__a_success_resets_the_count(
        self, repo: Repo, novel_id: str, adapter: FakeAdapter, runner: Runner,
        reachability: Reachability,
    ) -> None:  # fmt: skip
        _, second = queue(repo, novel_id, 2)
        for _ in range(SITE_UNREACHABLE_RETRIES - 1):
            reachability.failed(HOST)
        runner.step()
        adapter.errors[f"{URL}/2"] = SiteUnreachable("refused")

        with pytest.raises(RunnerBackoff):
            runner.step()

        assert chapter_of(repo, second).state == "pending"

    def test_download__a_site_error_means_the_host_answered_and_resets_the_count(
        self, repo: Repo, novel_id: str, adapter: FakeAdapter, runner: Runner,
        reachability: Reachability,
    ) -> None:  # fmt: skip
        _, second = queue(repo, novel_id, 2)
        adapter.errors[f"{URL}/1"] = SiteError("no content")
        adapter.errors[f"{URL}/2"] = SiteUnreachable("refused")
        for _ in range(SITE_UNREACHABLE_RETRIES - 1):
            reachability.failed(HOST)
        runner.step()

        with pytest.raises(RunnerBackoff):
            runner.step()

        assert chapter_of(repo, second).state == "pending"

    def test_download__counts_hosts_independently(
        self, repo: Repo, novel_id: str, adapter: FakeAdapter, runner: Runner,
        reachability: Reachability,
    ) -> None:  # fmt: skip
        queue(repo, novel_id, 1)
        adapter.errors[f"{URL}/1"] = SiteUnreachable("refused")
        for _ in range(SITE_UNREACHABLE_RETRIES):
            reachability.failed("other.com")

        with pytest.raises(RunnerBackoff):
            runner.step()


class TestDownload__WhenStopIsSetMidDownload:
    def test_download__finishes_the_current_chapter_and_releases_the_next_claim(
        self, repo: Repo, novel_id: str, adapter: FakeAdapter, runner: Runner,
        stop: RecordingStop,
    ) -> None:  # fmt: skip
        first, second, third = queue(repo, novel_id, 3)
        queued_at = repo.chapters.get(third).queued_at
        adapter.hook = lambda url: stop.set()

        runner.step()
        runner.step()

        assert chapter_of(repo, first).state == "downloaded"
        for chapter_id in (second, third):
            view = repo.chapters.get(chapter_id)
            assert (view.state, view.queued_at) == (ChapterState.PENDING, queued_at)
            task = download_task(repo, chapter_id)
            assert (task.state, task.claimed_by, task.attempts) == (TaskState.QUEUED, None, 0)


class TestDownload__WhenTheClaimWasLost:
    def test_download__discards_the_result_logs_task_lost_and_goes_on(
        self, repo: Repo, novel_id: str, adapter: FakeAdapter, runner: Runner
    ) -> None:
        first, second = queue(repo, novel_id, 2)

        def hook(url: str) -> None:
            if url == f"{URL}/1":
                with db.write(repo.engine) as conn:
                    conn.execute(
                        update(tasks).where(tasks.c.entity_id == first).values(claimed_by="x")
                    )

        adapter.hook = hook

        with capture_logs() as logs:
            run_all(runner)

        assert "task_lost" in [line["event"] for line in logs]
        assert chapter_of(repo, first).state == "downloading"
        assert chapter_of(repo, second).state == "downloaded"


class TestDownload__HostPoliteness:
    def test_download__waits_the_larger_of_request_delay_and_the_hosts_delay(
        self, repo: Repo, novel_id: str, adapter: FakeAdapter, runner: Runner,
        stop: RecordingStop,
    ) -> None:  # fmt: skip
        queue(repo, novel_id, 3)
        adapter.delays[HOST] = 10.0

        run_all(runner)

        assert stop.waits == [10.0, 10.0]

    def test_download__keeps_request_delay_when_the_hosts_delay_is_smaller(
        self, repo: Repo, novel_id: str, adapter: FakeAdapter, runner: Runner,
        stop: RecordingStop,
    ) -> None:  # fmt: skip
        queue(repo, novel_id, 2)
        adapter.delays[HOST] = DELAY / 2

        run_all(runner)

        assert stop.waits == [DELAY]


class TestRunner__Politeness:
    def test_run_task__a_download_waits_until_the_hosts_previous_fetch_plus_the_delay(
        self, repo: Repo, novel_id: str, runner: Runner, stop: RecordingStop, clock: FakeClock
    ) -> None:
        queue(repo, novel_id, 2)
        runner.step()
        clock.advance(seconds=0.1)

        runner.step()

        assert stop.waits == [pytest.approx(DELAY - 0.1)]

    def test_run_task__a_download_does_not_wait_once_the_delay_has_passed(
        self, repo: Repo, novel_id: str, runner: Runner, stop: RecordingStop, clock: FakeClock
    ) -> None:
        queue(repo, novel_id, 2)
        runner.step()
        clock.advance(seconds=DELAY)

        runner.step()

        assert stop.waits == []

    def test_run_task__another_hosts_download_does_not_wait(
        self, repo: Repo, novel_id: str, runner: Runner, stop: RecordingStop, adapter: FakeAdapter
    ) -> None:
        queue(repo, novel_id, 1)
        other = repo.novels.create(OTHER_URL, followed=True).id
        claim_task(repo, other, "worker-parked", 86400)
        add_chapter(repo, other, 2, url=f"{OTHER_URL}/2")

        assert run_all(runner) == 2

        assert adapter.fetched == [f"{URL}/1", f"{OTHER_URL}/2"]
        assert stop.waits == []

    def test_run_task__a_refresh_shares_the_hosts_slots_with_downloads(
        self, repo: Repo, novel_id: str, runner: Runner, stop: RecordingStop, adapter: FakeAdapter
    ) -> None:
        queue(repo, novel_id, 1)
        runner.step()
        repo.novels.request_sync(novel_id)
        refresh = live_task(repo, novel_id)
        claimed = claim_task(repo, novel_id, ME)
        assert claimed.id == refresh.id

        runner.run_task(claimed)

        assert stop.waits == [DELAY]
        assert live_task(repo, novel_id, TaskState.QUEUED).id != refresh.id

    def test_run_task__a_paused_host_reschedules_the_task_to_reach_after(
        self, repo: Repo, novel_id: str, runner: Runner, stop: RecordingStop,
        adapter: FakeAdapter, clock: FakeClock,
    ) -> None:  # fmt: skip
        (chapter_id,) = queue(repo, novel_id, 1)
        paused = clock() + timedelta(minutes=10)
        with db.write(repo.engine) as conn:
            conn.execute(update(hosts).where(hosts.c.host == HOST).values(reach_after=paused))

        with capture_logs() as logs:
            assert runner.step() is True

        assert adapter.fetched == []
        assert stop.waits == []
        task = download_task(repo, chapter_id)
        assert (task.state, task.run_after, task.claimed_by, task.attempts) == (
            TaskState.QUEUED,
            paused,
            None,
            0,
        )
        rescheduled = [e for e in logs if e["event"] == "task_rescheduled"]
        assert [e["reach_after"] for e in rescheduled] == [paused]

    def test_run_task__stop_during_the_wait_releases_the_task_unstarted(
        self, repo: Repo, novel_id: str, adapter: FakeAdapter, runner: Runner,
        stop: RecordingStop, monkeypatch: pytest.MonkeyPatch,
    ) -> None:  # fmt: skip
        _, second = queue(repo, novel_id, 2)
        runner.step()
        monkeypatch.setattr(stop, "_stop_after", 1)

        assert runner.step() is True

        assert adapter.fetched == [f"{URL}/1"]
        task = download_task(repo, second)
        assert (task.state, task.claimed_by, task.attempts) == (TaskState.QUEUED, None, 0)
        assert chapter_of(repo, second).state == "pending"
