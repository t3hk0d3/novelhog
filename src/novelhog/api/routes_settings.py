"""`GET /api/v1/settings` (spec section 5.6)."""

from typing import Annotated

from fastapi import APIRouter, Depends

from novelhog import vapid
from novelhog.api.deps import get_repo, get_settings
from novelhog.api.schemas import (
    ErrorBody,
    NotificationChannelsOut,
    NotificationsOut,
    SettingsOut,
    WebpushChannelOut,
)
from novelhog.config import Settings
from novelhog.repo import Repo

router = APIRouter()


@router.get(
    "/settings",
    operation_id="get_settings",
    response_model=SettingsOut,
    responses={403: {"model": ErrorBody}},
)
def get_server_settings(
    settings: Annotated[Settings, Depends(get_settings)],
    repo: Annotated[Repo, Depends(get_repo)],
) -> SettingsOut:
    """Return the server settings the UI needs at startup; never the private VAPID key."""
    keys = vapid.load(repo) if settings.webpush_enabled else None
    webpush = WebpushChannelOut(
        enabled=keys is not None,
        vapid_public_key=keys.public_key if keys is not None else None,
    )
    return SettingsOut(
        notifications=NotificationsOut(channels=NotificationChannelsOut(webpush=webpush)),
        static_base_url=settings.static_base_url,
        refresh_min_interval=settings.refresh_min_interval,
        refresh_max_interval=settings.refresh_max_interval,
    )
