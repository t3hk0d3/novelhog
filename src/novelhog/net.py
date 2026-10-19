"""Outbound HTTP guard (spec section 6.8).

``GuardedSession`` resolves a host once per connection, checks every resolved
address and connects to the checked IP itself, so a second DNS lookup (DNS
rebinding) can never swap in a private address. The URL host stays the
connection's host, so the Host header, TLS SNI and certificate verification use
the original name. Redirects go through the same path because every hop opens
its connection through the guard.
"""

import ipaddress
import socket
from collections.abc import Callable
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.connection import HTTPConnection, HTTPSConnection
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool
from urllib3.exceptions import ConnectTimeoutError, NewConnectionError
from urllib3.poolmanager import PoolManager

CONNECT_TIMEOUT = 10.0
READ_TIMEOUT = 30.0
MAX_REDIRECTS = 5

_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_NAT64_LOCAL = ipaddress.ip_network("64:ff9b:1::/48")
_SIXTOFOUR = ipaddress.ip_network("2002::/16")
_TEREDO = ipaddress.ip_network("2001::/32")
_SIIT = ipaddress.ip_network("::ffff:0:0:0/96")
_V4_COMPATIBLE = ipaddress.ip_network("::/96")
_SITE_LOCAL = ipaddress.ip_network("fec0::/10")


class Refused(Exception):  # noqa: N818 - name fixed by the task contract
    """The guard refused an outbound request (private address or bad scheme)."""


def _embedded_v4(address: ipaddress.IPv6Address) -> list[ipaddress.IPv4Address]:
    """IPv4 addresses hidden inside an IPv6 address (mapped, NAT64, 6to4, Teredo)."""
    found: list[ipaddress.IPv4Address] = []
    if address.ipv4_mapped is not None:
        found.append(address.ipv4_mapped)
    if address in _NAT64:
        found.append(ipaddress.IPv4Address(int(address) & 0xFFFFFFFF))
    if address.sixtofour is not None:
        found.append(address.sixtofour)
    if address in _SIIT:
        found.append(ipaddress.IPv4Address(int(address) & 0xFFFFFFFF))
    if address in _TEREDO:
        found.append(ipaddress.IPv4Address((int(address) & 0xFFFFFFFF) ^ 0xFFFFFFFF))
    return found


def is_public(address: str) -> bool:
    """True when ``address`` is a globally routable unicast address."""
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        if ip in _NAT64_LOCAL or ip in _V4_COMPATIBLE or ip in _SITE_LOCAL:
            return False
        if any(not is_public(str(v4)) for v4 in _embedded_v4(ip)):
            return False
    return ip.is_global and not ip.is_multicast


def default_resolver(host: str) -> list[str]:
    """Resolve ``host`` to all its IP addresses (also parses literal and odd IP forms)."""
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise Refused(f"cannot resolve {host}") from exc  # noqa: TRY003
    return list(dict.fromkeys(str(info[4][0]) for info in infos))


def _check(host: str, resolver: Callable[[str], list[str]], allow_private: bool) -> list[str]:
    """Resolve once and return the checked addresses; refuse if any is not public."""
    bare = host.strip("[]")
    try:
        ipaddress.ip_address(bare)
        addresses = [bare]
    except ValueError:
        addresses = resolver(bare)
    if not addresses:
        raise Refused(f"{host} resolved to no address")  # noqa: TRY003
    if not allow_private:
        for address in addresses:
            if not is_public(address):
                raise Refused(f"{host} resolves to a non-public address")  # noqa: TRY003
    return addresses


class _GuardMixin:
    """Connection mixin: resolve, check and connect to the checked IP."""

    _resolver: Callable[[str], list[str]]
    _allow_private: bool

    def _new_conn(self) -> socket.socket:
        conn: Any = self
        original = conn._dns_host
        addresses = _check(original, self._resolver, self._allow_private)
        last: Exception | None = None
        for address in addresses:
            conn._dns_host = address
            try:
                return HTTPConnection._new_conn(conn)
            except (NewConnectionError, ConnectTimeoutError) as exc:
                last = exc
            finally:
                conn._dns_host = original  # restore: SNI and verification use the name
        assert last is not None  # noqa: S101
        raise last


def _pool_classes(
    resolver: Callable[[str], list[str]], allow_private: bool
) -> dict[str, type[HTTPConnectionPool]]:
    class Http(_GuardMixin, HTTPConnection):
        pass

    class Https(_GuardMixin, HTTPSConnection):
        pass

    for cls in (Http, Https):
        cls._resolver = staticmethod(resolver)
        cls._allow_private = allow_private

    class HttpPool(HTTPConnectionPool):
        ConnectionCls = Http

    class HttpsPool(HTTPSConnectionPool):
        ConnectionCls = Https

    return {"http": HttpPool, "https": HttpsPool}


class _GuardedAdapter(HTTPAdapter):
    def __init__(self, resolver: Callable[[str], list[str]], allow_private: bool) -> None:
        self._guard_classes = _pool_classes(resolver, allow_private)
        super().__init__(max_retries=0)

    def init_poolmanager(
        self, connections: int, maxsize: int, block: bool = False, **pool_kwargs: Any
    ) -> None:
        manager = PoolManager(num_pools=connections, maxsize=maxsize, block=block, **pool_kwargs)
        manager.pool_classes_by_scheme = self._guard_classes
        self.poolmanager = manager

    def proxy_manager_for(self, proxy: str, **proxy_kwargs: Any) -> Any:
        raise Refused("proxies are not allowed")  # noqa: TRY003


class GuardedSession(requests.Session):
    """A ``requests`` session that refuses private targets, bad schemes and long redirect chains."""

    def __init__(
        self,
        *,
        allow_private: bool,
        resolver: Callable[[str], list[str]] = default_resolver,
    ) -> None:
        super().__init__()
        self.trust_env = False  # environment proxies would bypass the guard
        self.max_redirects = MAX_REDIRECTS
        adapter = _GuardedAdapter(resolver, allow_private)
        self.mount("http://", adapter)
        self.mount("https://", adapter)

    def send(self, request: requests.PreparedRequest, **kwargs: Any) -> requests.Response:
        scheme = (request.url or "").split(":", 1)[0].lower()
        if scheme not in ("http", "https"):
            raise Refused(f"scheme {scheme!r} is not allowed")  # noqa: TRY003
        if kwargs.get("proxies"):
            raise Refused("proxies are not allowed")  # noqa: TRY003
        if kwargs.get("timeout") is None:
            kwargs["timeout"] = (CONNECT_TIMEOUT, READ_TIMEOUT)
        return super().send(request, **kwargs)
