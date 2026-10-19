"""Runtime configuration read from NOVELHOG_ environment variables (spec section 9)."""

import os
import socket
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

_PREFIX = "NOVELHOG_"
_TRUE = frozenset({"true", "1"})
_FALSE = frozenset({"false", "0"})


@dataclass(frozen=True)
class Settings:
    """Immutable settings for the API, the worker and the CLI."""

    data_dir: Path
    database_url: str
    public_url: str | None
    vapid_subject: str | None
    allowed_hosts: tuple[str, ...]
    flaresolverr_url: str
    scraper_dir: Path
    static_backend: Literal["local"]
    static_base_url: str
    refresh_min_interval: int
    refresh_max_interval: int
    batch_size: int
    claim_ttl: int
    idle_sleep: int
    request_delay: float
    max_toc_pages: int
    max_image_bytes: int
    max_image_pixels: int
    image_max_dimension: int
    allow_private_fetch: bool
    worker_id: str
    http_port: int

    @property
    def static_dir(self) -> Path:
        """Directory holding served static files."""
        return self.data_dir / "static"

    @property
    def webpush_enabled(self) -> bool:
        """Web Push works only when both the public URL and the VAPID subject are set."""
        return self.public_url is not None and self.vapid_subject is not None


def _get(env: Mapping[str, str], name: str) -> str | None:
    """Return the variable's value, treating an empty value as unset."""
    return env.get(_PREFIX + name) or None


def _text(env: Mapping[str, str], name: str, default: str) -> str:
    raw = _get(env, name)
    return default if raw is None else raw


def _optional(env: Mapping[str, str], name: str) -> str | None:
    return _get(env, name)


def _int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = _get(env, name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        msg = f"{_PREFIX}{name} must be an integer, got {raw!r}"
        raise ValueError(msg) from None


def _float(env: Mapping[str, str], name: str, default: float) -> float:
    raw = _get(env, name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        msg = f"{_PREFIX}{name} must be a number, got {raw!r}"
        raise ValueError(msg) from None


def _bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = _get(env, name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    msg = f"{_PREFIX}{name} must be true, false, 1 or 0, got {raw!r}"
    raise ValueError(msg)


def load(env: Mapping[str, str] = os.environ) -> Settings:
    """Build Settings from `env`, applying the spec section 9 defaults."""
    data_dir = Path(_text(env, "DATA_DIR", "/data"))
    backend = _text(env, "STATIC_BACKEND", "local")
    if backend != "local":
        msg = f"{_PREFIX}STATIC_BACKEND must be 'local', got {backend!r}"
        raise ValueError(msg)
    hosts = tuple(
        h.strip().lower() for h in _text(env, "ALLOWED_HOSTS", "").split(",") if h.strip()
    )
    return Settings(
        data_dir=data_dir,
        database_url=_text(env, "DATABASE_URL", f"sqlite:///{data_dir / 'db' / 'app.db'}"),
        public_url=_optional(env, "PUBLIC_URL"),
        vapid_subject=_optional(env, "VAPID_SUBJECT"),
        allowed_hosts=hosts,
        flaresolverr_url=_text(env, "FLARESOLVERR_URL", "http://flaresolverr:8191"),
        scraper_dir=Path(_text(env, "SCRAPER_DIR", "/var/lib/novelhog/scraper")),
        static_backend="local",
        static_base_url=_text(env, "STATIC_BASE_URL", "/static"),
        refresh_min_interval=_int(env, "REFRESH_MIN_INTERVAL", 21600),
        refresh_max_interval=_int(env, "REFRESH_MAX_INTERVAL", 2592000),
        batch_size=_int(env, "BATCH_SIZE", 10),
        claim_ttl=_int(env, "CLAIM_TTL", 300),
        idle_sleep=_int(env, "IDLE_SLEEP", 30),
        request_delay=_float(env, "REQUEST_DELAY", 1.0),
        max_toc_pages=_int(env, "MAX_TOC_PAGES", 1000),
        max_image_bytes=_int(env, "MAX_IMAGE_BYTES", 2097152),
        max_image_pixels=_int(env, "MAX_IMAGE_PIXELS", 40000000),
        image_max_dimension=_int(env, "IMAGE_MAX_DIMENSION", 1600),
        allow_private_fetch=_bool(env, "ALLOW_PRIVATE_FETCH", False),
        worker_id=_text(env, "WORKER_ID", f"{socket.gethostname()}:{os.getpid()}"),
        http_port=_int(env, "HTTP_PORT", 8080),
    )
