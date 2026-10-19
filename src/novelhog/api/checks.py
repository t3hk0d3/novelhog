"""Host and origin checks (spec section 5.9)."""

import ipaddress
from urllib.parse import urlsplit

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

_UNSAFE_METHODS = frozenset({"POST", "PATCH", "DELETE"})
_DEFAULT_PORTS = {"http": 80, "https": 443}


def _split(netloc: str) -> tuple[str | None, int | None]:
    """Return the lower-cased host (no brackets) and the port of `host[:port]`, or Nones."""
    try:
        parts = urlsplit("//" + netloc)
        host, port = parts.hostname, parts.port
    except ValueError:
        return None, None
    else:
        return host, port


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def _forbidden(code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=403, content={"error": {"code": code, "message": message}})


class OriginChecks:
    """ASGI middleware: rejects foreign Host headers and cross-origin unsafe requests."""

    def __init__(self, app: ASGIApp, *, public_host: str | None, allowed_hosts: tuple[str, ...]):
        self._app = app
        self._allowed = frozenset(
            {"localhost", *allowed_hosts, *([public_host] if public_host else [])}
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        headers = {k.decode("latin-1"): v.decode("latin-1") for k, v in scope["headers"]}
        host_header = headers.get("host", "")
        host, port = _split(host_header)
        if host is None or not (_is_ip(host) or host in self._allowed):
            response = _forbidden("host_not_allowed", "Host is not allowed")
            await response(scope, receive, send)
            return
        origin = headers.get("origin")
        if scope["method"] in _UNSAFE_METHODS and origin is not None:
            if not self._same_origin(origin, host, port):
                response = _forbidden("cross_origin", "Cross-origin request refused")
                await response(scope, receive, send)
                return
        await self._app(scope, receive, send)

    @staticmethod
    def _same_origin(origin: str, host: str, port: int | None) -> bool:
        try:
            parts = urlsplit(origin)
            origin_host, origin_port = parts.hostname, parts.port
        except ValueError:
            return False
        if origin_host is None or origin_host != host:
            return False
        if origin_port is None:
            origin_port = _DEFAULT_PORTS.get(parts.scheme)
        if port is None:
            return origin_port == _DEFAULT_PORTS.get(parts.scheme)
        return origin_port == port
