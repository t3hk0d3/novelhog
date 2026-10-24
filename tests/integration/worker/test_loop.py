"""The worker main loop (spec section 6.1)."""

import json
import threading
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from novelhog import log
from novelhog.config import Settings
from novelhog.notifications import dispatcher
from novelhog.notifications.channels import Delivered
from novelhog.repo import Repo, SchemaOutdated, db
from novelhog.repo import events as event_log
from novelhog.scraper_adapter.errors import InfrastructureError
from novelhog.storage import LocalStaticStorage
from novelhog.worker import runtime
from novelhog.worker.loop import DISPATCH_BUDGET_S, Worker
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
        repo.tasks.claim("other", 10)  # keeps the refresh out of the way
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

        assert worker.step() == "task"  # the refresh runs as a task
        assert chapter_of(repo, chapter_id).state == "pending"
        assert worker.step() == "download"
        assert chapter_of(repo, chapter_id).state == "downloaded"

    def test_step__returns_build_when_an_export_was_built(
        self, novel_id: str, worker: Worker, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(Worker, "_build_export", lambda self: True)

        assert worker.step() == "task"
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
        repo.tasks.claim("other", 10)  # keeps the refresh out of the way

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

        assert worker.step() == "task"
        assert any(line["event"] == "notification_dispatch_failed" for line in _log_lines(capsys))

    def test_step__dispatches_before_running_a_task(
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

        def run_task() -> bool:
            calls.append("task")
            return True

        monkeypatch.setattr(worker._runner, "step", run_task)  # noqa: SLF001

        worker.step()

        assert calls == ["dispatch", "task"]


FULL = dispatcher.DispatchReport(claimed=True, events=dispatcher.BATCH, advanced=True)
PARTIAL = dispatcher.DispatchReport(claimed=True, events=3, advanced=True)


class _Reports:
    """Stands in for `dispatcher.dispatch`: returns `reports` in turn, then empty ones."""

    def __init__(self, *reports: dispatcher.DispatchReport) -> None:
        self.reports = list(reports)
        self.calls = 0

    def __call__(self, *_args: object, **_kwargs: object) -> dispatcher.DispatchReport:
        self.calls += 1
        return self.reports.pop(0) if self.reports else dispatcher.DispatchReport(claimed=True)


class _Ticks:
    """A monotonic clock that moves `step` seconds every time it is read."""

    def __init__(self, step: float) -> None:
        self.now = 0.0
        self.step = step

    def __call__(self) -> float:
        self.now += self.step
        return self.now


def idle_worker(
    repo: Repo, adapter: FakeAdapter, fetcher: FakeFetcher, settings: Settings, tmp_path: Path,
    stop: threading.Event, monotonic: _Ticks,
) -> Worker:  # fmt: skip
    """A worker with no novel work, so a step only dispatches."""
    storage = LocalStaticStorage(tmp_path / "static", "/static")
    return Worker(repo, adapter, fetcher, storage, {}, settings, stop=stop, monotonic=monotonic)


class TestStep__Notifications__WhileFullBatchesComeBack:
    def test_step__keeps_dispatching_until_a_partial_batch(
        self, repo: Repo, adapter: FakeAdapter, fetcher: FakeFetcher, settings: Settings,
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:  # fmt: skip
        reports = _Reports(FULL, FULL, PARTIAL)
        monkeypatch.setattr(dispatcher, "dispatch", reports)
        worker = idle_worker(
            repo, adapter, fetcher, settings, tmp_path, RecordingStop(), _Ticks(0.1)
        )

        assert worker.step() == "idle"
        assert reports.calls == 3

    def test_step__stops_dispatching_when_the_time_budget_is_spent(
        self, repo: Repo, adapter: FakeAdapter, fetcher: FakeFetcher, settings: Settings,
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:  # fmt: skip
        reports = _Reports(*[FULL] * 10)
        monkeypatch.setattr(dispatcher, "dispatch", reports)
        ticks = _Ticks(DISPATCH_BUDGET_S / 4)
        worker = idle_worker(repo, adapter, fetcher, settings, tmp_path, RecordingStop(), ticks)

        worker.step()

        assert reports.calls == 4

    def test_step__stops_dispatching_when_a_stop_is_requested(
        self, repo: Repo, adapter: FakeAdapter, fetcher: FakeFetcher, settings: Settings,
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:  # fmt: skip
        reports = _Reports(*[FULL] * 10)
        monkeypatch.setattr(dispatcher, "dispatch", reports)
        stop = RecordingStop()
        stop.set()
        worker = idle_worker(repo, adapter, fetcher, settings, tmp_path, stop, _Ticks(0.1))

        worker.step()

        assert reports.calls == 1

    def test_step__stops_dispatching_when_the_claim_is_lost(
        self, repo: Repo, adapter: FakeAdapter, fetcher: FakeFetcher, settings: Settings,
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:  # fmt: skip
        lost = dispatcher.DispatchReport(claimed=True, events=dispatcher.BATCH, lost=True)
        reports = _Reports(lost, FULL)
        monkeypatch.setattr(dispatcher, "dispatch", reports)
        worker = idle_worker(
            repo, adapter, fetcher, settings, tmp_path, RecordingStop(), _Ticks(0.1)
        )

        assert worker.step() == "idle"
        assert reports.calls == 1


class TestStep__Notifications__WhenTheNotifierIsBehind:
    def test_step__returns_notify_instead_of_idle(
        self, repo: Repo, adapter: FakeAdapter, fetcher: FakeFetcher, settings: Settings,
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:  # fmt: skip
        monkeypatch.setattr(dispatcher, "dispatch", _Reports(*[FULL] * 10))
        ticks = _Ticks(DISPATCH_BUDGET_S)
        worker = idle_worker(repo, adapter, fetcher, settings, tmp_path, RecordingStop(), ticks)

        assert worker.step() == "notify"

    def test_run__does_not_idle_sleep(
        self, repo: Repo, adapter: FakeAdapter, fetcher: FakeFetcher, settings: Settings,
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:  # fmt: skip
        reports = _Reports(FULL, FULL)
        monkeypatch.setattr(dispatcher, "dispatch", reports)
        stop = RecordingStop(stop_after_waits=1)
        ticks = _Ticks(DISPATCH_BUDGET_S)
        worker = idle_worker(repo, adapter, fetcher, settings, tmp_path, stop, ticks)

        worker.run()

        assert reports.calls == 3
        assert stop.waits == [IDLE]
