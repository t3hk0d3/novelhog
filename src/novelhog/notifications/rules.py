"""Notification rules: pure functions from an event and its novel to a message (spec 6.6, 8.6)."""

from dataclasses import dataclass

from novelhog.domain.payloads import NovelView
from novelhog.repo.events import EventRow

_DEFAULT_ICON = "/icons/192.png"


@dataclass(frozen=True)
class NotificationMessage:
    """A channel-neutral notification; `topic` lets a newer message replace an undelivered one."""

    title: str
    body: str
    url: str
    icon: str
    topic: str


def _icon(novel: NovelView, public_url: str) -> str:
    base = public_url.rstrip("/")
    cover = novel.cover_url
    if cover is None:
        return f"{base}{_DEFAULT_ICON}"
    if "://" in cover or cover.startswith("//"):
        return cover
    return f"{base}/{cover.lstrip('/')}"


def _release_body(released: list[int]) -> str:
    count = len(released)
    noun = "chapter" if count == 1 else "chapters"
    return f"{count} new {noun} ({min(released)}–{max(released)})"


def _released(payload: dict[str, object]) -> list[int]:
    value = payload.get("released")
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, int)]


def match(
    event: EventRow, novel: NovelView | None, *, public_url: str
) -> NotificationMessage | None:
    """Return the message for `event`, or None when no rule matches.

    `novel` is the novel as it is now (None when it no longer exists); `followed` and `released`
    come from the event payload, as they were when the sync ran.
    """
    if novel is None or event.payload.get("deleted"):
        return None
    base = public_url.rstrip("/")
    title = novel.title or novel.host
    kind = event.kind
    if kind == "novel":
        released = _released(event.payload)
        if not released or event.payload.get("followed") is not True:
            return None
        return NotificationMessage(
            title=title,
            body=_release_body(released),
            url=f"{base}/novels/{novel.id}",
            icon=_icon(novel, public_url),
            topic=novel.id,
        )
    if kind == "export":
        filename = event.payload.get("filename")
        if (
            event.payload.get("state") != "ready"
            or event.payload.get("waited") is not True
            or not isinstance(filename, str)
        ):
            return None
        return NotificationMessage(
            title=title,
            body=f"{filename} is ready",
            url=f"{base}/novels/{novel.id}#exports",
            icon=_icon(novel, public_url),
            topic=novel.id,
        )
    return None
