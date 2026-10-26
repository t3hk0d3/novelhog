"""Tests for API and SSE payload models."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone

import pytest

from novelhog.domain.chapters import ChapterState
from novelhog.domain.exports import ExportState
from novelhog.domain.payloads import (
    ChapterView,
    ExportBlocked,
    ExportProgress,
    ExportView,
    NovelView,
    SubscriptionView,
    deleted_export,
    deleted_novel,
    format_time,
)

T = datetime(2026, 10, 19, 12, 30, 5, 123456, tzinfo=UTC)
TS = "2026-10-19T12:30:05.123Z"


def novel() -> NovelView:
    return NovelView(
        id="NVa",
        title=None,
        host="example.com",
        toc_url="https://example.com/n",
        import_error=None,
        importing=True,
        cover_url=None,
        followed=True,
        syncing=False,
        refresh_interval=21600,
        next_refresh_at=T,
        last_refreshed_at=None,
        total_pages=1,
        downloaded_pages=2,
        pending_pages=3,
        error_pages=4,
        new_pages=5,
        created_at=T,
        updated_at=T,
    )


def chapter(state: ChapterState) -> ChapterView:
    return ChapterView(
        id="CHa",
        position=3,
        url="https://example.com/c3",
        title="Three",
        state=state,
        has_images=False,
        released_at=T,
        queued_at=T,
        updated_at=T,
        error="boom",
        error_at=T,
    )


def export(state: ExportState) -> ExportView:
    return ExportView(
        id="EXa",
        novel_id="NVa",
        filename="A Chapters 1-2.epub",
        chapter_from=None,
        chapter_to=None,
        skip_missing=False,
        mime="application/epub+zip",
        state=state,
        progress=ExportProgress(1, 2),
        blocked=ExportBlocked((1,), (2,)),
        waited=True,
        created_at=T,
        ready_at=T,
        error="bad",
        size_bytes=10,
        url="/static/x",
    )


NOVEL_FIELDS = {
    "id", "title", "host", "toc_url", "import_error", "importing", "cover_url", "followed",
    "syncing", "refresh_interval", "next_refresh_at", "last_refreshed_at", "total_pages",
    "downloaded_pages", "pending_pages", "error_pages", "new_pages", "created_at", "updated_at",
}  # fmt: skip


class TestNovelView__ToJson:
    def test_to_json__has_exactly_the_spec_fields(self) -> None:
        assert set(novel().to_json()) == NOVEL_FIELDS

    def test_to_json__formats_timestamps(self) -> None:
        data = novel().to_json()
        assert data["next_refresh_at"] == TS
        assert data["created_at"] == TS
        assert data["last_refreshed_at"] is None


class TestNovelView__ToEvent:
    def test_to_event__adds_released(self) -> None:
        assert novel().to_event([4, 5]) == {**novel().to_json(), "released": [4, 5]}


class TestChapterView__ToJson:
    def test_to_json__error_fields_only_in_error_state(self) -> None:
        err = chapter(ChapterState.ERROR).to_json()
        assert (err["error"], err["error_at"]) == ("boom", TS)
        ok = chapter(ChapterState.DOWNLOADED).to_json()
        assert (ok["error"], ok["error_at"]) == (None, None)

    @pytest.mark.parametrize("state", list(ChapterState))
    def test_to_json__queued_at_only_when_pending_or_downloading(self, state: ChapterState) -> None:
        queued = state in {ChapterState.PENDING, ChapterState.DOWNLOADING}
        assert chapter(state).to_json()["queued_at"] == (TS if queued else None)

    def test_to_json__has_spec_fields(self) -> None:
        assert set(chapter(ChapterState.ERROR).to_json()) == {
            "id", "position", "url", "title", "state", "error", "error_at", "has_images",
            "released_at", "queued_at", "updated_at",
        }  # fmt: skip


class TestChapterView__ToEvent:
    def test_to_event__adds_novel_id(self) -> None:
        assert chapter(ChapterState.PENDING).to_event("NVa", None, None)["novel_id"] == "NVa"

    def test_to_event__error_fields_from_arguments_in_error_state(self) -> None:
        data = chapter(ChapterState.ERROR).to_event("NVa", "newer", T + timedelta(seconds=1))
        assert (data["error"], data["error_at"]) == ("newer", "2026-10-19T12:30:06.123Z")

    def test_to_event__no_error_outside_error_state(self) -> None:
        data = chapter(ChapterState.PENDING).to_event("NVa", "stale", T)
        assert (data["error"], data["error_at"]) == (None, None)


EXPORT_FIELDS = {
    "id", "novel_id", "filename", "chapter_from", "chapter_to", "skip_missing", "format", "mime",
    "state", "progress", "blocked", "error", "waited", "size_bytes", "url", "created_at",
    "ready_at",
}  # fmt: skip


class TestExportView__ToJson:
    def test_to_json__has_exactly_the_spec_fields(self) -> None:
        assert set(export(ExportState.READY).to_json()) == EXPORT_FIELDS

    def test_to_json__derives_format_from_mime(self) -> None:
        assert export(ExportState.READY).to_json()["format"] == "epub"

    def test_to_json__blocked_only_when_waiting_without_skip_missing(self) -> None:
        empty: dict[str, list[int]] = {"errors": [], "not_queued": []}
        assert export(ExportState.WAITING).to_json()["blocked"] == {
            "errors": [1],
            "not_queued": [2],
        }
        assert export(ExportState.BUILDING).to_json()["blocked"] == empty
        assert replace(export(ExportState.WAITING), skip_missing=True).to_json()["blocked"] == empty

    def test_to_json__url_and_size_only_when_ready(self) -> None:
        ready = export(ExportState.READY).to_json()
        assert (ready["url"], ready["size_bytes"]) == ("/static/x", 10)
        waiting = export(ExportState.WAITING).to_json()
        assert (waiting["url"], waiting["size_bytes"]) == (None, None)

    def test_to_json__error_only_when_failed(self) -> None:
        assert export(ExportState.FAILED).to_json()["error"] == "bad"
        assert export(ExportState.READY).to_json()["error"] is None

    def test_to_json__includes_computed_fields_and_null_range(self) -> None:
        data = export(ExportState.WAITING).to_json()
        assert data["waited"] is True
        assert data["skip_missing"] is False
        assert data["progress"] == {"downloaded": 1, "total": 2}
        assert data["blocked"] == {"errors": [1], "not_queued": [2]}
        assert (data["chapter_from"], data["chapter_to"]) == (None, None)
        assert data["format"] == "epub"
        assert data["state"] == "waiting"


class TestSubscriptionView__ToJson:
    def test_to_json__has_no_address(self) -> None:
        data = SubscriptionView("SBa", "webpush", None, T).to_json()
        assert data == {"id": "SBa", "channel": "webpush", "label": None, "created_at": TS}


class TestDeleted:
    def test_deleted_novel__returns_id_and_flag(self) -> None:
        assert deleted_novel("NVa") == {"id": "NVa", "deleted": True}

    def test_deleted_export__returns_ids_and_flag(self) -> None:
        assert deleted_export("EXa", "NVa") == {"id": "EXa", "novel_id": "NVa", "deleted": True}


class TestFormatTime:
    def test_format_time__formats_utc_with_milliseconds(self) -> None:
        assert format_time(T) == TS

    def test_format_time__converts_other_zones_to_utc(self) -> None:
        assert format_time(T.astimezone(timezone(timedelta(hours=3)))) == TS

    def test_format_time__pads_milliseconds(self) -> None:
        assert format_time(datetime(2026, 1, 2, tzinfo=UTC)) == "2026-01-02T00:00:00.000Z"

    def test_format_time__raises_for_naive(self) -> None:
        with pytest.raises(ValueError, match="naive"):
            format_time(datetime(2026, 1, 2))  # noqa: DTZ001
