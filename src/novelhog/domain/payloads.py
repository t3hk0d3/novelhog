"""API and SSE payload models (spec sections 5.2, 5.6 and 5.7)."""

from dataclasses import dataclass, replace
from datetime import UTC, datetime

from novelhog.domain.chapters import ChapterState
from novelhog.domain.exports import ExportState, Format


def format_time(value: datetime) -> str:
    """Format an aware datetime as RFC 3339 UTC with milliseconds and `Z`."""
    if value.tzinfo is None or value.utcoffset() is None:
        msg = "naive datetime cannot be put on the wire"
        raise ValueError(msg)
    utc = value.astimezone(UTC)
    return f"{utc:%Y-%m-%dT%H:%M:%S}.{utc.microsecond // 1000:03d}Z"


def _time(value: datetime | None) -> str | None:
    return None if value is None else format_time(value)


@dataclass(frozen=True)
class NovelView:
    """The API `Novel` object; counters and flags are computed by `repo`."""

    id: str
    title: str | None
    host: str
    toc_url: str
    import_error: str | None
    importing: bool
    cover_url: str | None
    followed: bool
    syncing: bool
    refresh_interval: int
    next_refresh_at: datetime
    last_refreshed_at: datetime | None
    total_pages: int
    downloaded_pages: int
    pending_pages: int
    error_pages: int
    new_pages: int
    created_at: datetime
    updated_at: datetime

    def to_json(self) -> dict[str, object]:
        """Return the wire form of the novel."""
        return {
            "id": self.id,
            "title": self.title,
            "host": self.host,
            "toc_url": self.toc_url,
            "import_error": self.import_error,
            "importing": self.importing,
            "cover_url": self.cover_url,
            "followed": self.followed,
            "syncing": self.syncing,
            "refresh_interval": self.refresh_interval,
            "next_refresh_at": format_time(self.next_refresh_at),
            "last_refreshed_at": _time(self.last_refreshed_at),
            "total_pages": self.total_pages,
            "downloaded_pages": self.downloaded_pages,
            "pending_pages": self.pending_pages,
            "error_pages": self.error_pages,
            "new_pages": self.new_pages,
            "created_at": format_time(self.created_at),
            "updated_at": format_time(self.updated_at),
        }

    def to_event(self, released: list[int]) -> dict[str, object]:
        """Return the `novel` event data written by a sync: the novel plus released positions."""
        return {**self.to_json(), "released": released}


@dataclass(frozen=True)
class ChapterView:
    """The API `Chapter` object. `error` and `error_at` matter only in the error state."""

    id: str
    position: int
    url: str
    title: str | None
    state: ChapterState
    has_images: bool
    released_at: datetime | None
    queued_at: datetime | None
    updated_at: datetime
    error: str | None = None
    error_at: datetime | None = None

    def to_json(self) -> dict[str, object]:
        """Return the wire form; state-specific fields are null outside their state."""
        is_error = self.state is ChapterState.ERROR
        is_queued = self.state in {ChapterState.PENDING, ChapterState.DOWNLOADING}
        return {
            "id": self.id,
            "position": self.position,
            "url": self.url,
            "title": self.title,
            "state": self.state.value,
            "error": self.error if is_error else None,
            "error_at": _time(self.error_at) if is_error else None,
            "has_images": self.has_images,
            "released_at": _time(self.released_at),
            "queued_at": _time(self.queued_at) if is_queued else None,
            "updated_at": format_time(self.updated_at),
        }

    def to_event(
        self, novel_id: str, error: str | None, error_at: datetime | None
    ) -> dict[str, object]:
        """Return the `chapter` event data: the chapter plus `novel_id`.

        `error` and `error_at` (taken from the newest event) override the view's own and
        are emitted only in the error state.
        """
        return {**replace(self, error=error, error_at=error_at).to_json(), "novel_id": novel_id}


@dataclass(frozen=True)
class ExportProgress:
    """How many chapters of an export's range are downloaded."""

    downloaded: int
    total: int


@dataclass(frozen=True)
class ExportBlocked:
    """Positions holding a waiting export back."""

    errors: tuple[int, ...]
    not_queued: tuple[int, ...]


@dataclass(frozen=True)
class ExportView:
    """The API `Export` object; `progress` and `blocked` are computed by `repo`."""

    id: str
    novel_id: str
    filename: str
    chapter_from: int | None
    chapter_to: int | None
    skip_missing: bool
    format: Format
    mime: str
    state: ExportState
    progress: ExportProgress
    blocked: ExportBlocked
    waited: bool
    created_at: datetime
    ready_at: datetime | None
    error: str | None = None
    size_bytes: int | None = None
    url: str | None = None

    def to_json(self) -> dict[str, object]:
        """Return the wire form; `url`/`size_bytes` only when ready, `error` only when failed."""
        ready = self.state is ExportState.READY
        return {
            "id": self.id,
            "novel_id": self.novel_id,
            "filename": self.filename,
            "chapter_from": self.chapter_from,
            "chapter_to": self.chapter_to,
            "skip_missing": self.skip_missing,
            "format": self.format.value,
            "mime": self.mime,
            "state": self.state.value,
            "progress": {"downloaded": self.progress.downloaded, "total": self.progress.total},
            "blocked": {
                "errors": list(self.blocked.errors),
                "not_queued": list(self.blocked.not_queued),
            },
            "error": self.error if self.state is ExportState.FAILED else None,
            "waited": self.waited,
            "size_bytes": self.size_bytes if ready else None,
            "url": self.url if ready else None,
            "created_at": format_time(self.created_at),
            "ready_at": _time(self.ready_at),
        }


@dataclass(frozen=True)
class SubscriptionView:
    """The API `Subscription` object; the channel address is never included."""

    id: str
    channel: str
    label: str | None
    created_at: datetime

    def to_json(self) -> dict[str, object]:
        """Return the wire form."""
        return {
            "id": self.id,
            "channel": self.channel,
            "label": self.label,
            "created_at": format_time(self.created_at),
        }


def deleted_novel(novel_id: str) -> dict[str, object]:
    """Return the `novel` event data after a delete."""
    return {"id": novel_id, "deleted": True}


def deleted_export(export_id: str, novel_id: str) -> dict[str, object]:
    """Return the `export` event data after a delete."""
    return {"id": export_id, "novel_id": novel_id, "deleted": True}
