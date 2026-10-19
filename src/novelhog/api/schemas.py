"""Pydantic models of the API wire format (spec section 5).

Names match the schema names the web client uses. Models are input-only or output-only so the
OpenAPI document holds exactly one schema per name (request models are named `...Create`,
`...Patch`).
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict


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
    """A supported host (`GET /api/v1/hosts`)."""

    host: str


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
