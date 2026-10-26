"""Notification rules (spec 6.6, 8.6)."""

from dataclasses import replace
from datetime import UTC, datetime

from novelhog.domain.payloads import NovelView
from novelhog.notifications.rules import NotificationMessage, match
from novelhog.repo.events import EventRow

PUBLIC = "https://novels.example"
NOW = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)
NOVEL_ID = "NV000000000001"
EXPORT_ID = "EX000000000001"


def _novel(**overrides: object) -> NovelView:
    base = NovelView(
        id=NOVEL_ID,
        title="The Tale",
        host="site.example",
        toc_url="https://site.example/toc",
        import_error=None,
        importing=False,
        cover_url=None,
        followed=True,
        syncing=False,
        refresh_interval=3600,
        next_refresh_at=NOW,
        last_refreshed_at=NOW,
        total_pages=10,
        downloaded_pages=0,
        pending_pages=0,
        error_pages=0,
        new_pages=0,
        created_at=NOW,
        updated_at=NOW,
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


def _event(entity_id: str, payload: dict[str, object]) -> EventRow:
    return EventRow(1, NOVEL_ID, entity_id, payload, NOW)


def _nv(**payload: object) -> EventRow:
    return _event(NOVEL_ID, {"followed": True, "released": [4, 5, 6], **payload})


def _ex(**payload: object) -> EventRow:
    data = {"filename": "tale.epub", "state": "ready", "waited": True, **payload}
    return _event(EXPORT_ID, data)


class TestMatch__OnReleasedChapters:
    def test_match__returns_the_release_message(self) -> None:
        message = match(_nv(), _novel(), public_url=PUBLIC)
        assert message == NotificationMessage(
            title="The Tale",
            body="3 new chapters (4–6)",
            url=f"{PUBLIC}/novels/{NOVEL_ID}",
            icon=f"{PUBLIC}/icons/192.png",
            topic=NOVEL_ID,
        )

    def test_match__uses_the_host_when_the_novel_has_no_title(self) -> None:
        message = match(_nv(), _novel(title=None), public_url=PUBLIC)
        assert message is not None
        assert message.title == "site.example"

    def test_match__says_chapter_for_a_single_release(self) -> None:
        message = match(_nv(released=[7]), _novel(), public_url=PUBLIC)
        assert message is not None
        assert message.body == "1 new chapter (7–7)"


class TestMatch__OnNovelEventsWithoutARelease:
    def test_match__returns_none_for_empty_released(self) -> None:
        assert match(_nv(released=[]), _novel(), public_url=PUBLIC) is None

    def test_match__returns_none_for_missing_released(self) -> None:
        event = _event(NOVEL_ID, {"followed": True})
        assert match(event, _novel(), public_url=PUBLIC) is None

    def test_match__returns_none_when_not_followed(self) -> None:
        assert match(_nv(followed=False), _novel(), public_url=PUBLIC) is None

    def test_match__returns_none_for_a_deleted_marker(self) -> None:
        event = _event(NOVEL_ID, {"id": NOVEL_ID, "deleted": True})
        assert match(event, _novel(), public_url=PUBLIC) is None


class TestMatch__OnExportEvents:
    def test_match__returns_the_ready_message_for_a_waited_export(self) -> None:
        message = match(_ex(), _novel(), public_url=PUBLIC)
        assert message is not None
        assert message.body == "tale.epub is ready"
        assert message.url == f"{PUBLIC}/novels/{NOVEL_ID}#exports"
        assert message.topic == NOVEL_ID

    def test_match__returns_none_when_the_export_was_not_waited_for(self) -> None:
        assert match(_ex(waited=False), _novel(), public_url=PUBLIC) is None

    def test_match__returns_none_for_other_states(self) -> None:
        assert match(_ex(state="building"), _novel(), public_url=PUBLIC) is None

    def test_match__returns_none_for_a_deleted_marker(self) -> None:
        event = _event(EXPORT_ID, {"id": EXPORT_ID, "deleted": True})
        assert match(event, _novel(), public_url=PUBLIC) is None


class TestMatch__OnFailedExport:
    def test_match__returns_the_failed_message_with_the_exports_url(self) -> None:
        event = _ex(state="failed", waited=False, error="Chapter 2 was cancelled")
        message = match(event, _novel(), public_url=PUBLIC)
        assert message is not None
        assert message.body == "tale.epub failed: Chapter 2 was cancelled"
        assert message.url == f"{PUBLIC}/novels/{NOVEL_ID}#exports"

    def test_match__returns_none_for_a_deleted_novel(self) -> None:
        event = _ex(state="failed", error="boom")
        assert match(event, None, public_url=PUBLIC) is None


class TestMatch__OnOtherEvents:
    def test_match__returns_none_for_chapter_events(self) -> None:
        event = _event("CH000000000001", {"state": "downloaded", "released": [1]})
        assert match(event, _novel(), public_url=PUBLIC) is None

    def test_match__returns_none_when_the_novel_no_longer_exists(self) -> None:
        assert match(_nv(), None, public_url=PUBLIC) is None
        assert match(_ex(), None, public_url=PUBLIC) is None


class TestMatch__Icon:
    def test_match__makes_a_relative_cover_absolute(self) -> None:
        novel = _novel(cover_url="/static/covers/a.jpg")
        message = match(_nv(), novel, public_url=PUBLIC)
        assert message is not None
        assert message.icon == f"{PUBLIC}/static/covers/a.jpg"

    def test_match__keeps_an_absolute_cover(self) -> None:
        novel = _novel(cover_url="https://cdn.example/a.jpg")
        message = match(_nv(), novel, public_url=PUBLIC)
        assert message is not None
        assert message.icon == "https://cdn.example/a.jpg"

    def test_match__falls_back_to_the_default_icon(self) -> None:
        message = match(_nv(), _novel(), public_url=PUBLIC + "/")
        assert message is not None
        assert message.icon == f"{PUBLIC}/icons/192.png"

    def test_match__keeps_a_protocol_relative_cover(self) -> None:
        novel = _novel(cover_url="//cdn.example/a.jpg")
        message = match(_nv(), novel, public_url=PUBLIC)
        assert message is not None
        assert message.icon == "//cdn.example/a.jpg"
