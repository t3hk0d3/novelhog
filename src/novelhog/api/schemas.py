"""Pydantic models of the API wire format (spec section 5).

Names match the schema names the web client uses. Models are input-only or output-only so the
OpenAPI document holds exactly one schema per name (request models are named `...Create`,
`...Patch`).
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator

from novelhog.domain import webpush
from novelhog.domain.chapters import ChapterState
from novelhog.domain.exports import ExportState
from novelhog.domain.tasks import TaskKind, TaskState


class ErrorDetail(BaseModel):
    """The `error` object of every error response."""

    code: str
    message: str
    details: dict[str, object] | None = None


class ErrorBody(BaseModel):
    """Every error response: `{"error": {"code", "message", "details"?}}`."""

    error: ErrorDetail


class WebpushChannelOut(BaseModel):
    """Web Push availability and the public VAPID key."""

    enabled: bool
    vapid_public_key: str | None


class NotificationChannelsOut(BaseModel):
    """Notification channels by name."""

    webpush: WebpushChannelOut


class NotificationsOut(BaseModel):
    """Notification settings."""

    channels: NotificationChannelsOut


class SettingsOut(BaseModel):
    """`GET /api/v1/settings`: server settings the UI needs at startup (spec section 5.6)."""

    notifications: NotificationsOut
    static_base_url: str
    refresh_min_interval: int
    refresh_max_interval: int


class Host(BaseModel):
    """A supported host (`GET /api/v1/hosts`).

    `reach_after` is when it may be contacted again (a pause after failures, or the request
    delay after a fetch); `failing_since` is when its current run of failures began. Both are
    RFC 3339 strings, null when unset.
    """

    host: str
    reach_after: str | None
    failing_since: str | None


class Novel(BaseModel):
    """The `Novel` object (spec section 4, "Derived values"); timestamps are RFC 3339 strings."""

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
    next_refresh_at: str
    last_refreshed_at: str | None
    total_pages: int
    downloaded_pages: int
    pending_pages: int
    error_pages: int
    new_pages: int
    created_at: str
    updated_at: str


class NovelCreate(BaseModel):
    """`POST /api/v1/novels` request."""

    model_config = ConfigDict(extra="forbid")

    toc_url: str
    followed: bool = True


class NovelPatch(BaseModel):
    """`PATCH /api/v1/novels/{id}` request; absent fields are left unchanged."""

    model_config = ConfigDict(extra="forbid")

    followed: bool | None = None
    seen: Literal[True] | None = None
    title: str | None = None


class Chapter(BaseModel):
    """The `Chapter` object (spec section 4); timestamps are RFC 3339 strings."""

    id: str
    position: int
    url: str
    title: str | None
    state: ChapterState
    error: str | None
    error_at: str | None
    has_images: bool
    released_at: str | None
    queued_at: str | None
    updated_at: str


class ChapterContent(BaseModel):
    """`GET /api/v1/novels/{id}/chapters/{chapter_id}/content`: a downloaded chapter to read.

    `markdown` is the stored chapter text, images inlined as data URIs; `previous_id` and `next_id`
    are the chapters at the positions around it (any state), null at either end.
    """

    chapter: Chapter
    markdown: str
    fetched_at: str
    previous_id: str | None
    next_id: str | None


class ChapterPage(BaseModel):
    """`GET /api/v1/novels/{id}/chapters`: one page and the total matching the filter."""

    items: list[Chapter]
    total: int


class ChapterRange(BaseModel):
    """An inclusive range of chapter positions."""

    model_config = ConfigDict(extra="forbid")

    start: int
    end: int


type ChapterSelector = list[int] | ChapterRange


class ChapterPatch(BaseModel):
    """`PATCH /api/v1/novels/{id}/chapters` request."""

    model_config = ConfigDict(extra="forbid")

    state: ChapterState
    chapters: ChapterSelector


class ChapterSkipped(BaseModel):
    """A chapter a patch left alone, with its current state."""

    position: int
    state: ChapterState


class ChapterPatchResult(BaseModel):
    """`PATCH /api/v1/novels/{id}/chapters` response."""

    updated: list[int]
    skipped: list[ChapterSkipped]


class ExportProgress(BaseModel):
    """Chapters of an export's range that are downloaded, out of the range size."""

    downloaded: int
    total: int


class ExportBlocked(BaseModel):
    """Positions holding a waiting export back."""

    errors: list[int]
    not_queued: list[int]


class Export(BaseModel):
    """The `Export` object (spec section 5.2); timestamps are RFC 3339 strings."""

    id: str
    novel_id: str
    filename: str
    chapter_from: int | None
    chapter_to: int | None
    skip_missing: bool
    format: Literal["epub"]
    mime: str
    state: ExportState
    progress: ExportProgress
    blocked: ExportBlocked
    error: str | None
    waited: bool
    size_bytes: int | None
    url: str | None
    created_at: str
    ready_at: str | None


class ExportCreate(BaseModel):
    """`POST /api/v1/novels/{id}/exports` request.

    `chapters` accepts the general selector and `format` any string so that a list selector and an
    unknown format get their own domain error codes (`invalid_selector`, `unsupported_format`)
    instead of `validation_error`.
    """

    model_config = ConfigDict(extra="forbid")

    chapters: ChapterSelector
    format: str = "epub"
    skip_missing: bool = False


class Subscription(BaseModel):
    """The `Subscription` object of any channel; the address is never included."""

    id: str
    channel: str
    label: str | None
    created_at: str


class WebpushKeys(BaseModel):
    """The `keys` of a browser push subscription."""

    model_config = ConfigDict(extra="forbid")

    p256dh: str
    auth: str

    @field_validator("p256dh")
    @classmethod
    def _p256dh(cls, value: str) -> str:
        return webpush.check_p256dh(value)

    @field_validator("auth")
    @classmethod
    def _auth(cls, value: str) -> str:
        return webpush.check_auth(value)


class WebpushSubscriptionCreate(BaseModel):
    """`POST /api/v1/webpush/subscriptions` request."""

    model_config = ConfigDict(extra="forbid")

    endpoint: str
    keys: WebpushKeys


class Task(BaseModel):
    """The `Task` object (tasks spec section 7); timestamps are RFC 3339 strings."""

    id: str
    kind: TaskKind
    entity_id: str
    novel_id: str
    state: TaskState
    attempts: int
    error: str | None
    run_after: str
    change_seq: int
    created_at: str
    updated_at: str
    started_at: str | None
    finished_at: str | None
    duration_ms: int | None


class TaskList(BaseModel):
    """`GET /api/v1/tasks`: tasks in claim order, the match count and the cursor to poll from."""

    items: list[Task]
    total: int
    next_since: int
