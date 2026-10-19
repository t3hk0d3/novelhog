"""Host publishing and the TOC refresh (spec sections 6.1 to 6.3)."""

import random
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
import structlog
from sqlalchemy import select, update

from novelhog import config
from novelhog.config import Settings
from novelhog.images import FetchedImage
from novelhog.repo import Repo, db
from novelhog.repo.novels import NovelClaim
from novelhog.repo.schema import chapters, novels
from novelhog.scraper_adapter.adapter import TocResult
from novelhog.scraper_adapter.errors import InfrastructureError, SiteError
from novelhog.storage import LocalStaticStorage
from novelhog.worker.refresh import publish_hosts, refresh
from tests.conftest import FakeClock

HOST = "example.com"
URL = "https://example.com/novel"
COVER_URL = "https://example.com/cover.png"
PNG = b"\x89PNG fake"
MIN = 21600
MAX = 2592000
ME = "worker-a"


def u(n: int) -> str:
    return f"{URL}/{n}"


class FakeAdapter:
    """A `TocAdapter` that returns a prepared TOC, raises a prepared error or runs a hook."""

    def __init__(self, hosts: list[str] | None = None) -> None:
        self.hosts = hosts or [HOST]
        self.urls: list[str] = [u(1), u(2)]
        self.title: str | None = "Lantern"
        self.cover_url: str | None = COVER_URL
        self.error: Exception | None = None
        self.hook: Any = None
        self.calls: list[tuple[str, str, str]] = []

    def supported_hosts(self) -> list[str]:
        return self.hosts

    def sync_toc(self, novel_id: str, host: str, toc_url: str) -> TocResult:
        self.calls.append((novel_id, host, toc_url))
        if self.hook is not None:
            self.hook()
        if self.error is not None:
            raise self.error
        return TocResult(self.title, "en", self.cover_url, list(self.urls), "<html></html>")


class FakeFetcher:
    def __init__(self) -> None:
        self.image: FetchedImage | None = FetchedImage(PNG, "image/png")
        self.urls: list[str] = []

    def fetch(self, url: str) -> FetchedImage | None:
        self.urls.append(url)
        return self.image


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return config.load(
        {
            "NOVELHOG_DATA_DIR": str(tmp_path),
            "NOVELHOG_WORKER_ID": ME,
            "NOVELHOG_CLAIM_TTL": "300",
            "NOVELHOG_REFRESH_MIN_INTERVAL": str(MIN),
            "NOVELHOG_REFRESH_MAX_INTERVAL": str(MAX),
        }
    )


@pytest.fixture
def adapter() -> FakeAdapter:
    return FakeAdapter()


@pytest.fixture
def fetcher() -> FakeFetcher:
    return FakeFetcher()


@pytest.fixture
def files(tmp_path: Path) -> LocalStaticStorage:
    return LocalStaticStorage(tmp_path / "static", "/static")


@pytest.fixture
def novel_id(repo: Repo, adapter: FakeAdapter) -> str:
    publish_hosts(repo, adapter)
    return repo.novels.create(URL, followed=True).id


class Harness:
    """Claims the due novel and runs one refresh with the fakes."""

    def __init__(
        self,
        repo: Repo,
        adapter: FakeAdapter,
        fetcher: FakeFetcher,
        files: LocalStaticStorage,
        settings: Settings,
    ) -> None:
        self.repo, self.adapter, self.fetcher = repo, adapter, fetcher
        self.files, self.settings = files, settings

    def claim(self) -> NovelClaim:
        claim = self.repo.novels.claim_due_refresh(ME, self.settings.claim_ttl)
        assert claim is not None
        return claim

    def run(self, claim: NovelClaim | None = None) -> None:
        refresh(
            self.repo,
            self.adapter,
            self.fetcher,
            self.files,
            claim or self.claim(),
            settings=self.settings,
            rng=random.Random(7),  # noqa: S311
        )

    def novel(self, novel_id: str) -> Any:
        with db.read(self.repo.engine) as conn:
            return conn.execute(select(novels).where(novels.c.id == novel_id)).one()

    def chapters(self, novel_id: str) -> list[Any]:
        with db.read(self.repo.engine) as conn:
            statement = select(chapters).where(chapters.c.novel_id == novel_id)
            return list(conn.execute(statement.order_by(chapters.c.url)).all())

    def last_nv(self, novel_id: str) -> dict[str, object]:
        rows = [e for e in self.repo.events.since(0, novel_id=novel_id) if e.entity_id == novel_id]
        return rows[-1].payload


@pytest.fixture
def run(
    repo: Repo,
    adapter: FakeAdapter,
    fetcher: FakeFetcher,
    files: LocalStaticStorage,
    settings: Settings,
) -> Harness:
    return Harness(repo, adapter, fetcher, files, settings)


def do_initial(run: Harness, novel_id: str, clock: FakeClock) -> None:
    run.run()
    clock.advance(days=40)


class TestPublishHosts:
    def test_publish_hosts__publishes_the_adapters_hosts(self, repo: Repo) -> None:
        publish_hosts(repo, FakeAdapter(["a.com", "b.com"]))

        assert [h.host for h in repo.hosts.list_supported()] == ["a.com", "b.com"]

    def test_publish_hosts__marks_a_vanished_host_unsupported_and_keeps_its_novels(
        self, repo: Repo
    ) -> None:
        publish_hosts(repo, FakeAdapter([HOST, "b.com"]))
        novel = repo.novels.create(URL, followed=True)

        publish_hosts(repo, FakeAdapter(["b.com"]))

        assert [h.host for h in repo.hosts.list_supported()] == ["b.com"]
        assert repo.novels.get(novel.id).host == HOST


class TestRefresh__OnInitialImport:
    def test_refresh__sets_title_and_inserts_chapters_not_requested_without_released_at(
        self, repo: Repo, novel_id: str, run: Harness
    ) -> None:
        run.run()

        assert run.novel(novel_id).title == "Lantern"
        rows = run.chapters(novel_id)
        assert [(c.state, c.released_at) for c in rows] == [("not_requested", None)] * 2
        assert run.adapter.calls == [(novel_id, HOST, URL)]

    def test_refresh__writes_released_empty_and_stores_the_cover(
        self, repo: Repo, novel_id: str, run: Harness
    ) -> None:
        run.run()

        assert run.last_nv(novel_id)["released"] == []
        key = run.novel(novel_id).cover_key
        assert key.startswith(f"covers/{novel_id}/")
        assert key.endswith(".png")
        assert run.files.get(key) == PNG
        assert run.fetcher.urls == [COVER_URL]

    def test_refresh__does_not_fetch_the_cover_again_when_the_novel_has_one(
        self, repo: Repo, novel_id: str, run: Harness
    ) -> None:
        with db.write(repo.engine) as conn:
            conn.execute(update(novels).values(cover_key="covers/x/old.png"))

        run.run()

        assert run.fetcher.urls == []
        assert run.novel(novel_id).cover_key == "covers/x/old.png"

    def test_refresh__keeps_an_existing_title(
        self, repo: Repo, novel_id: str, run: Harness
    ) -> None:
        repo.novels.update(novel_id, title="Mine")

        run.run()

        assert run.novel(novel_id).title == "Mine"

    def test_refresh__treats_a_missing_title_and_cover_as_no_metadata(
        self, repo: Repo, novel_id: str, run: Harness
    ) -> None:
        run.adapter.title = None
        run.adapter.cover_url = None

        run.run()

        row = run.novel(novel_id)
        assert (row.title, row.cover_key, row.import_error) == (None, None, None)
        assert run.fetcher.urls == []
        assert row.last_refreshed_at is not None


class TestRefresh__OnLaterSync:
    def test_refresh__inserts_new_chapters_pending_when_followed_and_resets_interval(
        self, repo: Repo, novel_id: str, run: Harness, clock: FakeClock
    ) -> None:
        do_initial(run, novel_id, clock)
        with db.write(repo.engine) as conn:
            conn.execute(update(novels).values(refresh_interval=MIN * 8))
        run.adapter.urls = [u(1), u(2), u(3)]

        run.run()

        new = run.chapters(novel_id)[-1]
        assert (new.state, new.priority, new.released_at) == ("pending", True, clock())
        assert run.novel(novel_id).refresh_interval == MIN
        assert run.last_nv(novel_id)["released"] == [3]

    def test_refresh__inserts_not_requested_when_not_followed(
        self, repo: Repo, novel_id: str, run: Harness, clock: FakeClock
    ) -> None:
        repo.novels.update(novel_id, followed=False)
        do_initial(run, novel_id, clock)
        run.adapter.urls = [u(1), u(2), u(3)]

        run.run()

        assert run.chapters(novel_id)[-1].state == "not_requested"

    def test_refresh__doubles_the_interval_when_nothing_is_new(
        self, repo: Repo, novel_id: str, run: Harness, clock: FakeClock
    ) -> None:
        do_initial(run, novel_id, clock)
        interval = run.novel(novel_id).refresh_interval
        assert interval == MIN * 2

        run.run()

        assert run.novel(novel_id).refresh_interval == MIN * 4
        assert run.last_nv(novel_id)["released"] == []

    def test_refresh__caps_the_interval_at_the_maximum(
        self, repo: Repo, novel_id: str, run: Harness, clock: FakeClock
    ) -> None:
        do_initial(run, novel_id, clock)
        with db.write(repo.engine) as conn:
            conn.execute(update(novels).values(refresh_interval=MAX - 5))

        run.run()

        assert run.novel(novel_id).refresh_interval == MAX

    def test_refresh__logs_a_warning_listing_chapters_that_left_the_toc(
        self, repo: Repo, novel_id: str, run: Harness, clock: FakeClock
    ) -> None:
        do_initial(run, novel_id, clock)
        gone = run.chapters(novel_id)[1]
        run.adapter.urls = [u(1)]

        with structlog.testing.capture_logs() as logs:
            run.run()

        warning = next(e for e in logs if e["event"] == "toc_chapters_removed")
        assert warning["log_level"] == "warning"
        assert warning["chapter_ids"] == [gone.id]
        kept = run.chapters(novel_id)[1]
        assert (kept.position, kept.state) == (None, gone.state)

    def test_refresh__logs_a_warning_for_dequeued_chapters(
        self, repo: Repo, novel_id: str, run: Harness, clock: FakeClock
    ) -> None:
        do_initial(run, novel_id, clock)
        run.adapter.urls = [u(1), u(2), u(3)]
        run.run()
        clock.advance(days=40)
        run.adapter.urls = [u(1), u(2)]

        with structlog.testing.capture_logs() as logs:
            run.run()

        assert any(e["event"] == "toc_chapters_dequeued" for e in logs)
        assert run.chapters(novel_id)[2].state == "not_requested"

    def test_refresh__treats_a_toc_with_fewer_than_half_of_20_chapters_as_a_site_error(
        self, repo: Repo, novel_id: str, run: Harness, clock: FakeClock
    ) -> None:
        run.adapter.urls = [u(n) for n in range(1, 21)]
        do_initial(run, novel_id, clock)
        run.adapter.urls = [u(n) for n in range(1, 10)]

        run.run()

        assert [c.position for c in run.chapters(novel_id)].count(None) == 0
        row = run.novel(novel_id)
        assert row.import_error is None
        assert row.refresh_interval == MIN * 4

    def test_refresh__accepts_a_toc_of_exactly_half_of_20_chapters(
        self, repo: Repo, novel_id: str, run: Harness, clock: FakeClock
    ) -> None:
        run.adapter.urls = [u(n) for n in range(1, 21)]
        do_initial(run, novel_id, clock)
        run.adapter.urls = [u(n) for n in range(1, 11)]

        run.run()

        assert [c.position for c in run.chapters(novel_id)].count(None) == 10

    def test_refresh__accepts_a_big_drop_below_20_known_chapters(
        self, repo: Repo, novel_id: str, run: Harness, clock: FakeClock
    ) -> None:
        run.adapter.urls = [u(n) for n in range(1, 11)]
        do_initial(run, novel_id, clock)
        run.adapter.urls = [u(1)]

        run.run()

        assert [c.position for c in run.chapters(novel_id)].count(None) == 9


class TestRefresh__OnSiteError:
    def test_refresh__sets_import_error_and_doubles_the_interval_on_the_initial_import(
        self, repo: Repo, novel_id: str, run: Harness
    ) -> None:
        run.adapter.error = SiteError("HTTP 503 from the site")

        run.run()

        row = run.novel(novel_id)
        assert row.import_error == "SiteError: HTTP 503 from the site"
        assert row.refresh_interval == MIN * 2
        assert row.last_refreshed_at is None
        assert row.claimed_by is None
        wait = (row.next_refresh_at - repo.clock()).total_seconds()
        assert MIN * 2 * 0.9 <= wait <= MIN * 2 * 1.1
        assert run.last_nv(novel_id)["released"] == []

    def test_refresh__fails_when_the_host_is_no_longer_supported(
        self, repo: Repo, novel_id: str, run: Harness
    ) -> None:
        publish_hosts(repo, FakeAdapter(["other.com"]))

        run.run()

        assert run.novel(novel_id).import_error == (
            "SiteError: Host no longer supported by the scraper"
        )
        assert run.adapter.calls == []

    def test_refresh__keeps_import_error_null_and_backs_off_on_a_later_sync(
        self, repo: Repo, novel_id: str, run: Harness, clock: FakeClock
    ) -> None:
        do_initial(run, novel_id, clock)
        run.adapter.error = SiteError("boom")

        run.run()

        row = run.novel(novel_id)
        assert row.import_error is None
        assert row.refresh_interval == MIN * 4
        assert len(run.chapters(novel_id)) == 2

    def test_refresh__records_unexpected_errors_as_site_errors(
        self, repo: Repo, novel_id: str, run: Harness
    ) -> None:
        run.adapter.error = ValueError("odd")

        run.run()

        assert run.novel(novel_id).import_error == "ValueError: odd"


class TestRefresh__OnInfrastructureError:
    def test_refresh__keeps_the_interval_schedules_min_and_reraises(
        self, repo: Repo, novel_id: str, run: Harness, clock: FakeClock
    ) -> None:
        do_initial(run, novel_id, clock)
        run.adapter.error = InfrastructureError("connection refused")
        before = run.novel(novel_id).refresh_interval

        with pytest.raises(InfrastructureError):
            run.run()

        row = run.novel(novel_id)
        assert row.refresh_interval == before
        assert row.next_refresh_at == clock() + timedelta(seconds=MIN)
        assert row.claimed_by is None
        assert row.import_error is None

    def test_refresh__does_not_set_import_error_on_the_initial_import(
        self, repo: Repo, novel_id: str, run: Harness
    ) -> None:
        run.adapter.error = InfrastructureError("connection refused")

        with pytest.raises(InfrastructureError):
            run.run()

        assert run.novel(novel_id).import_error is None
        assert run.novel(novel_id).refresh_interval == MIN


class TestRefresh__OnCoverFailure:
    def test_refresh__logs_and_continues_when_the_fetch_fails(
        self, repo: Repo, novel_id: str, run: Harness
    ) -> None:
        run.fetcher.image = None

        with structlog.testing.capture_logs() as logs:
            run.run()

        assert any(e["event"] == "cover_fetch_failed" for e in logs)
        row = run.novel(novel_id)
        assert row.cover_key is None
        assert row.last_refreshed_at is not None
        assert len(run.chapters(novel_id)) == 2


class TestRefresh__OnRequestedSync:
    def test_refresh__is_due_immediately_when_a_sync_arrived_during_the_sync(
        self, repo: Repo, novel_id: str, run: Harness
    ) -> None:
        run.adapter.hook = lambda: repo.novels.request_sync(novel_id)

        run.run()

        row = run.novel(novel_id)
        assert row.next_refresh_at == repo.clock()
        assert row.sync_requested is False


class TestRefresh__OnLostClaim:
    def test_refresh__discards_the_result_and_logs_it(
        self, repo: Repo, novel_id: str, run: Harness, clock: FakeClock
    ) -> None:
        run.adapter.hook = lambda: clock.advance(seconds=run.settings.claim_ttl + 1)

        with structlog.testing.capture_logs() as logs:
            run.run()

        assert any(e["event"] == "refresh_claim_lost" for e in logs)
        assert run.chapters(novel_id) == []
        assert run.novel(novel_id).last_refreshed_at is None


class TestRefresh__Heartbeat:
    def test_refresh__renews_the_claim_while_sync_toc_runs(
        self,
        repo: Repo,
        novel_id: str,
        adapter: FakeAdapter,
        fetcher: FakeFetcher,
        files: LocalStaticStorage,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        short = config.load(
            {
                "NOVELHOG_DATA_DIR": str(tmp_path),
                "NOVELHOG_WORKER_ID": ME,
                "NOVELHOG_CLAIM_TTL": "1",
            }
        )
        renewals: list[bool] = []
        original = repo.novels.renew_claim

        def spy(nid: str, worker: str, ttl: int) -> bool:
            renewals.append(original(nid, worker, ttl))
            return renewals[-1]

        monkeypatch.setattr(repo.novels, "renew_claim", spy)
        adapter.hook = lambda: time.sleep(1.0)
        harness = Harness(repo, adapter, fetcher, files, short)

        harness.run()

        assert renewals
        assert all(renewals)
        assert harness.novel(novel_id).last_refreshed_at is not None
