"""URL normalisation and static URLs (spec sections 4 and 8.3)."""

from urllib.parse import quote, urlsplit, urlunsplit

from novelhog.domain.errors import ValidationFailed

_DEFAULT_PORTS = {"http": 80, "https": 443}


def _invalid(message: str) -> ValidationFailed:
    return ValidationFailed("invalid_url", message)


def normalize_url(url: str) -> str:
    """Return the canonical form of an http(s) URL; raise `ValidationFailed` if unacceptable."""
    try:
        parts = urlsplit(url.strip())
        port = parts.port
        host = parts.hostname
    except ValueError as exc:
        raise _invalid("The URL is malformed") from exc  # noqa: TRY003
    scheme = parts.scheme.lower()
    if scheme not in _DEFAULT_PORTS:
        raise _invalid("Only http and https URLs are supported")  # noqa: TRY003
    if port is not None and port != _DEFAULT_PORTS[scheme]:
        raise _invalid("Only the default port is supported")  # noqa: TRY003
    host = (host or "").removeprefix("www.")
    if not host:
        raise _invalid("The URL has no host")  # noqa: TRY003
    if ":" in host:
        host = f"[{host}]"
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((scheme, host, path, parts.query, ""))


def host_of(normalized_url: str) -> str:
    """Return the host of a normalised URL (the key into `hosts`)."""
    return urlsplit(normalized_url).netloc


def static_url(base: str, key: str) -> str:
    """Join the static base URL and a storage key, percent-encoding each key segment."""
    encoded = "/".join(quote(segment, safe="") for segment in key.split("/"))
    return f"{base.rstrip('/')}/{encoded}"
