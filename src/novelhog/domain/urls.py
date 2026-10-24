"""URL normalisation and static URLs (spec sections 4 and 8.3)."""

import ipaddress
import re
from urllib.parse import quote, urlsplit, urlunsplit

from novelhog.domain.errors import ValidationFailed

_DEFAULT_PORTS = {"http": 80, "https": 443}


MAX_URL_LENGTH = 2048
"""Longest normalised URL accepted; keeps the unique index entries within PostgreSQL's limit."""


class UrlTooLong(ValidationFailed):
    """A normalised URL is longer than `MAX_URL_LENGTH` (code `invalid_url`)."""


def _invalid(message: str) -> ValidationFailed:
    return ValidationFailed("invalid_url", message)


_LABEL = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?")
_MAX_HOST_LENGTH = 253


def _clean_host(host: str) -> str:
    """Return the canonical host (lower case, punycode, no `www.`, no trailing dot) or raise."""
    if ":" in host:
        if "%" in host:
            raise _invalid("The URL host is not valid")  # noqa: TRY003
        try:
            return f"[{ipaddress.IPv6Address(host)}]"
        except ValueError as exc:
            raise _invalid("The URL host is not valid") from exc  # noqa: TRY003
    try:
        host = host.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise _invalid("The URL host is not valid") from exc  # noqa: TRY003
    host = host.removesuffix(".")
    while host.startswith("www."):
        host = host.removeprefix("www.")
    if host.replace(".", "").isdigit():
        try:
            return str(ipaddress.IPv4Address(host))
        except ValueError as exc:
            raise _invalid("The URL host is not valid") from exc  # noqa: TRY003
    labels = host.split(".")
    if len(host) > _MAX_HOST_LENGTH or not all(_LABEL.fullmatch(label) for label in labels):
        raise _invalid("The URL host is not valid")  # noqa: TRY003
    return host


def normalize_url(url: str) -> str:
    """Return the canonical form of an http(s) URL; raise `ValidationFailed` if unacceptable.

    Leading `www.` labels are all stripped so that normalising twice changes nothing.
    """
    try:
        parts = urlsplit(url.strip())
        port = parts.port
        host = parts.hostname
    except ValueError as exc:
        raise _invalid("The URL is malformed") from exc  # noqa: TRY003
    scheme = parts.scheme.lower()
    if scheme not in _DEFAULT_PORTS:
        raise _invalid("Only http and https URLs are supported")  # noqa: TRY003
    if not host:
        raise _invalid("The URL has no host")  # noqa: TRY003
    host = _clean_host(host)
    if port is not None and port != _DEFAULT_PORTS[scheme]:
        host = f"{host}:{port}"
    path = parts.path.rstrip("/") or "/"
    normalized = urlunsplit((scheme, host, path, parts.query, ""))
    if len(normalized) > MAX_URL_LENGTH:
        msg = f"The URL is longer than {MAX_URL_LENGTH} characters"
        raise UrlTooLong("invalid_url", msg)
    return normalized


def normalize_toc_url(url: str) -> str:
    """Normalise a novel's table-of-contents URL, which must use the scheme's default port."""
    normalized = normalize_url(url)
    parts = urlsplit(normalized)
    if parts.port is not None:
        raise _invalid("Only the default port is supported")  # noqa: TRY003
    return normalized


def host_of(normalized_url: str) -> str:
    """Return the host of a normalised URL (the key into `hosts`)."""
    host = urlsplit(normalized_url).hostname or ""
    return f"[{host}]" if ":" in host else host


def static_url(base: str, key: str) -> str:
    """Join the static base URL and a storage key, percent-encoding each key segment."""
    encoded = "/".join(quote(segment, safe="") for segment in key.split("/"))
    return f"{base.rstrip('/')}/{encoded}"
