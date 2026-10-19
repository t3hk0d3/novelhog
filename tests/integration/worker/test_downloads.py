"""The per-chapter download pipeline (spec section 6.4)."""

import threading
from collections.abc import Callable
from typing import Any

import pytest
from sqlalchemy import update

from novelhog import html_to_markdown
from novelhog.config import Settings
from novelhog.repo import Repo, db
from novelhog.repo.schema import chapters
from novelhog.repo.work import ChapterClaim
from novelhog.scraper_adapter.errors import InfrastructureError, SiteError
from novelhog.worker import downloads
from novelhog.worker.downloads import BatchReport, download
from tests.integration.worker.conftest import (
    DELAY,
    ME,
    URL,
    FakeAdapter,
    FakeFetcher,
    RecordingStop,
    add_chapter,
    chapter_of,
)

TTL = 300


class StubHeartbeat:
    """Replaces `Heartbeat`: captures `renew` and lets a test set `lost`."""

    instances: list[StubHeartbeat] = []
    lost_from_start = False

    def __init__(self, renew: Callable[[], bool], interval_s: float) -> None:
        self.renew = renew
        self.interval_s = interval_s
        self.lost = StubHeartbeat.lost_from_start
        StubHeartbeat.instances.append(self)

    def __enter__(self) -> StubHeartbeat:
        return self

    def __exit__(self, *_: object) -> None:
        return None


@pytest.fixture
def heartbeat(monkeypatch: pytest.MonkeyPatch) -> type[StubHeartbeat]:
    StubHeartbeat.instances = []
    StubHeartbeat.lost_from_start = False
    monkeypatch.setattr(downloads, "Heartbeat", StubHeartbeat)
    return StubHeartbeat


def claim(repo: Repo, novel_id: str, count: int) -> list[ChapterClaim]:
    ids = [add_chapter(repo, novel_id, at) for at in range(1, count + 1)]
    batch = repo.work.claim_batch(ME, 10, TTL)
    assert [c.chapter_id for c in batch] == ids
    return batch


def run(
    repo: Repo,
    adapter: FakeAdapter,
    fetcher: FakeFetcher,
    batch: list[ChapterClaim],
    settings: Settings,
    stop: threading.Event,
) -> BatchReport:
    return download(repo, adapter, fetcher, batch, settings=settings, stop=stop)


class TestDownload__ForEachClaimedChapter:
    def test_download__completes_with_title_and_has_images(
        self, repo: Repo, novel_id: str, adapter: FakeAdapter, fetcher: FakeFetcher,
        settings: Settings,
    ) -> None:  # fmt: skip
        batch = claim(repo, novel_id, 1)
        adapter.titles[f"{URL}/1"] = "The Start"
        adapter.bodies[f"{URL}/1"] = '<p>Hi</p><p><img src="https://example.com/a.png"></p>'

        report = run(repo, adapter, fetcher, batch, settings, RecordingStop())

        row = chapter_of(repo, batch[0].chapter_id)
        assert (row.state, row.title, row.has_images, row.claimed_by) == (
            "downloaded",
            "The Start",
            True,
            None,
        )
        assert report == BatchReport(downloaded=1, failed=0, skipped=0, released=0, lost=0)

    def test_download__stores_markdown_with_the_image_inlined(
        self, repo: Repo, novel_id: str, adapter: FakeAdapter, fetcher: FakeFetcher,
        settings: Settings,
    ) -> None:  # fmt: skip
        batch = claim(repo, novel_id, 1)
        adapter.bodies[f"{URL}/1"] = '<p>Hi</p><p><img src="https://example.com/a.png"></p>'

        run(repo, adapter, fetcher, batch, settings, RecordingStop())

        assert "data:image/png;base64," in repo.contents.get(batch[0].chapter_id)

    def test_download__titles_a_chapter_without_one_by_its_position(
        self, repo: Repo, novel_id: str, adapter: FakeAdapter, fetcher: FakeFetcher,
        settings: Settings,
    ) -> None:  # fmt: skip
        batch = claim(repo, novel_id, 1)
        adapter.titles[f"{URL}/1"] = None

        run(repo, adapter, fetcher, batch, settings, RecordingStop())

        assert chapter_of(repo, batch[0].chapter_id).title == "Chapter 1"

    def test_download__waits_request_delay_between_chapters_through_stop_wait(
        self, repo: Repo, novel_id: str, adapter: FakeAdapter, fetcher: FakeFetcher,
        settings: Settings,
    ) -> None:  # fmt: skip
        batch = claim(repo, novel_id, 3)
        stop = RecordingStop()

        run(repo, adapter, fetcher, batch, settings, stop)

        assert stop.waits == [DELAY, DELAY]

    def test_download__renews_the_remaining_claims_through_a_heartbeat(
        self, repo: Repo, novel_id: str, adapter: FakeAdapter, fetcher: FakeFetcher,
        settings: Settings, heartbeat: type[StubHeartbeat], clock: Any,
    ) -> None:  # fmt: skip
        batch = claim(repo, novel_id, 3)
        before = chapter_of(repo, batch[2].chapter_id).claimed_until
        renewed: list[bool] = []
        after: list[Any] = []

        def hook(url: str) -> None:
            if url == f"{URL}/2":
                clock.advance(seconds=100)
                renewed.append(heartbeat.instances[0].renew())
                after.append(chapter_of(repo, batch[2].chapter_id).claimed_until)

        adapter.hook = hook

        run(repo, adapter, fetcher, batch, settings, RecordingStop())

        assert renewed == [True]
        assert heartbeat.instances[0].interval_s == TTL / 3
        assert after[0] > before

    def test_download__heartbeat_reports_false_when_a_claim_is_gone(
        self, repo: Repo, novel_id: str, adapter: FakeAdapter, fetcher: FakeFetcher,
        settings: Settings, heartbeat: type[StubHeartbeat],
    ) -> None:  # fmt: skip
        batch = claim(repo, novel_id, 2)
        results: list[bool] = []

        def hook(url: str) -> None:
            with db.write(repo.engine) as conn:
                conn.execute(update(chapters).values(claimed_by="someone-else"))
            results.append(heartbeat.instances[0].renew())

        adapter.hook = hook

        run(repo, adapter, fetcher, batch, settings, RecordingStop())

        assert results[0] is False


class TestDownload__WhenDequeuedAfterTheClaim:
    def test_download__skips_the_chapter_and_goes_on(
        self, repo: Repo, novel_id: str, adapter: FakeAdapter, fetcher: FakeFetcher,
        settings: Settings,
    ) -> None:  # fmt: skip
        batch = claim(repo, novel_id, 2)
        with db.write(repo.engine) as conn:
            conn.execute(
                update(chapters)
                .where(chapters.c.id == batch[0].chapter_id)
                .values(state="not_requested", queued_at=None, claimed_by=None, claimed_until=None)
            )

        report = run(repo, adapter, fetcher, batch, settings, RecordingStop())

        assert adapter.fetched == [f"{URL}/2"]
        assert report == BatchReport(downloaded=1, failed=0, skipped=1, released=0, lost=0)


class TestDownload__WhenFetchingOrConvertingRaises:
    @pytest.mark.parametrize(
        ("error", "message"),
        [
            (SiteError("page gone"), "SiteError: page gone"),
            (ValueError("bad\nhtml"), "ValueError: bad html"),
        ],
    )
    def test_download__fails_the_chapter_with_type_and_summary_and_continues(
        self, repo: Repo, novel_id: str, adapter: FakeAdapter, fetcher: FakeFetcher,
        settings: Settings, error: Exception, message: str,
    ) -> None:  # fmt: skip
        batch = claim(repo, novel_id, 2)
        adapter.errors[f"{URL}/1"] = error

        report = run(repo, adapter, fetcher, batch, settings, RecordingStop())

        first = chapter_of(repo, batch[0].chapter_id)
        assert (first.state, repo.chapters.get(first.id).error, first.claimed_by) == (
            "error",
            message,
            None,
        )
        assert chapter_of(repo, batch[1].chapter_id).state == "downloaded"
        assert report.failed == 1
        assert report.downloaded == 1

    def test_download__fails_the_chapter_when_conversion_raises(
        self, repo: Repo, novel_id: str, adapter: FakeAdapter, fetcher: FakeFetcher,
        settings: Settings, monkeypatch: pytest.MonkeyPatch,
    ) -> None:  # fmt: skip
        batch = claim(repo, novel_id, 1)

        def boom(*_: Any) -> None:
            msg = "no parse"
            raise RuntimeError(msg)

        monkeypatch.setattr(html_to_markdown, "convert", boom)

        run(repo, adapter, fetcher, batch, settings, RecordingStop())

        assert repo.chapters.get(batch[0].chapter_id).error == "RuntimeError: no parse"


class TestDownload__WhenFetchingRaisesInfrastructureError:
    def test_download__requeues_the_chapter_releases_the_rest_and_reraises(
        self, repo: Repo, novel_id: str, adapter: FakeAdapter, fetcher: FakeFetcher,
        settings: Settings,
    ) -> None:  # fmt: skip
        batch = claim(repo, novel_id, 3)
        queued_at = chapter_of(repo, batch[0].chapter_id).queued_at
        adapter.errors[f"{URL}/1"] = InfrastructureError("connection refused")

        with pytest.raises(InfrastructureError):
            run(repo, adapter, fetcher, batch, settings, RecordingStop())

        for item in batch:
            row = chapter_of(repo, item.chapter_id)
            assert (row.state, row.claimed_by, row.queued_at) == ("pending", None, queued_at)
        assert adapter.fetched == [f"{URL}/1"]


class TestDownload__WhenStopIsSetMidBatch:
    def test_download__finishes_the_current_chapter_and_releases_the_rest(
        self, repo: Repo, novel_id: str, adapter: FakeAdapter, fetcher: FakeFetcher,
        settings: Settings,
    ) -> None:  # fmt: skip
        batch = claim(repo, novel_id, 3)
        queued_at = chapter_of(repo, batch[2].chapter_id).queued_at
        stop = threading.Event()
        adapter.hook = lambda url: stop.set()

        report = run(repo, adapter, fetcher, batch, settings, stop)

        assert chapter_of(repo, batch[0].chapter_id).state == "downloaded"
        for item in batch[1:]:
            row = chapter_of(repo, item.chapter_id)
            assert (row.state, row.claimed_by, row.queued_at) == ("pending", None, queued_at)
        assert report == BatchReport(downloaded=1, failed=0, skipped=0, released=2, lost=0)

    def test_download__releases_everything_when_the_heartbeat_lost_the_claims(
        self, repo: Repo, novel_id: str, adapter: FakeAdapter, fetcher: FakeFetcher,
        settings: Settings, heartbeat: type[StubHeartbeat],
    ) -> None:  # fmt: skip
        heartbeat.lost_from_start = True
        batch = claim(repo, novel_id, 2)

        report = run(repo, adapter, fetcher, batch, settings, RecordingStop())

        assert adapter.fetched == []
        assert report.released == 2


class TestDownload__WhenTheClaimWasLost:
    def test_download__counts_it_as_lost_and_continues(
        self, repo: Repo, novel_id: str, adapter: FakeAdapter, fetcher: FakeFetcher,
        settings: Settings,
    ) -> None:  # fmt: skip
        batch = claim(repo, novel_id, 2)

        def hook(url: str) -> None:
            if url == f"{URL}/1":
                with db.write(repo.engine) as conn:
                    conn.execute(
                        update(chapters)
                        .where(chapters.c.id == batch[0].chapter_id)
                        .values(claimed_by="someone-else")
                    )

        adapter.hook = hook

        report = run(repo, adapter, fetcher, batch, settings, RecordingStop())

        assert report == BatchReport(downloaded=1, failed=0, skipped=0, released=0, lost=1)
        assert chapter_of(repo, batch[0].chapter_id).state == "downloading"
