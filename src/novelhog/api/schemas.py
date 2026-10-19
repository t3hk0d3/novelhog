"""Pydantic models of the API wire format (spec section 5).

Names match the schema names the web client uses. Models are input-only or output-only so the
OpenAPI document holds exactly one schema per name.
"""

from pydantic import BaseModel


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
