"""Tests for URL normalisation and static URLs."""

from urllib.parse import unquote

import pytest

from novelhog.domain.errors import ValidationFailed
from novelhog.domain.urls import host_of, normalize_url, static_url


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
            "https://example.com:8080/a",
            "http://example.com:443/a",
            "https:///a",
            "https://example.com:notaport/a",
            "",
        ],
    )
    def test_normalize_url__raises_invalid_url(self, url: str) -> None:
        with pytest.raises(ValidationFailed) as info:
            normalize_url(url)
        assert info.value.code == "invalid_url"


class TestHostOf:
    def test_host_of__returns_host(self) -> None:
        assert host_of(normalize_url("https://www.Example.com/a/b?x=1")) == "example.com"


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
