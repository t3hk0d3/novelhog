"""Subscriptions routes (spec section 5.6)."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Response

from novelhog import vapid
from novelhog.api.deps import get_repo, get_settings, path_id
from novelhog.api.schemas import ErrorBody, Subscription, WebpushSubscriptionCreate
from novelhog.config import Settings
from novelhog.domain import webpush
from novelhog.domain.errors import ServiceUnavailable
from novelhog.repo import Repo

router = APIRouter()

SubscriptionId = Annotated[str, Depends(path_id("SB", "subscription_id"))]
RepoDep = Annotated[Repo, Depends(get_repo)]
SettingsDep = Annotated[Settings, Depends(get_settings)]

_MAX_LABEL = 200
_NOT_FOUND: dict[int | str, dict[str, Any]] = {404: {"model": ErrorBody}}


@router.post(
    "/webpush/subscriptions",
    operation_id="create_webpush_subscription",
    response_model=Subscription,
    status_code=201,
    responses={
        200: {"model": Subscription, "description": "The endpoint was already subscribed"},
        422: {"model": ErrorBody},
        503: {"model": ErrorBody},
    },
)
def create_webpush_subscription(
    body: WebpushSubscriptionCreate,
    repo: RepoDep,
    settings: SettingsDep,
    response: Response,
    user_agent: Annotated[str | None, Header()] = None,
) -> Subscription:
    """Register a browser push subscription; an existing endpoint keeps its id."""
    if not settings.webpush_enabled or vapid.load(repo) is None:
        raise ServiceUnavailable("channel_disabled", "Web Push is not enabled")
    webpush.validate_endpoint(body.endpoint)
    label = user_agent[:_MAX_LABEL] if user_agent else None
    address: dict[str, object] = {
        "endpoint": body.endpoint,
        "keys": {"p256dh": body.keys.p256dh, "auth": body.keys.auth},
    }
    view, created = repo.subscriptions.upsert("webpush", address, body.endpoint, label)
    if not created:
        response.status_code = 200
    return Subscription.model_validate(view.to_json())


@router.get("/subscriptions", operation_id="list_subscriptions", response_model=list[Subscription])
def list_subscriptions(repo: RepoDep) -> list[Subscription]:
    """Return every subscription by creation time, without addresses."""
    return [Subscription.model_validate(v.to_json()) for v in repo.subscriptions.list()]


@router.delete(
    "/subscriptions/{subscription_id}",
    operation_id="delete_subscription",
    status_code=204,
    response_class=Response,
    responses=_NOT_FOUND,
)
def delete_subscription(subscription_id: SubscriptionId, repo: RepoDep) -> Response:
    """Remove a subscription."""
    repo.subscriptions.delete(subscription_id)
    return Response(status_code=204)
