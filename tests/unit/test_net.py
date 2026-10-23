"""Tests for the outbound HTTP guard."""

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import requests

from novelhog import net
from tests.fixtures.images.helpers import Server


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
            "::7f00:1",
            "::a00:1",
            "::ffff:0:7f00:1",
            "::ffff:0:a00:1",
            "::ffff:0:808:808",
            "fec0::1",
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


class TestCheckHost__OnPublicHost:
    def test_check_host__returns_the_resolved_addresses(self) -> None:
        assert net.check_host("a.test", allow_private=False, resolver=fixed("8.8.8.8")) == [
            "8.8.8.8"
        ]


class TestCheckHost__OnNonPublicHost:
    def test_check_host__raises_non_public_naming_the_host(self) -> None:
        with pytest.raises(net.NonPublic) as raised:
            net.check_host("a.test", allow_private=False, resolver=fixed("10.0.0.1"))
        assert raised.value.host == "a.test"

    def test_check_host__non_public_is_refused(self) -> None:
        assert issubclass(net.NonPublic, net.Refused)

    def test_check_host__returns_the_addresses_when_private_is_allowed(self) -> None:
        assert net.check_host("a.test", allow_private=True, resolver=fixed("10.0.0.1")) == [
            "10.0.0.1"
        ]


class TestCheckHost__OnUnresolvableHost:
    def test_check_host__default_resolver_raises_unresolvable(self) -> None:
        with pytest.raises(net.Unresolvable):
            net.check_host("no-such-host.invalid", allow_private=True)

    def test_check_host__no_address_raises_unresolvable(self) -> None:
        with pytest.raises(net.Unresolvable):
            net.check_host("a.test", allow_private=True, resolver=fixed())

    def test_check_host__unresolvable_is_refused(self) -> None:
        assert issubclass(net.Unresolvable, net.Refused)


class TestGuardedSession__PrivateHost:
    def test_get__raises_refused_before_connecting(self, http_server: Server) -> None:
        session = net.GuardedSession(allow_private=False, resolver=fixed("127.0.0.1"))
        with pytest.raises(net.Refused):
            session.get(http_server.url("/x", host="evil.test"))
        assert http_server.hits == []

    def test_get__refuses_when_any_address_is_private(self, http_server: Server) -> None:
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
    def test_get__connects(self, http_server: Server) -> None:
        http_server.routes["/ok"] = (200, {}, b"hi")
        session = net.GuardedSession(allow_private=True, resolver=fixed("127.0.0.1"))
        response = session.get(http_server.url("/ok", host="site.test"))
        assert response.content == b"hi"

    def test_get__keeps_host_header_and_resolves_once(self, http_server: Server) -> None:
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
    @pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
    @pytest.mark.parametrize("kind", ["absolute", "scheme_relative"])
    def test_get__redirect_to_private_after_public_hop_is_refused(
        self, status: int, kind: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Hop one is treated as public (127.0.0.1 is the only reachable server); hop two is real.
        server = Server()
        server.start()
        try:
            target = (
                server.url("/in", host="inner.test")
                if kind == "absolute"
                else f"//inner.test:{server.port}/in"
            )
            server.routes["/r"] = (status, {"Location": target}, b"")
            server.routes["/in"] = (200, {}, b"secret")
            real = net.is_public
            monkeypatch.setattr(net, "is_public", lambda a: True if not server.hits else real(a))
            session = net.GuardedSession(allow_private=False, resolver=fixed("127.0.0.1"))
            with pytest.raises(net.Refused):
                session.get(server.url("/r", host="outer.test"))
            assert [p for p, _ in server.hits] == ["/r"]
        finally:
            server.stop()

    def test_get__same_host_relative_redirect_reuses_the_checked_connection(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Keep-alive: the second hop goes over the connection that was already checked.
        server = Server()
        server.start()
        try:
            server.routes["/r"] = (302, {"Location": "/in"}, b"")
            server.routes["/in"] = (200, {}, b"ok")
            session = net.GuardedSession(allow_private=True, resolver=fixed("127.0.0.1"))
            assert session.get(server.url("/r", host="outer.test")).content == b"ok"
            assert [p for p, _ in server.hits] == ["/r", "/in"]
        finally:
            server.stop()

    def test_get__more_than_five_redirects_raises_too_many(self, http_server: Server) -> None:
        for i in range(10):
            http_server.routes[f"/{i}"] = (302, {"Location": f"/{i + 1}"}, b"")
        session = net.GuardedSession(allow_private=True, resolver=fixed("127.0.0.1"))
        with pytest.raises(requests.TooManyRedirects):
            session.get(http_server.url("/0", host="site.test"))

    def test_get__five_redirects_are_followed(self, http_server: Server) -> None:
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


class TestGuardedSession__Proxies:
    def test_get__session_proxies_raise_refused(self, http_server: Server) -> None:
        session = net.GuardedSession(allow_private=True, resolver=fixed("127.0.0.1"))
        session.proxies = {"http": http_server.url("/")}
        with pytest.raises(net.Refused):
            session.get("http://site.test/x")
        assert http_server.hits == []

    def test_get__proxies_argument_raises_refused(self, http_server: Server) -> None:
        session = net.GuardedSession(allow_private=True, resolver=fixed("127.0.0.1"))
        with pytest.raises(net.Refused):
            session.get("http://site.test/x", proxies={"http": http_server.url("/")})
        assert http_server.hits == []


class TestGuardedSession__Tls:
    def test_get__verifies_certificate_against_original_hostname(
        self, tls_server: Server, tls_files: tuple[Path, Path]
    ) -> None:
        tls_server.routes["/ok"] = (200, {}, b"secure")
        session = net.GuardedSession(allow_private=True, resolver=fixed("127.0.0.1"))
        response = session.get(tls_server.url("/ok", host="site.test"), verify=str(tls_files[0]))
        assert response.content == b"secure"

    def test_get__sends_original_hostname_as_sni(
        self, tls_server: Server, tls_files: tuple[Path, Path]
    ) -> None:
        tls_server.routes["/ok"] = (200, {}, b"secure")
        session = net.GuardedSession(allow_private=True, resolver=fixed("127.0.0.1"))
        session.get(tls_server.url("/ok", host="site.test"), verify=str(tls_files[0]))
        assert tls_server.sni == ["site.test"]
        assert tls_server.hits == [("/ok", f"site.test:{tls_server.port}")]

    def test_get__hostname_mismatch_fails(
        self, tls_server: Server, tls_files: tuple[Path, Path]
    ) -> None:
        session = net.GuardedSession(allow_private=True, resolver=fixed("127.0.0.1"))
        with pytest.raises(requests.exceptions.SSLError):
            session.get(tls_server.url("/ok", host="other.test"), verify=str(tls_files[0]))
        assert tls_server.hits == []

    def test_get__untrusted_certificate_fails(self, tls_server: Server) -> None:
        session = net.GuardedSession(allow_private=True, resolver=fixed("127.0.0.1"))
        with pytest.raises(requests.exceptions.SSLError):
            session.get(tls_server.url("/ok", host="site.test"))
