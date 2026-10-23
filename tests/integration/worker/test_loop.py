"""The worker main loop (spec section 6.1)."""

import json
import threading
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from novelhog import log
from novelhog.config import Settings
from novelhog.notifications.channels import Delivered
from novelhog.repo import Repo, SchemaOutdated, db
from novelhog.repo import events as event_log
from novelhog.scraper_adapter.errors import InfrastructureError
from novelhog.storage import LocalStaticStorage
from novelhog.worker import runtime
from novelhog.worker.loop import Worker
from tests.conftest import FakeClock
from tests.integration.worker.conftest import (
    HOST,
    IDLE,
    FakeAdapter,
    FakeFetcher,
    RecordingStop,
    add_chapter,
    chapter_of,
)


def make_worker(
    repo: Repo,
    adapter: FakeAdapter,
    fetcher: FakeFetcher,
    settings: Settings,
    tmp_path: Path,
    stop: threading.Event | None = None,
) -> Worker:
    storage = LocalStaticStorage(tmp_path / "static", "/static")
    return Worker(repo, adapter, fetcher, storage, {}, settings, stop=stop)


@pytest.fixture
def worker(
    repo: Repo, adapter: FakeAdapter, fetcher: FakeFetcher, settings: Settings, tmp_path: Path
) -> Worker:
    return make_worker(repo, adapter, fetcher, settings, tmp_path, RecordingStop())


class TestStep__OrderOfWork:
    def test_step__recovers_expired_claims_first(
        self, repo: Repo, novel_id: str, worker: Worker, clock: FakeClock
    ) -> None:
        repo.novels.claim_due_refresh("other", 10)  # keeps the refresh out of the way
        chapter_id = add_chapter(
            repo,
            novel_id,
            1,
            state="downloading",
            claimed_by="other",
            claimed_until=clock() - timedelta(seconds=1),
        )

        assert worker.step() == "download"
        assert chapter_of(repo, chapter_id).state == "downloaded"

    def test_step__prefers_a_due_refresh_over_a_download(
        self, repo: Repo, novel_id: str, worker: Worker
    ) -> None:
        chapter_id = add_chapter(repo, novel_id, 1)

        assert worker.step() == "refresh"
        assert chapter_of(repo, chapter_id).state == "pending"
        assert worker.step() == "download"
        assert chapter_of(repo, chapter_id).state == "downloaded"

    def test_step__returns_build_when_an_export_was_built(
        self, novel_id: str, worker: Worker, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(Worker, "_build_export", lambda self: True)

        assert worker.step() == "refresh"
        assert worker.step() == "build"

    def test_step__runs_notification_dispatch_before_the_work(
        self, novel_id: str, worker: Worker, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[str] = []
        monkeypatch.setattr(Worker, "_dispatch_notifications", lambda self: calls.append("d"))

        worker.step()

        assert calls == ["d"]


class TestStep__WhenThereIsNoWork:
    def test_step__returns_idle(self, repo: Repo, worker: Worker) -> None:
        assert worker.step() == "idle"


class TestRun__OnStart:
    def test_run__raises_schema_outdated_when_the_schema_is_not_at_head(
        self, empty_database_url: str, adapter: FakeAdapter, fetcher: FakeFetcher,
        settings: Settings, tmp_path: Path,
    ) -> None:  # fmt: skip
        outdated = Repo(empty_database_url)
        try:
            with pytest.raises(SchemaOutdated):
                make_worker(outdated, adapter, fetcher, settings, tmp_path).run()
        finally:
            outdated.engine.dispose()

    def test_run__configures_the_runtime_once(
        self, repo: Repo, worker: Worker, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[Settings] = []
        monkeypatch.setattr(runtime, "configure_runtime", seen.append)
        worker.request_stop()

        worker.run()

        assert seen == [settings]

    def test_run__publishes_hosts_and_recovers_once(
        self, repo: Repo, worker: Worker, clock: FakeClock
    ) -> None:
        worker.request_stop()

        worker.run()

        assert [h.host for h in repo.hosts.list_supported()] == [HOST]


class TestRun__WhenIdle:
    def test_run__waits_idle_sleep_through_stop_wait_and_ends_when_it_wakes(
        self, repo: Repo, adapter: FakeAdapter, fetcher: FakeFetcher, settings: Settings,
        tmp_path: Path,
    ) -> None:  # fmt: skip
        stop = RecordingStop(stop_after_waits=2)

        make_worker(repo, adapter, fetcher, settings, tmp_path, stop).run()

        assert stop.waits == [IDLE, IDLE]

    def test_run__downloads_pending_work_before_waiting(
        self, repo: Repo, novel_id: str, adapter: FakeAdapter, fetcher: FakeFetcher,
        settings: Settings, tmp_path: Path,
    ) -> None:  # fmt: skip
        chapter_id = add_chapter(repo, novel_id, 1)
        stop = RecordingStop(stop_after_waits=1)

        make_worker(repo, adapter, fetcher, settings, tmp_path, stop).run()

        assert chapter_of(repo, chapter_id).state == "downloaded"


class TestRun__WhenAStepRaises:
    def test_run__logs_and_backs_off_5_10_20_capped_at_idle_sleep(
        self, repo: Repo, adapter: FakeAdapter, fetcher: FakeFetcher, tmp_path: Path,
        settings: Settings, monkeypatch: pytest.MonkeyPatch,
    ) -> None:  # fmt: skip
        stop = RecordingStop(stop_after_waits=5)
        small = Settings(**{**settings.__dict__, "idle_sleep": 12})
        worker = make_worker(repo, adapter, fetcher, small, tmp_path, stop)

        def boom(self: Worker) -> Any:
            msg = "database busy"
            raise RuntimeError(msg)

        monkeypatch.setattr(Worker, "step", boom)

        worker.run()

        assert stop.waits == [5, 10, 12, 12, 12]

    def test_run__survives_an_infrastructure_error_from_a_refresh(
        self, repo: Repo, novel_id: str, adapter: FakeAdapter, fetcher: FakeFetcher,
        settings: Settings, tmp_path: Path,
    ) -> None:  # fmt: skip
        adapter.toc_error = InfrastructureError("dns")
        stop = RecordingStop(stop_after_waits=2)

        make_worker(repo, adapter, fetcher, settings, tmp_path, stop).run()

        assert stop.waits == [5, IDLE]


class TestRun__AfterRequestStop:
    def test_run__returns_after_the_current_step(
        self, repo: Repo, novel_id: str, adapter: FakeAdapter, fetcher: FakeFetcher,
        settings: Settings, tmp_path: Path,
    ) -> None:  # fmt: skip
        first = add_chapter(repo, novel_id, 1)
        second = add_chapter(repo, novel_id, 2)
        worker = make_worker(repo, adapter, fetcher, settings, tmp_path)
        adapter.hook = lambda url: worker.request_stop()
        repo.novels.claim_due_refresh("other", 10)  # keeps the refresh out of the way

        worker.run()

        assert chapter_of(repo, first).state == "downloaded"
        assert chapter_of(repo, second).state == "pending"


def _log_lines(capsys: pytest.CaptureFixture[str]) -> list[dict[str, Any]]:
    out = capsys.readouterr().out
    return [json.loads(line) for line in out.splitlines() if line.startswith("{")]


class _Channel:
    name = "webpush"

    def __init__(self) -> None:
        self.sent: list[str] = []

    def send(self, subscription: Any, message: Any) -> Any:
        self.sent.append(message.body)
        return Delivered()


def _announce(repo: Repo, novel_id: str) -> None:
    repo.subscriptions.upsert("webpush", {"endpoint": "e"}, "e", None)
    with db.write(repo.engine) as conn:
        event_log.append(
            conn, novel_id, novel_id, {"followed": True, "released": [1]}, now=repo.clock()
        )


class TestStep__Notifications:
    def test_step__dispatches_new_events_to_the_channels_every_iteration(
        self,
        repo: Repo,
        novel_id: str,
        adapter: FakeAdapter,
        fetcher: FakeFetcher,
        settings: Settings,
        tmp_path: Path,
    ) -> None:
        repo.consumers.claim("notifier", "setup", 60)
        repo.consumers.advance("notifier", "setup", repo.events.since(0)[-1].seq)
        channel = _Channel()
        storage = LocalStaticStorage(tmp_path / "static", "/static")
        worker = Worker(
            repo,
            adapter,
            fetcher,
            storage,
            lambda: {"webpush": channel},
            settings,
            stop=RecordingStop(),
        )
        _announce(repo, novel_id)

        worker.step()

        assert channel.sent == ["1 new chapter (1\u20131)"]

    def test_step__survives_a_failing_dispatch_and_still_does_the_work(
        self,
        repo: Repo,
        novel_id: str,
        adapter: FakeAdapter,
        fetcher: FakeFetcher,
        settings: Settings,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        log.setup("worker")

        def broken() -> dict[str, Any]:
            msg = "registry down"
            raise RuntimeError(msg)

        storage = LocalStaticStorage(tmp_path / "static", "/static")
        worker = Worker(repo, adapter, fetcher, storage, broken, settings, stop=RecordingStop())

        assert worker.step() == "refresh"
        assert any(line["event"] == "notification_dispatch_failed" for line in _log_lines(capsys))

    def test_step__dispatches_before_refreshing(
        self,
        repo: Repo,
        adapter: FakeAdapter,
        fetcher: FakeFetcher,
        settings: Settings,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        calls: list[str] = []
        worker = make_worker(repo, adapter, fetcher, settings, tmp_path, RecordingStop())
        monkeypatch.setattr(worker, "_dispatch_notifications", lambda: calls.append("dispatch"))

        def refresh_due() -> bool:
            calls.append("refresh")
            return True

        monkeypatch.setattr(worker, "_refresh_due", refresh_due)

        worker.step()

        assert calls == ["dispatch", "refresh"]
