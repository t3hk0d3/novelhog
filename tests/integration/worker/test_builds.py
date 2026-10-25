"""Export builds (spec section 6.5) as `export_build` tasks, run through the runner and the loop."""

import random
import threading
import zipfile
from collections.abc import Callable
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
import structlog.testing
from sqlalchemy import select, update

from novelhog import exporter
from novelhog.chapter_store import ChapterStore
from novelhog.config import Settings
from novelhog.domain.chapters import ChapterState
from novelhog.domain.exports import ExportState, Format
from novelhog.domain.payloads import ExportView
from novelhog.domain.tasks import Done, TaskState
from novelhog.exporter import BookInput
from novelhog.repo import Repo, db
from novelhog.repo import contents as contents_repo
from novelhog.repo.schema import chapters, exports, hosts, novels, tasks
from novelhog.repo.task_kinds.export_build import BuildResult
from novelhog.storage import LocalStaticStorage
from novelhog.worker import runtime
from novelhog.worker.loop import Worker
from novelhog.worker.runner import Runner
from novelhog.worker.tasks import default_processors
from novelhog.worker.tasks.base import HeartbeatFactory, TaskContext
from novelhog.worker.tasks.export_build import ExportBuildProcessor
from tests.integration.helpers import trash_keys
from tests.integration.task_fakes import live_task, start_build
from tests.integration.worker.conftest import (
    HOST,
    ME,
    URL,
    FakeAdapter,
    FakeFetcher,
    RecordingStop,
    add_chapter,
    chapter_of,
)

OTHER = "worker-b"
PARK_S = 86400


class HookedStorage(LocalStaticStorage):
    """Local storage that runs a hook after each `put_file`."""

    def __init__(self, root: Path) -> None:
        super().__init__(root, "/static")
        self.after_put: Callable[[], None] | None = None

    def put_file(self, key: str, path: Path, mime: str, download_name: str | None = None) -> None:
        super().put_file(key, path, mime, download_name)
        if self.after_put is not None:
            self.after_put()


class HookedBeat:
    """A heartbeat whose start runs `on_enter` (after the task started, before `run`)."""

    def __init__(self, on_enter: Callable[[], None], lost: bool = False) -> None:
        self._on_enter = on_enter
        self.lost = lost

    def __enter__(self) -> HookedBeat:
        self._on_enter()
        return self

    def __exit__(self, *_: object) -> None:
        return None


def hooked(on_enter: Callable[[], None], *, lost: bool = False) -> HeartbeatFactory:
    return lambda renew, interval_s: HookedBeat(on_enter, lost)


@pytest.fixture
def novel_id(repo: Repo) -> str:
    """A novel whose first refresh is claimed elsewhere, so only builds run here."""
    repo.hosts.publish([HOST])
    novel = repo.novels.create(URL, followed=True).id
    assert repo.tasks.claim(OTHER, PARK_S) is not None
    return novel


@pytest.fixture
def storage(tmp_path: Path) -> HookedStorage:
    return HookedStorage(tmp_path / "static")


@pytest.fixture
def tmp_dir(settings: Settings) -> Path:
    return settings.data_dir / "tmp"


@pytest.fixture
def ctx(repo: Repo, storage: HookedStorage, settings: Settings) -> TaskContext:
    return TaskContext(
        adapter=FakeAdapter(),
        fetcher=FakeFetcher(),
        storage=storage,
        chapter_store=ChapterStore(repo),
        settings=settings,
        stop=threading.Event(),
        hosts=repo.hosts,
        rng=random.Random(3),  # noqa: S311 - refresh jitter, not security
        heartbeat=runtime.Heartbeat,
        clock=repo.clock,
    )


@pytest.fixture
def make_runner(repo: Repo, ctx: TaskContext) -> Callable[..., Runner]:
    def make(heartbeat: HeartbeatFactory = runtime.Heartbeat) -> Runner:
        return Runner(repo, default_processors(), replace(ctx, heartbeat=heartbeat))

    return make


@pytest.fixture
def runner(make_runner: Callable[..., Runner]) -> Runner:
    return make_runner()


def add_downloaded(repo: Repo, novel: str, at: int, title: str | None = None) -> str:
    chapter_id = add_chapter(repo, novel, at, state="downloaded", title=title)
    with db.write(repo.engine) as conn:
        contents_repo.put(conn, chapter_id, f"Body of {at}", repo.clock())
    return chapter_id


def create_export(
    repo: Repo, novel: str, start: int, end: int, *, skip_missing: bool = False
) -> ExportView:
    view, _ = repo.exports.create(novel, start, end, Format.EPUB, skip_missing=skip_missing)
    return view


def run_build(runner: Runner) -> None:
    """Run the one claimable task, the build."""
    assert runner.step() is True


def export_of(repo: Repo, export_id: str) -> Any:
    with db.read(repo.engine) as conn:
        return conn.execute(select(exports).where(exports.c.id == export_id)).one()


def key_of(view: ExportView) -> str:
    return f"exports/{view.novel_id}/{view.id}/{view.filename}"


def epub_text(path: Path) -> str:
    with zipfile.ZipFile(path) as archive:
        return "".join(archive.read(n).decode() for n in archive.namelist() if n.endswith(".xhtml"))


def steal(repo: Repo, export_id: str) -> None:
    """Another worker takes over the running build's claim."""
    with db.write(repo.engine) as conn:
        conn.execute(
            update(tasks)
            .where(tasks.c.entity_id == export_id, tasks.c.state == TaskState.RUNNING.value)
            .values(claimed_by=OTHER)
        )


def remove_position(repo: Repo, chapter_id: str) -> None:
    with db.write(repo.engine) as conn:
        conn.execute(
            update(chapters)
            .where(chapters.c.id == chapter_id)
            .values(position=None, removed_at=repo.clock())
        )


class TestBuild__OnSuccess:
    def test_build__builds_from_stored_chapters_in_position_order(
        self, repo: Repo, novel_id: str, storage: HookedStorage, runner: Runner
    ) -> None:
        add_downloaded(repo, novel_id, 2, "Second")
        add_downloaded(repo, novel_id, 1, "First")
        view = create_export(repo, novel_id, 1, 2)

        run_build(runner)

        row = export_of(repo, view.id)
        text = epub_text(storage._path(row.storage_key))  # noqa: SLF001
        assert text.index("Body of 1") < text.index("Body of 2")
        assert "First" in text
        assert "Second" in text

    def test_build__falls_back_to_a_numbered_title(
        self, repo: Repo, novel_id: str, storage: HookedStorage, runner: Runner
    ) -> None:
        add_downloaded(repo, novel_id, 1)
        view = create_export(repo, novel_id, 1, 1)

        run_build(runner)

        row = export_of(repo, view.id)
        assert "Chapter 1" in epub_text(storage._path(row.storage_key))  # noqa: SLF001

    def test_build__passes_the_stored_cover_and_language_default(
        self,
        repo: Repo,
        novel_id: str,
        storage: HookedStorage,
        runner: Runner,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        add_downloaded(repo, novel_id, 1)
        storage.put("covers/x/abc.png", b"cover-bytes", "image/png")
        with db.write(repo.engine) as conn:
            conn.execute(update(novels).values(cover_key="covers/x/abc.png"))
        create_export(repo, novel_id, 1, 1)
        seen: list[BookInput] = []
        monkeypatch.setattr(exporter, "build", lambda fmt, book, out: seen.append(book))

        run_build(runner)

        assert seen[0].cover == (b"cover-bytes", "image/png")
        assert seen[0].language == "en"

    def test_build__missing_cover_builds_without_one_and_logs(
        self, repo: Repo, novel_id: str, runner: Runner
    ) -> None:
        add_downloaded(repo, novel_id, 1)
        with db.write(repo.engine) as conn:
            conn.execute(update(novels).values(cover_key="covers/x/gone.png"))
        view = create_export(repo, novel_id, 1, 1)

        with structlog.testing.capture_logs() as logs:
            run_build(runner)

        assert export_of(repo, view.id).state == ExportState.READY
        assert any(entry["event"] == "export_cover_missing" for entry in logs)

    def test_build__stores_the_file_with_mime_and_download_name(
        self,
        repo: Repo,
        novel_id: str,
        storage: HookedStorage,
        runner: Runner,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        add_downloaded(repo, novel_id, 1)
        view = create_export(repo, novel_id, 1, 1)
        calls: list[tuple[str, str, str | None]] = []
        original = storage.put_file

        def spy(key: str, path: Path, mime: str, download_name: str | None = None) -> None:
            calls.append((key, mime, download_name))
            original(key, path, mime, download_name)

        monkeypatch.setattr(storage, "put_file", spy)

        run_build(runner)

        assert calls == [(key_of(view), "application/epub+zip", view.filename)]

    def test_build__marks_the_export_ready(
        self, repo: Repo, novel_id: str, storage: HookedStorage, runner: Runner
    ) -> None:
        add_downloaded(repo, novel_id, 1)
        view = create_export(repo, novel_id, 1, 1)

        run_build(runner)

        row = export_of(repo, view.id)
        stored = storage._path(row.storage_key)  # noqa: SLF001
        assert row.state == ExportState.READY
        assert row.size_bytes == stored.stat().st_size > 0
        assert row.ready_at == repo.clock()

    def test_build__contacts_no_host_so_books_no_slot(
        self, repo: Repo, novel_id: str, runner: Runner
    ) -> None:
        add_downloaded(repo, novel_id, 1)
        create_export(repo, novel_id, 1, 1)
        with db.write(repo.engine) as conn:
            conn.execute(update(hosts).values(reach_after=None))

        run_build(runner)

        with db.read(repo.engine) as conn:
            assert set(conn.execute(select(hosts.c.reach_after)).scalars()) == {None}

    def test_build__removes_its_temporary_file(
        self, repo: Repo, novel_id: str, runner: Runner, tmp_dir: Path
    ) -> None:
        add_downloaded(repo, novel_id, 1)
        create_export(repo, novel_id, 1, 1)

        run_build(runner)

        assert list(tmp_dir.iterdir()) == []

    def test_build__runs_while_the_novel_syncs_and_its_chapters_download(
        self, repo: Repo, novel_id: str, runner: Runner
    ) -> None:
        add_downloaded(repo, novel_id, 1)
        add_chapter(repo, novel_id, 2, state="downloading", claimed_by=OTHER,
                    claimed_until=repo.clock() + timedelta(hours=1))  # fmt: skip
        refresh = live_task(repo, novel_id)  # parked: claimed by OTHER
        repo.tasks.start(refresh, OTHER)  # the novel is syncing
        view = create_export(repo, novel_id, 1, 1)

        run_build(runner)

        assert export_of(repo, view.id).state == ExportState.READY
        assert repo.novels.get(novel_id).syncing is True

    def test_build__renews_the_task_claim_through_a_heartbeat(
        self,
        repo: Repo,
        novel_id: str,
        settings: Settings,
        make_runner: Callable[..., Runner],
    ) -> None:
        add_downloaded(repo, novel_id, 1)
        create_export(repo, novel_id, 1, 1)
        beats: list[tuple[Callable[[], bool], float]] = []

        class Recorded:
            def __init__(self, renew: Callable[[], bool], interval_s: float) -> None:
                beats.append((renew, interval_s))
                self.lost = False
                self._renew = renew

            def __enter__(self) -> Recorded:
                assert self._renew() is True  # the claim is still ours while building
                return self

            def __exit__(self, *_: object) -> None:
                return None

        run_build(make_runner(Recorded))

        assert [interval for _, interval in beats] == [settings.claim_ttl / 3]


class TestBuild__SkipMissing:
    def test_build__contains_only_the_downloaded_chapters(
        self, repo: Repo, novel_id: str, storage: HookedStorage, runner: Runner
    ) -> None:
        add_downloaded(repo, novel_id, 1)
        add_chapter(repo, novel_id, 2, state="not_requested")
        add_downloaded(repo, novel_id, 3)
        view = create_export(repo, novel_id, 1, 3, skip_missing=True)

        run_build(runner)

        text = epub_text(storage._path(export_of(repo, view.id).storage_key))  # noqa: SLF001
        assert "Body of 1" in text
        assert "Body of 3" in text
        assert "Chapter 2" not in text


class TestBuild__RangeChanged:
    def test_build__fails_the_export_when_an_end_chapter_was_removed(
        self, repo: Repo, novel_id: str, runner: Runner
    ) -> None:
        add_downloaded(repo, novel_id, 1)
        last = add_downloaded(repo, novel_id, 2)
        view = create_export(repo, novel_id, 1, 2)
        remove_position(repo, last)

        run_build(runner)

        row = export_of(repo, view.id)
        assert row.state == ExportState.FAILED
        assert row.error == "Range changed upstream"


class TestBuild__WhenBuildingRaises:
    @pytest.fixture
    def failing(self, repo: Repo, novel_id: str, monkeypatch: pytest.MonkeyPatch) -> ExportView:
        add_downloaded(repo, novel_id, 1)
        view = create_export(repo, novel_id, 1, 1)

        def boom(fmt: Format, book: BookInput, out: Path) -> None:
            out.write_bytes(b"partial")
            msg = "disk\nexploded"
            raise OSError(msg)

        monkeypatch.setattr(exporter, "build", boom)
        return view

    def test_build__marks_the_export_failed_with_type_and_summary(
        self, repo: Repo, failing: ExportView, runner: Runner
    ) -> None:
        run_build(runner)

        row = export_of(repo, failing.id)
        assert row.state == ExportState.FAILED
        assert row.error == "OSError: disk exploded"

    def test_build__logs_the_traceback(self, failing: ExportView, runner: Runner) -> None:
        with structlog.testing.capture_logs() as logs:
            run_build(runner)

        entry = next(e for e in logs if e["event"] == "export_build_failed")
        assert entry["exc_info"] is True

    def test_build__removes_the_temporary_file(
        self, failing: ExportView, runner: Runner, tmp_dir: Path
    ) -> None:
        run_build(runner)

        assert list(tmp_dir.iterdir()) == []


class TestBuild__WhenTheClaimWasLost:
    def test_build__trashes_the_key_and_leaves_the_export_alone(
        self, repo: Repo, novel_id: str, storage: HookedStorage, runner: Runner
    ) -> None:
        add_downloaded(repo, novel_id, 1)
        view = create_export(repo, novel_id, 1, 1)
        storage.after_put = lambda: steal(repo, view.id)

        run_build(runner)

        assert trash_keys(repo.engine) == [key_of(view)]
        row = export_of(repo, view.id)
        assert (row.state, row.storage_key) == ("building", None)
        assert live_task(repo, view.id, TaskState.RUNNING).claimed_by == OTHER


def make_worker(
    repo: Repo, adapter: FakeAdapter, settings: Settings, tmp_path: Path, stop: threading.Event
) -> Worker:
    storage = LocalStaticStorage(tmp_path / "static", "/static")
    return Worker(repo, adapter, FakeFetcher(), storage, {}, settings, stop=stop)


class TestStep__WithAReadyExport:
    def test_step__builds_before_starting_another_download(
        self, repo: Repo, novel_id: str, settings: Settings, tmp_path: Path
    ) -> None:
        worker = make_worker(repo, FakeAdapter(), settings, tmp_path, RecordingStop())
        add_downloaded(repo, novel_id, 1)
        add_chapter(repo, novel_id, 2)  # still pending
        view = create_export(repo, novel_id, 1, 1)

        assert worker.step() == "task"
        assert repo.exports.get(novel_id, view.id).state is ExportState.READY
        assert chapter_of(repo, "CH000000000002").state == "pending"
        assert worker.step() == "task"
        assert chapter_of(repo, "CH000000000002").state == "downloaded"


class TestEndToEnd__ExportWithMissingChapters:
    def test_export__waits_reports_blocked_positions_and_becomes_ready(
        self, repo: Repo, novel_id: str, settings: Settings, tmp_path: Path
    ) -> None:
        adapter = FakeAdapter()
        worker = make_worker(repo, adapter, settings, tmp_path, RecordingStop())
        for position in (1, 2, 3):
            add_chapter(repo, novel_id, position, state="not_requested")
        view = create_export(repo, novel_id, 1, 3)
        adapter.errors["https://example.com/novel/2"] = ValueError("boom")
        states: list[ExportState] = []
        adapter.hook = lambda url: states.append(repo.exports.get(novel_id, view.id).state)

        assert [worker.step() for _ in range(3)] == ["task"] * 3  # one download each

        assert states == [ExportState.WAITING] * 3  # waiting while chapters download
        waiting = repo.exports.get(novel_id, view.id)
        assert waiting.state is ExportState.WAITING
        assert waiting.blocked.errors == (2,)
        assert worker.step() == "idle"

        del adapter.errors["https://example.com/novel/2"]
        repo.chapters.patch(novel_id, ChapterState.PENDING, [2])
        assert worker.step() == "task"  # the download
        assert worker.step() == "task"  # the build
        assert repo.exports.get(novel_id, view.id).state is ExportState.READY


class TestBuild__Hardening:
    def test_build__lost_heartbeat_skips_upload_and_finish(
        self,
        repo: Repo,
        novel_id: str,
        storage: HookedStorage,
        tmp_dir: Path,
        make_runner: Callable[..., Runner],
    ) -> None:
        add_downloaded(repo, novel_id, 1)
        view = create_export(repo, novel_id, 1, 1)

        with structlog.testing.capture_logs() as logs:
            run_build(make_runner(hooked(lambda: steal(repo, view.id), lost=True)))

        assert storage.get(key_of(view)) is None
        assert trash_keys(repo.engine) == []
        assert export_of(repo, view.id).state == ExportState.BUILDING
        assert list(tmp_dir.iterdir()) == []
        assert any(entry["event"] == "export_claim_lost" for entry in logs)

    def test_build__each_attempt_uses_its_own_temporary_path(
        self,
        repo: Repo,
        novel_id: str,
        tmp_dir: Path,
        ctx: TaskContext,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        add_downloaded(repo, novel_id, 1)
        view = create_export(repo, novel_id, 1, 1)
        started = start_build(repo, view.id, ME)
        paths: list[Path] = []

        def record(fmt: Format, book: BookInput, out: Path) -> None:
            paths.append(out)
            out.write_bytes(b"x")

        monkeypatch.setattr(exporter, "build", record)
        processor = ExportBuildProcessor()

        processor.run(started.task, started.work, ctx)  # the same build run twice by hand
        processor.run(started.task, started.work, ctx)

        assert paths[0] != paths[1]
        assert paths[0].parent == paths[1].parent == tmp_dir
        assert view.id in paths[0].name

    def test_build__leaves_out_an_interior_chapter_removed_after_the_start(
        self,
        repo: Repo,
        novel_id: str,
        storage: HookedStorage,
        make_runner: Callable[..., Runner],
    ) -> None:
        add_downloaded(repo, novel_id, 1)
        middle = add_downloaded(repo, novel_id, 2)
        add_downloaded(repo, novel_id, 3)
        view = create_export(repo, novel_id, 1, 3)

        run_build(make_runner(hooked(lambda: remove_position(repo, middle))))

        row = export_of(repo, view.id)
        text = epub_text(storage._path(row.storage_key))  # noqa: SLF001
        assert row.state == ExportState.READY
        assert "Body of 1" in text
        assert "Body of 3" in text
        assert "Body of 2" not in text

    def test_build__fails_when_an_end_chapter_was_removed_after_the_start(
        self, repo: Repo, novel_id: str, make_runner: Callable[..., Runner]
    ) -> None:
        add_downloaded(repo, novel_id, 1)
        last = add_downloaded(repo, novel_id, 2)
        view = create_export(repo, novel_id, 1, 2)

        run_build(make_runner(hooked(lambda: remove_position(repo, last))))

        row = export_of(repo, view.id)
        assert (row.state, row.error) == (ExportState.FAILED, "Range changed upstream")

    def test_build__a_failing_cover_read_builds_without_cover(
        self,
        repo: Repo,
        novel_id: str,
        storage: HookedStorage,
        runner: Runner,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        add_downloaded(repo, novel_id, 1)
        with db.write(repo.engine) as conn:
            conn.execute(update(novels).values(cover_key="covers/x/a.png"))
        view = create_export(repo, novel_id, 1, 1)

        def broken(key: str) -> bytes | None:
            raise ValueError(key)

        monkeypatch.setattr(storage, "get", broken)

        with structlog.testing.capture_logs() as logs:
            run_build(runner)

        assert export_of(repo, view.id).state == ExportState.READY
        assert any(entry["event"] == "export_cover_failed" for entry in logs)

    def test_build__success_leaves_the_trash_empty(
        self, repo: Repo, novel_id: str, runner: Runner
    ) -> None:
        add_downloaded(repo, novel_id, 1)
        create_export(repo, novel_id, 1, 1)

        run_build(runner)

        assert trash_keys(repo.engine) == []

    def test_build__a_storage_failure_fails_the_export_and_removes_the_temp_file(
        self,
        repo: Repo,
        novel_id: str,
        storage: HookedStorage,
        runner: Runner,
        tmp_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        add_downloaded(repo, novel_id, 1)
        view = create_export(repo, novel_id, 1, 1)

        def refuse(*_: object, **__: object) -> None:
            msg = "no space"
            raise OSError(msg)

        monkeypatch.setattr(storage, "put_file", refuse)

        run_build(runner)

        row = export_of(repo, view.id)
        assert (row.state, row.error) == (ExportState.FAILED, "OSError: no space")
        assert list(tmp_dir.iterdir()) == []

    def test_build__old_owner_losing_the_claim_cannot_get_the_new_owners_file_deleted(
        self, repo: Repo, novel_id: str, storage: HookedStorage, runner: Runner
    ) -> None:
        add_downloaded(repo, novel_id, 1)
        view = create_export(repo, novel_id, 1, 1)
        key = key_of(view)

        def new_owner_finishes() -> None:
            # The old owner's claim is gone; the new owner publishes the same deterministic key.
            steal(repo, view.id)
            task = live_task(repo, view.id, TaskState.RUNNING)
            assert repo.tasks.finish(task, OTHER, Done(BuildResult(key, 1))) is not None

        storage.after_put = new_owner_finishes

        run_build(runner)  # old owner: the finish fence fails -> discard

        assert trash_keys(repo.engine) == []
        assert storage.get(key) is not None
