"""Export builds (spec section 6.5) and their place in the worker loop."""

import threading
import zipfile
from collections.abc import Callable
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
from novelhog.exporter import BookInput
from novelhog.repo import Repo, db
from novelhog.repo import contents as contents_repo
from novelhog.repo.exports import ExportClaim
from novelhog.repo.schema import chapters, exports, novels
from novelhog.storage import LocalStaticStorage
from novelhog.worker import builds
from novelhog.worker.loop import Worker
from tests.integration.worker.conftest import (
    ME,
    FakeAdapter,
    FakeFetcher,
    RecordingStop,
    add_chapter,
    chapter_of,
)

OTHER = "worker-b"


class HookedStorage(LocalStaticStorage):
    """Local storage that runs a hook after each `put_file`."""

    def __init__(self, root: Path) -> None:
        super().__init__(root, "/static")
        self.after_put: Callable[[], None] | None = None

    def put_file(self, key: str, path: Path, mime: str, download_name: str | None = None) -> None:
        super().put_file(key, path, mime, download_name)
        if self.after_put is not None:
            self.after_put()


@pytest.fixture
def storage(tmp_path: Path) -> HookedStorage:
    return HookedStorage(tmp_path / "static")


@pytest.fixture
def tmp_dir(tmp_path: Path) -> Path:
    return tmp_path / "work"


def add_downloaded(repo: Repo, novel: str, at: int, title: str | None = None) -> str:
    chapter_id = add_chapter(repo, novel, at, state="downloaded", queued_at=None, title=title)
    with db.write(repo.engine) as conn:
        contents_repo.put(conn, chapter_id, f"Body of {at}", repo.clock())
    return chapter_id


def claim_export(
    repo: Repo, novel: str, start: int, end: int, *, skip_missing: bool = False
) -> ExportClaim:
    repo.exports.create(novel, start, end, Format.EPUB, skip_missing=skip_missing)
    claim = repo.exports.claim_ready(ME, 300)
    assert claim is not None
    return claim


def run_build(
    repo: Repo, storage: HookedStorage, claim: ExportClaim, settings: Settings, tmp_dir: Path
) -> None:
    builds.build(repo, ChapterStore(repo), storage, claim, settings=settings, tmp_dir=tmp_dir)


def export_of(repo: Repo, export_id: str) -> Any:
    with db.read(repo.engine) as conn:
        return conn.execute(select(exports).where(exports.c.id == export_id)).one()


def epub_text(path: Path) -> str:
    with zipfile.ZipFile(path) as archive:
        return "".join(archive.read(n).decode() for n in archive.namelist() if n.endswith(".xhtml"))


class TestBuild__OnSuccess:
    def test_build__builds_from_stored_chapters_in_position_order(
        self,
        repo: Repo,
        novel_id: str,
        storage: HookedStorage,
        settings: Settings,
        tmp_dir: Path,
    ) -> None:
        add_downloaded(repo, novel_id, 2, "Second")
        add_downloaded(repo, novel_id, 1, "First")
        claim = claim_export(repo, novel_id, 1, 2)

        run_build(repo, storage, claim, settings, tmp_dir)

        row = export_of(repo, claim.export_id)
        text = epub_text(storage._path(row.storage_key))  # noqa: SLF001
        assert text.index("Body of 1") < text.index("Body of 2")
        assert "First" in text
        assert "Second" in text

    def test_build__falls_back_to_a_numbered_title(
        self,
        repo: Repo,
        novel_id: str,
        storage: HookedStorage,
        settings: Settings,
        tmp_dir: Path,
    ) -> None:
        add_downloaded(repo, novel_id, 1)
        claim = claim_export(repo, novel_id, 1, 1)

        run_build(repo, storage, claim, settings, tmp_dir)

        row = export_of(repo, claim.export_id)
        assert "Chapter 1" in epub_text(storage._path(row.storage_key))  # noqa: SLF001

    def test_build__passes_the_stored_cover_and_language_default(
        self,
        repo: Repo,
        novel_id: str,
        storage: HookedStorage,
        settings: Settings,
        tmp_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        add_downloaded(repo, novel_id, 1)
        storage.put("covers/x/abc.png", b"cover-bytes", "image/png")
        with db.write(repo.engine) as conn:
            conn.execute(update(novels).values(cover_key="covers/x/abc.png"))
        claim = claim_export(repo, novel_id, 1, 1)
        seen: list[BookInput] = []
        monkeypatch.setattr(exporter, "build", lambda fmt, book, out: seen.append(book))

        run_build(repo, storage, claim, settings, tmp_dir)

        assert seen[0].cover == (b"cover-bytes", "image/png")
        assert seen[0].language == "en"

    def test_build__missing_cover_builds_without_one_and_logs(
        self,
        repo: Repo,
        novel_id: str,
        storage: HookedStorage,
        settings: Settings,
        tmp_dir: Path,
    ) -> None:
        add_downloaded(repo, novel_id, 1)
        with db.write(repo.engine) as conn:
            conn.execute(update(novels).values(cover_key="covers/x/gone.png"))
        claim = claim_export(repo, novel_id, 1, 1)

        with structlog.testing.capture_logs() as logs:
            run_build(repo, storage, claim, settings, tmp_dir)

        assert export_of(repo, claim.export_id).state == ExportState.READY
        assert any(entry["event"] == "export_cover_missing" for entry in logs)

    def test_build__stores_the_file_with_mime_and_download_name(
        self,
        repo: Repo,
        novel_id: str,
        storage: HookedStorage,
        settings: Settings,
        tmp_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        add_downloaded(repo, novel_id, 1)
        claim = claim_export(repo, novel_id, 1, 1)
        calls: list[tuple[str, str, str | None]] = []
        original = storage.put_file

        def spy(key: str, path: Path, mime: str, download_name: str | None = None) -> None:
            calls.append((key, mime, download_name))
            original(key, path, mime, download_name)

        monkeypatch.setattr(storage, "put_file", spy)

        run_build(repo, storage, claim, settings, tmp_dir)

        key = f"exports/{claim.novel_id}/{claim.export_id}/{claim.filename}"
        assert calls == [(key, "application/epub+zip", claim.filename)]

    def test_build__marks_the_export_ready(
        self,
        repo: Repo,
        novel_id: str,
        storage: HookedStorage,
        settings: Settings,
        tmp_dir: Path,
    ) -> None:
        add_downloaded(repo, novel_id, 1)
        claim = claim_export(repo, novel_id, 1, 1)

        run_build(repo, storage, claim, settings, tmp_dir)

        row = export_of(repo, claim.export_id)
        stored = storage._path(row.storage_key)  # noqa: SLF001
        assert row.state == ExportState.READY
        assert row.size_bytes == stored.stat().st_size > 0
        assert row.ready_at == repo.clock()

    def test_build__removes_its_temporary_file(
        self,
        repo: Repo,
        novel_id: str,
        storage: HookedStorage,
        settings: Settings,
        tmp_dir: Path,
    ) -> None:
        add_downloaded(repo, novel_id, 1)
        claim = claim_export(repo, novel_id, 1, 1)

        run_build(repo, storage, claim, settings, tmp_dir)

        assert list(tmp_dir.iterdir()) == []

    def test_build__takes_no_novel_claim(
        self,
        repo: Repo,
        novel_id: str,
        storage: HookedStorage,
        settings: Settings,
        tmp_dir: Path,
    ) -> None:
        add_downloaded(repo, novel_id, 1)
        add_chapter(
            repo,
            novel_id,
            2,
            state="downloading",
            claimed_by=OTHER,
            claimed_until=repo.clock() + timedelta(days=1),
        )
        with db.write(repo.engine) as conn:
            conn.execute(
                update(novels).values(claimed_by=OTHER, claimed_until=repo.clock() + timedelta(1))
            )
        claim = claim_export(repo, novel_id, 1, 1)

        run_build(repo, storage, claim, settings, tmp_dir)

        assert export_of(repo, claim.export_id).state == ExportState.READY
        with db.read(repo.engine) as conn:
            assert conn.execute(select(novels.c.claimed_by)).scalar_one() == OTHER

    def test_build__renews_the_export_claim_through_a_heartbeat(
        self,
        repo: Repo,
        novel_id: str,
        storage: HookedStorage,
        settings: Settings,
        tmp_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        add_downloaded(repo, novel_id, 1)
        claim = claim_export(repo, novel_id, 1, 1)
        beats: list[tuple[Callable[[], bool], float]] = []

        class Recorded:
            def __init__(self, renew: Callable[[], bool], interval_s: float) -> None:
                beats.append((renew, interval_s))
                self._renew = renew

            def __enter__(self) -> Recorded:
                assert self._renew() is True  # the claim is still ours while building
                return self

            def __exit__(self, *_: object) -> None:
                return None

        monkeypatch.setattr(builds, "Heartbeat", Recorded)

        run_build(repo, storage, claim, settings, tmp_dir)

        assert [interval for _, interval in beats] == [settings.claim_ttl / 3]


class TestBuild__SkipMissing:
    def test_build__contains_only_the_downloaded_chapters(
        self,
        repo: Repo,
        novel_id: str,
        storage: HookedStorage,
        settings: Settings,
        tmp_dir: Path,
    ) -> None:
        add_downloaded(repo, novel_id, 1)
        add_chapter(repo, novel_id, 2, state="not_requested", queued_at=None)
        add_downloaded(repo, novel_id, 3)
        claim = claim_export(repo, novel_id, 1, 3, skip_missing=True)

        run_build(repo, storage, claim, settings, tmp_dir)

        text = epub_text(storage._path(export_of(repo, claim.export_id).storage_key))  # noqa: SLF001
        assert "Body of 1" in text
        assert "Body of 3" in text
        assert "Chapter 2" not in text


class TestBuild__RangeChanged:
    def test_build__fails_the_export_when_an_end_chapter_was_removed(
        self,
        repo: Repo,
        novel_id: str,
        storage: HookedStorage,
        settings: Settings,
        tmp_dir: Path,
    ) -> None:
        add_downloaded(repo, novel_id, 1)
        last = add_downloaded(repo, novel_id, 2)
        repo.exports.create(novel_id, 1, 2, Format.EPUB)
        with db.write(repo.engine) as conn:
            conn.execute(
                update(chapters)
                .where(chapters.c.id == last)
                .values(position=None, removed_at=repo.clock())
            )
        claim = repo.exports.claim_ready(ME, 300)
        assert claim is not None
        assert claim.chapter_ids is None

        run_build(repo, storage, claim, settings, tmp_dir)

        row = export_of(repo, claim.export_id)
        assert row.state == ExportState.FAILED
        assert row.error == "Range changed upstream"


class TestBuild__WhenBuildingRaises:
    @pytest.fixture
    def failing(
        self,
        repo: Repo,
        novel_id: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> ExportClaim:
        add_downloaded(repo, novel_id, 1)
        claim = claim_export(repo, novel_id, 1, 1)

        def boom(fmt: Format, book: BookInput, out: Path) -> None:
            out.write_bytes(b"partial")
            msg = "disk\nexploded"
            raise OSError(msg)

        monkeypatch.setattr(exporter, "build", boom)
        return claim

    def test_build__marks_the_export_failed_with_type_and_summary(
        self,
        repo: Repo,
        failing: ExportClaim,
        storage: HookedStorage,
        settings: Settings,
        tmp_dir: Path,
    ) -> None:
        run_build(repo, storage, failing, settings, tmp_dir)

        row = export_of(repo, failing.export_id)
        assert row.state == ExportState.FAILED
        assert row.error == "OSError: disk exploded"

    def test_build__logs_the_traceback(
        self,
        repo: Repo,
        failing: ExportClaim,
        storage: HookedStorage,
        settings: Settings,
        tmp_dir: Path,
    ) -> None:
        with structlog.testing.capture_logs() as logs:
            run_build(repo, storage, failing, settings, tmp_dir)

        entry = next(e for e in logs if e["event"] == "export_build_failed")
        assert entry["exc_info"] is True

    def test_build__removes_the_temporary_file(
        self,
        repo: Repo,
        failing: ExportClaim,
        storage: HookedStorage,
        settings: Settings,
        tmp_dir: Path,
    ) -> None:
        run_build(repo, storage, failing, settings, tmp_dir)

        assert list(tmp_dir.iterdir()) == []


class TestBuild__WhenTheClaimWasLost:
    def test_build__trashes_the_key_and_leaves_the_export_alone(
        self,
        repo: Repo,
        novel_id: str,
        storage: HookedStorage,
        settings: Settings,
        tmp_dir: Path,
    ) -> None:
        add_downloaded(repo, novel_id, 1)
        claim = claim_export(repo, novel_id, 1, 1)

        def steal() -> None:
            with db.write(repo.engine) as conn:
                conn.execute(update(exports).values(claimed_by=OTHER))

        storage.after_put = steal

        run_build(repo, storage, claim, settings, tmp_dir)

        key = f"exports/{claim.novel_id}/{claim.export_id}/{claim.filename}"
        assert repo.trash.take() == [key]
        row = export_of(repo, claim.export_id)
        assert (row.state, row.claimed_by, row.storage_key) == ("building", OTHER, None)


def make_worker(
    repo: Repo, adapter: FakeAdapter, settings: Settings, tmp_path: Path, stop: threading.Event
) -> Worker:
    storage = LocalStaticStorage(tmp_path / "static", "/static")
    return Worker(repo, adapter, FakeFetcher(), storage, {}, settings, stop=stop)


class TestStep__WithAReadyExport:
    def test_step__builds_before_starting_another_download_batch(
        self,
        repo: Repo,
        novel_id: str,
        adapter: FakeAdapter,
        settings: Settings,
        tmp_path: Path,
    ) -> None:
        worker = make_worker(repo, adapter, settings, tmp_path, RecordingStop())
        repo.novels.claim_due_refresh("other", 10)  # keeps the refresh out of the way
        add_downloaded(repo, novel_id, 1)
        add_chapter(repo, novel_id, 2)  # still pending
        view, _ = repo.exports.create(novel_id, 1, 1, Format.EPUB)

        assert worker.step() == "build"
        assert repo.exports.get(novel_id, view.id).state is ExportState.READY
        assert chapter_of(repo, "CH000000000002").state == "pending"
        assert worker.step() == "download"


class TestEndToEnd__ExportWithMissingChapters:
    def test_export__waits_reports_blocked_positions_and_becomes_ready(
        self,
        repo: Repo,
        novel_id: str,
        adapter: FakeAdapter,
        settings: Settings,
        tmp_path: Path,
    ) -> None:
        worker = make_worker(repo, adapter, settings, tmp_path, RecordingStop())
        repo.novels.claim_due_refresh("other", 10)
        for position in (1, 2, 3):
            add_chapter(repo, novel_id, position, state="not_requested", queued_at=None)
        view, _ = repo.exports.create(novel_id, 1, 3, Format.EPUB)
        adapter.errors["https://example.com/novel/2"] = ValueError("boom")
        states: list[ExportState] = []
        adapter.hook = lambda url: states.append(repo.exports.get(novel_id, view.id).state)

        assert worker.step() == "download"

        assert states == [ExportState.WAITING] * 3  # waiting while chapters download
        waiting = repo.exports.get(novel_id, view.id)
        assert waiting.state is ExportState.WAITING
        assert waiting.blocked.errors == (2,)
        assert worker.step() == "idle"

        del adapter.errors["https://example.com/novel/2"]
        repo.chapters.patch(novel_id, ChapterState.PENDING, [2])
        assert worker.step() == "download"
        assert worker.step() == "build"
        assert repo.exports.get(novel_id, view.id).state is ExportState.READY
