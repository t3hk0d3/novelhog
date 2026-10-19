"""Tests for the outbound HTTP guard."""

from collections.abc import Callable
from typing import Any

import pytest
import requests

from novelhog import net
from tests.fixtures.images.conftest import Server, http_server  # noqa: F401


def fixed(*addresses: str) -> Callable[[str], list[str]]:
    return lambda host: list(addresses)


class TestIsPublic__Addresses:
    @pytest.mark.parametrize(
        "address",
        [
            "127.0.0.1",
            "::1",
            "10.1.2.3",
            "172.16.0.1",
            "172.31.255.255",
            "192.168.1.1",
            "169.254.169.254",
            "fe80::1",
            "fc00::1",
            "fd12::1",
            "0.0.0.0",  # noqa: S104
            "::",
            "224.0.0.1",
            "ff02::1",
            "::ffff:127.0.0.1",
            "::ffff:10.0.0.1",
            "64:ff9b::7f00:1",
            "64:ff9b:1::1",
            "2002:7f00:1::1",
            "100.64.0.1",
            "fe80::1%eth0",
            "not-an-ip",
        ],
    )
    def test_is_public__false_for_private_forms(self, address: str) -> None:
        assert net.is_public(address) is False

    @pytest.mark.parametrize("address", ["8.8.8.8", "93.184.216.34", "2606:4700::1111"])
    def test_is_public__true_for_public(self, address: str) -> None:
        assert net.is_public(address) is True

    def test_is_public__true_for_mapped_public(self) -> None:
        assert net.is_public("::ffff:8.8.8.8") is True


class TestGuardedSession__PrivateHost:
    def test_get__raises_refused_before_connecting(self, http_server: Server) -> None:  # noqa: F811
        session = net.GuardedSession(allow_private=False, resolver=fixed("127.0.0.1"))
        with pytest.raises(net.Refused):
            session.get(http_server.url("/x", host="evil.test"))
        assert http_server.hits == []

    def test_get__refuses_when_any_address_is_private(self, http_server: Server) -> None:  # noqa: F811
        session = net.GuardedSession(allow_private=False, resolver=fixed("8.8.8.8", "127.0.0.1"))
        with pytest.raises(net.Refused):
            session.get(http_server.url("/x", host="evil.test"))

    @pytest.mark.parametrize(
        "host", ["127.0.0.1", "[::1]", "[::ffff:127.0.0.1]", "2130706433", "0177.0.0.1"]
    )
    def test_get__refuses_literal_ip_forms(self, host: str) -> None:
        session = net.GuardedSession(
            allow_private=False, resolver=lambda h: net.default_resolver(h)
        )
        with pytest.raises(net.Refused):
            session.get(f"http://{host}:1/x")

    def test_get__refuses_unresolvable_host(self) -> None:
        def fail(host: str) -> list[str]:
            raise net.Refused("nope")

        session = net.GuardedSession(allow_private=False, resolver=fail)
        with pytest.raises(net.Refused):
            session.get("http://nowhere.test/")


class TestGuardedSession__AllowPrivate:
    def test_get__connects(self, http_server: Server) -> None:  # noqa: F811
        http_server.routes["/ok"] = (200, {}, b"hi")
        session = net.GuardedSession(allow_private=True, resolver=fixed("127.0.0.1"))
        response = session.get(http_server.url("/ok", host="site.test"))
        assert response.content == b"hi"

    def test_get__keeps_host_header_and_resolves_once(self, http_server: Server) -> None:  # noqa: F811
        http_server.routes["/ok"] = (200, {}, b"hi")
        calls: list[str] = []

        def resolver(host: str) -> list[str]:
            calls.append(host)
            return ["127.0.0.1"]

        session = net.GuardedSession(allow_private=True, resolver=resolver)
        session.get(http_server.url("/ok", host="site.test"))
        assert calls == ["site.test"]
        assert http_server.hits == [("/ok", f"site.test:{http_server.port}")]


class TestGuardedSession__Redirects:
    def test_get__redirect_to_private_raises_refused(self, http_server: Server) -> None:  # noqa: F811
        http_server.routes["/r"] = (
            302,
            {"Location": http_server.url("/in", host="inner.test")},
            b"",
        )
        http_server.routes["/in"] = (200, {}, b"secret")

        def resolver(host: str) -> list[str]:
            return ["8.8.8.8"] if host == "outer.test" else ["127.0.0.1"]

        # outer is public but unreachable here, so pin it to the server by allowing it via a
        # resolver that the guard sees as public only for the first hop.
        session = net.GuardedSession(allow_private=False, resolver=resolver)
        with pytest.raises((net.Refused, requests.ConnectionError)):
            session.get(http_server.url("/r", host="outer.test"), timeout=1)
        assert ("/in", f"inner.test:{http_server.port}") not in http_server.hits

    def test_get__redirect_to_private_after_public_hop_is_refused(self, monkeypatch: Any) -> None:
        # Public first hop is simulated by treating 127.0.0.1 as public for hop one only.
        server = Server()
        server.start()
        try:
            server.routes["/r"] = (302, {"Location": server.url("/in", host="inner.test")}, b"")
            server.routes["/in"] = (200, {}, b"secret")
            real = net.is_public
            monkeypatch.setattr(net, "is_public", lambda a: True if not server.hits else real(a))
            session = net.GuardedSession(allow_private=False, resolver=fixed("127.0.0.1"))
            with pytest.raises(net.Refused):
                session.get(server.url("/r", host="outer.test"))
            assert [p for p, _ in server.hits] == ["/r"]
        finally:
            server.stop()

    def test_get__more_than_five_redirects_raises_too_many(self, http_server: Server) -> None:  # noqa: F811
        for i in range(10):
            http_server.routes[f"/{i}"] = (302, {"Location": f"/{i + 1}"}, b"")
        session = net.GuardedSession(allow_private=True, resolver=fixed("127.0.0.1"))
        with pytest.raises(requests.TooManyRedirects):
            session.get(http_server.url("/0", host="site.test"))

    def test_get__five_redirects_are_followed(self, http_server: Server) -> None:  # noqa: F811
        for i in range(5):
            http_server.routes[f"/{i}"] = (302, {"Location": f"/{i + 1}"}, b"")
        http_server.routes["/5"] = (200, {}, b"end")
        session = net.GuardedSession(allow_private=True, resolver=fixed("127.0.0.1"))
        assert session.get(http_server.url("/0", host="site.test")).content == b"end"


class TestGuardedSession__Scheme:
    @pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://example.com/x", "gopher://a/"])
    def test_get__non_http_raises_refused(self, url: str) -> None:
        session = net.GuardedSession(allow_private=True, resolver=fixed("8.8.8.8"))
        with pytest.raises(net.Refused):
            session.get(url)


class TestGuardedSession__Timeouts:
    def test_send__uses_default_timeouts(self, monkeypatch: Any) -> None:
        seen: dict[str, Any] = {}

        def fake_send(self: Any, request: Any, **kwargs: Any) -> str:
            seen.update(kwargs)
            return "ok"

        monkeypatch.setattr(requests.Session, "send", fake_send)
        session = net.GuardedSession(allow_private=True, resolver=fixed("8.8.8.8"))
        session.get("http://site.test/")
        assert seen["timeout"] == (10.0, 30.0)

    def test_session__ignores_environment_proxies(self) -> None:
        assert net.GuardedSession(allow_private=False).trust_env is False
