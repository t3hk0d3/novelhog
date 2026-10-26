"""Web Push enrolment rules: known push services and subscription keys (spec section 5.6)."""

import base64
import binascii
import re
from urllib.parse import urlsplit

from novelhog.domain.errors import ValidationFailed

PUSH_HOSTS = frozenset({"fcm.googleapis.com", "updates.push.services.mozilla.com"})
PUSH_HOST_SUFFIXES = (".push.apple.com", ".notify.windows.com")
_APPLE_HOST = "web.push.apple.com"

P256DH_BYTES = 65
AUTH_BYTES = 16
_LABEL = re.compile(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?")
_FORBIDDEN_CHARS = re.compile(r"[\\\s\x00-\x1f\x7f]")
_URL_SAFE = re.compile(r"[A-Za-z0-9_-]+={0,2}")


def validate_endpoint(endpoint: str) -> None:
    """Raise `ValidationFailed("invalid_endpoint")` unless `endpoint` is an https URL on a known
    push service host (default port only, no credentials)."""
    if _FORBIDDEN_CHARS.search(endpoint):
        raise _invalid_endpoint()
    try:
        parts = urlsplit(endpoint)
        host = parts.hostname
        port = parts.port
    except ValueError:
        host, port, parts = None, None, urlsplit("")
    known = (
        host is not None
        and _is_strict_hostname(host)
        and (host in PUSH_HOSTS or host == _APPLE_HOST or host.endswith(PUSH_HOST_SUFFIXES))
    )
    if (
        parts.scheme != "https"
        or not known
        or port not in (None, 443)
        or parts.username is not None
        or parts.password is not None
        or "%" in parts.netloc
    ):
        raise _invalid_endpoint()


def _invalid_endpoint() -> ValidationFailed:
    return ValidationFailed(
        "invalid_endpoint", "The endpoint must be an https URL of a known push service"
    )


def _is_strict_hostname(host: str) -> bool:
    """True for a lowercase ASCII DNS name of at least two non-empty labels."""
    labels = host.split(".")
    return len(labels) >= 2 and all(len(x) <= 63 and _LABEL.fullmatch(x) for x in labels)


def decode_key(value: str) -> bytes:
    """Decode URL-safe base64 (padding optional); raise `ValueError` if it is not."""
    if not _URL_SAFE.fullmatch(value):
        msg = "not URL-safe base64"
        raise ValueError(msg)
    bare = value.rstrip("=")
    if len(bare) % 4 == 1:
        msg = "not URL-safe base64"
        raise ValueError(msg)
    try:
        return base64.urlsafe_b64decode(bare + "=" * (-len(bare) % 4))
    except (binascii.Error, ValueError) as error:  # pragma: no cover - guarded above
        raise ValueError(str(error)) from None


def check_p256dh(value: str) -> str:
    """Return `value` if it is the URL-safe base64 of an uncompressed P-256 point."""
    raw = decode_key(value)
    if len(raw) != P256DH_BYTES or raw[0] != 0x04:
        msg = "p256dh must be a 65-byte uncompressed P-256 point"
        raise ValueError(msg)
    return value


def check_auth(value: str) -> str:
    """Return `value` if it is the URL-safe base64 of a 16-byte auth secret."""
    if len(decode_key(value)) != AUTH_BYTES:
        msg = "auth must be 16 bytes"
        raise ValueError(msg)
    return value
