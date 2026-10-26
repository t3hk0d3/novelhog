"""Tests for the in-memory Cloudflare clearance cache."""

from collections.abc import Iterator
from typing import Any

import pytest
from structlog.testing import capture_logs

from novelhog.clearance import ClearanceCache
from tests.fixtures.fake_flaresolverr import FakeFlareSolverr

UA = "Mozilla/5.0 Chrome/142.0.0.0"
UNUSED = "http://127.0.0.1:9"  # FlareSolverr's address where no test asks it anything
PAGE = "https://site.test/a.png"


@pytest.fixture
def flaresolverr() -> Iterator[FakeFlareSolverr]:
    with FakeFlareSolverr({PAGE: "<html><img></html>"}) as fake:
        yield fake


def cookie(name: str, domain: str = ".site.test", **extra: Any) -> dict[str, Any]:
    return {"name": name, "value": f"{name}-value", "domain": domain, "path": "/", **extra}


class Clock:
    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class TestClearanceCacheRecord__WithClearance:
    def test_record__keeps_user_agent_and_domain(self) -> None:
        clearance = ClearanceCache(UNUSED).record(UA, [cookie("cf_clearance")])
        assert clearance is not None
        assert clearance.user_agent == UA
        assert clearance.domain == "site.test"

    def test_record__keeps_cookies_of_the_domain_and_its_parents(self) -> None:
        cookies = [
            cookie("cf_clearance", ".www.site.test"),
            cookie("__cf_bm", ".site.test"),
            cookie("other", ".elsewhere.test"),
        ]
        clearance = ClearanceCache(UNUSED).record(UA, cookies)
        assert clearance is not None
        assert sorted(c.name for c in clearance.cookies) == ["__cf_bm", "cf_clearance"]

    def test_record__takes_the_clearance_cookies_expiry(self) -> None:
        clearance = ClearanceCache(UNUSED).record(UA, [cookie("cf_clearance", expiry=2_000)])
        assert clearance is not None
        assert clearance.expires_at == 2_000.0

    def test_record__session_cookie_has_no_expiry(self) -> None:
        clearance = ClearanceCache(UNUSED).record(UA, [cookie("cf_clearance", expires=-1)])
        assert clearance is not None
        assert clearance.expires_at is None

    def test_record__replaces_the_previous_clearance_of_the_domain(self) -> None:
        cache = ClearanceCache(UNUSED)
        cache.record(UA, [cookie("cf_clearance")])
        newer = cache.record("Other UA", [cookie("cf_clearance")])
        assert cache.for_url("https://site.test/a.png") is newer


class TestClearanceCacheRecord__WithoutClearance:
    def test_record__without_cf_clearance_keeps_nothing(self) -> None:
        cache = ClearanceCache(UNUSED)
        assert cache.record(UA, [cookie("__cf_bm")]) is None
        assert cache.for_url("https://site.test/a.png") is None

    def test_record__without_user_agent_keeps_nothing(self) -> None:
        cache = ClearanceCache(UNUSED)
        assert cache.record("", [cookie("cf_clearance")]) is None
        assert cache.for_url("https://site.test/a.png") is None


class TestClearanceCacheForUrl:
    def test_for_url__matches_the_domain_and_its_subdomains(self) -> None:
        cache = ClearanceCache(UNUSED)
        clearance = cache.record(UA, [cookie("cf_clearance")])
        assert cache.for_url("https://site.test/a.png") is clearance
        assert cache.for_url("https://cdn.site.test/a.png") is clearance

    def test_for_url__other_host_gets_none(self) -> None:
        cache = ClearanceCache(UNUSED)
        cache.record(UA, [cookie("cf_clearance")])
        assert cache.for_url("https://notsite.test/a.png") is None
        assert cache.for_url("https://site.test.evil/a.png") is None

    def test_for_url__the_most_specific_domain_wins(self) -> None:
        cache = ClearanceCache(UNUSED)
        cache.record(UA, [cookie("cf_clearance", ".site.test")])
        cdn = cache.record(UA, [cookie("cf_clearance", ".cdn.site.test")])
        assert cache.for_url("https://cdn.site.test/a.png") is cdn

    def test_for_url__expired_clearance_is_dropped(self) -> None:
        clock = Clock()
        cache = ClearanceCache(UNUSED, now=clock)
        cache.record(UA, [cookie("cf_clearance", expiry=1_500)])
        clock.now = 1_500.0
        assert cache.for_url("https://site.test/a.png") is None


class TestClearanceCacheForget:
    def test_forget__drops_the_clearance(self) -> None:
        cache = ClearanceCache(UNUSED)
        clearance = cache.record(UA, [cookie("cf_clearance")])
        assert clearance is not None
        cache.forget(clearance)
        assert cache.for_url("https://site.test/a.png") is None

    def test_forget__keeps_a_newer_clearance(self) -> None:
        cache = ClearanceCache(UNUSED)
        old = cache.record(UA, [cookie("cf_clearance")])
        newer = cache.record(UA, [cookie("cf_clearance")])
        assert old is not None
        cache.forget(old)
        assert cache.for_url("https://site.test/a.png") is newer


class TestClearanceCacheSolve:
    def test_solve__asks_flaresolverr_for_the_url_and_keeps_its_clearance(
        self, flaresolverr: FakeFlareSolverr
    ) -> None:
        flaresolverr.solution = {"userAgent": UA, "cookies": [cookie("cf_clearance")]}
        cache = ClearanceCache(flaresolverr.url)
        clearance = cache.solve(PAGE)
        assert flaresolverr.requested_urls == [PAGE]
        assert clearance is not None
        assert clearance.user_agent == UA
        assert cache.for_url(PAGE) is clearance

    def test_solve__without_cf_clearance_gives_none_and_logs(
        self, flaresolverr: FakeFlareSolverr
    ) -> None:
        flaresolverr.solution = {"userAgent": UA, "cookies": [cookie("__cf_bm")]}
        cache = ClearanceCache(flaresolverr.url)
        with capture_logs() as logs:
            assert cache.solve(PAGE) is None
        assert [e["event"] for e in logs] == ["clearance_solve_failed"]
        assert cache.for_url(PAGE) is None

    def test_solve__failed_solve_gives_none(self, flaresolverr: FakeFlareSolverr) -> None:
        cache = ClearanceCache(flaresolverr.url)
        with capture_logs() as logs:
            assert cache.solve("https://site.test/unknown.png") is None
        assert [e["event"] for e in logs] == ["clearance_solve_failed"]

    def test_solve__flaresolverr_down_gives_none(self) -> None:
        with capture_logs() as logs:
            assert ClearanceCache(UNUSED, timeout=1).solve(PAGE) is None
        assert [e["event"] for e in logs] == ["clearance_solve_failed"]
