"""Tests for URL normalisation and static URLs."""

from urllib.parse import unquote

import pytest

from novelhog.domain.errors import ValidationFailed
from novelhog.domain.urls import host_of, normalize_toc_url, normalize_url, static_url


class TestNormalizeUrl__OnValidUrl:
    def test_normalize_url__lowercases_scheme_and_host_and_strips_www(self) -> None:
        assert normalize_url("HTTPS://WWW.Example.COM/Path") == "https://example.com/Path"

    def test_normalize_url__drops_fragment_port_userinfo_and_trailing_slash(self) -> None:
        url = "http://user:pw@example.com:80/novel/42/#top"
        assert normalize_url(url) == "http://example.com/novel/42"

    def test_normalize_url__drops_default_https_port(self) -> None:
        assert normalize_url("https://example.com:443/a") == "https://example.com/a"

    def test_normalize_url__keeps_query(self) -> None:
        assert normalize_url("https://example.com/a/?id=3&b=x") == "https://example.com/a?id=3&b=x"

    def test_normalize_url__keeps_root_slash(self) -> None:
        assert normalize_url("https://example.com") == "https://example.com/"
        assert normalize_url("https://example.com/") == "https://example.com/"

    @pytest.mark.parametrize(
        "url",
        [
            "HTTPS://WWW.Example.COM:443/a/b/?q=1#frag",
            "http://u@example.com/x//",
            "https://example.com",
            "http://www.www.example.com/a",
            "http://www.example.com./a",
            "http://www.www.example.com.:8080/a",
            "http://EXAMPLE.com:80",
            "http://[::1]:8080/x/",
            "http://b\u00fccher.example/a",
            "http://192.168.0.1/a/",
            "http://\uff57\uff57\uff57.example.com/a",
            "http://www\u3002example\u3002com/a",
            "http://www.\uff57\uff57\uff57.example.com/a",
            "http://example.com\u3002/a",
        ],
    )
    def test_normalize_url__is_idempotent(self, url: str) -> None:
        once = normalize_url(url)
        assert normalize_url(once) == once


class TestNormalizeUrl__OnInvalidUrl:
    @pytest.mark.parametrize(
        "url",
        [
            "ftp://example.com/a",
            "example.com/a",
            "https://exa mple.com/a",
            "http://[fe80::1%25eth0]/a",
            "https://-bad.example/a",
            "https://bad_.example/a",
            "https://a..b/a",
            "https://www../a",
            "https://" + "a" * 64 + ".com/",
            "https:///a",
            "https://example.com:notaport/a",
            "",
        ],
    )
    def test_normalize_url__raises_invalid_url(self, url: str) -> None:
        with pytest.raises(ValidationFailed) as info:
            normalize_url(url)
        assert info.value.code == "invalid_url"


class TestNormalizeUrl__OnHost:
    def test_normalize_url__keeps_non_default_port(self) -> None:
        assert normalize_url("https://example.com:8080/a") == "https://example.com:8080/a"
        assert normalize_url("http://example.com:443/a") == "http://example.com:443/a"

    def test_normalize_url__strips_trailing_dot(self) -> None:
        assert normalize_url("https://Example.com./a") == "https://example.com/a"

    def test_normalize_url__strips_www_hidden_by_unicode_folding(self) -> None:
        assert normalize_url("http://\uff57\uff57\uff57.example.com/a") == "http://example.com/a"
        assert normalize_url("http://www\u3002example\u3002com/a") == "http://example.com/a"

    def test_normalize_url__strips_repeated_www(self) -> None:
        assert normalize_url("http://www.www.example.com/a") == "http://example.com/a"

    def test_normalize_url__accepts_ip_literals(self) -> None:
        assert normalize_url("http://127.0.0.1/a") == "http://127.0.0.1/a"
        assert normalize_url("http://[::1]/a") == "http://[::1]/a"


class TestNormalizeTocUrl:
    def test_normalize_toc_url__normalises(self) -> None:
        assert normalize_toc_url("HTTP://www.Example.com:80/n/") == "http://example.com/n"

    @pytest.mark.parametrize("url", ["https://example.com:8080/a", "http://example.com:443/a"])
    def test_normalize_toc_url__rejects_non_default_port(self, url: str) -> None:
        with pytest.raises(ValidationFailed) as info:
            normalize_toc_url(url)
        assert info.value.code == "invalid_url"


class TestHostOf:
    def test_host_of__returns_host(self) -> None:
        assert host_of(normalize_url("https://www.Example.com/a/b?x=1")) == "example.com"


class TestHostOf__OnPortAndIpv6:
    def test_host_of__excludes_port(self) -> None:
        assert host_of(normalize_url("http://example.com:8080/x")) == "example.com"

    def test_host_of__keeps_ipv6_brackets(self) -> None:
        assert host_of(normalize_url("http://[::1]:8080/x")) == "[::1]"


class TestStaticUrl__OnRelativeBase:
    def test_static_url__prefixes_base_and_encodes_segments(self) -> None:
        assert static_url("/static", "covers/NVx/a b.jpg") == "/static/covers/NVx/a%20b.jpg"


class TestStaticUrl__OnAbsoluteBase:
    @pytest.mark.parametrize("base", ["https://h.example/static", "https://h.example/static/"])
    def test_static_url__joins_with_one_slash(self, base: str) -> None:
        assert static_url(base, "a/b.epub") == "https://h.example/static/a/b.epub"


class TestStaticUrl__OnSpecialCharacters:
    @pytest.mark.parametrize("name", ["Re:Zero #2.epub", "100%.epub", "what?.epub", "\U0001f600 a"])
    def test_static_url__encodes_and_round_trips(self, name: str) -> None:
        url = static_url("/static", f"exports/NV1/EX1/{name}")
        tail = url.removeprefix("/static/")
        last = tail.split("/")[-1]
        assert not any(c in last for c in "#? ")
        assert unquote(tail) == f"exports/NV1/EX1/{name}"
        assert tail.count("/") == 3
