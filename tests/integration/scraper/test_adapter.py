"""Scraper adapter behaviour against a fake FlareSolverr (spec section 6.7)."""

import importlib.resources
import json
import shutil
import socket
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path

import pytest
import requests
from structlog.testing import capture_logs

from novelhog import config, net
from novelhog.config import Settings
from novelhog.domain.hosts import FailureKind
from novelhog.scraper_adapter import ScraperAdapter, guard
from novelhog.scraper_adapter.errors import (
    HostFailureError,
    InfrastructureError,
    SiteChallenged,
    SiteError,
    SiteThrottled,
    SiteTransient,
    SiteUnreachable,
    TocLoopError,
)
from tests.fixtures.fake_flaresolverr import FakeFlareSolverr, load_html
from tests.fixtures.fake_site import FakeSite

SH_HOST = "scribblehub.com"
SH_TOC = "https://scribblehub.com/series/1/lantern/"
SH_TOC_2 = f"{SH_TOC}?toc=2"
SH_CH = "https://www.scribblehub.com/read/1-lantern/chapter/{}/"
FM_HOST = "fanmtl.com"
FM_TOC = "https://fanmtl.com/novel/moss-lantern-atlas.html"
FM_PAGE_2 = "https://www.fanmtl.com/e/extend/fy.php?page=1&wjm=moss-lantern-atlas"
FM_CH = "https://fanmtl.com/novel/moss-lantern-atlas_{}.html"
HN_HOST = "hostednovel.com"
HN_TOC = "https://hostednovel.com/novel/glass-orchard"
HN_CH = "https://hostednovel.com/novel/glass-orchard/chapter-{}"


def scribblehub_pages() -> dict[str, str]:
    return {
        SH_TOC: load_html("scribblehub_toc_1.html"),
        SH_TOC_2: load_html("scribblehub_toc_2.html"),
    }


def hostednovel_pages(*, loop: bool = False) -> dict[str, str]:
    second = "hostednovel_toc_2_loop.html" if loop else "hostednovel_toc_2.html"
    return {
        HN_TOC: load_html("hostednovel_toc_1.html"),
        f"{HN_TOC}?page=2": load_html(second),
    }


def fanmtl_pages() -> dict[str, str]:
    return {
        FM_TOC: load_html("fanmtl_toc_1.html"),
        FM_PAGE_2: load_html("fanmtl_toc_2.html"),
    }


def make_settings(tmp_path: Path, flaresolverr_url: str, **overrides: str) -> Settings:
    """Settings for the fake sites and FlareSolverr, which run on 127.0.0.1."""
    env = {
        "NOVELHOG_SCRAPER_DIR": str(tmp_path / "scraper"),
        "NOVELHOG_FLARESOLVERR_URL": flaresolverr_url,
        "NOVELHOG_ALLOW_PRIVATE_FETCH": "true",
        **overrides,
    }
    return config.load(env)


@pytest.fixture(autouse=True)
def _no_retry_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """The scraper sleeps between retries; skip that so failure tests stay fast."""
    monkeypatch.setattr("web_novel_scraper.request_helper.time.sleep", lambda _seconds: None)


@pytest.fixture
def dead_url() -> str:
    """The URL of a port nothing listens on."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    return f"http://127.0.0.1:{port}"


@pytest.fixture
def flaresolverr() -> Iterator[FakeFlareSolverr]:
    with FakeFlareSolverr({}) as fake:
        yield fake


class TestScraperAdapter__SupportedHosts:
    def test_supported_hosts__returns_every_decode_guide_host_without_www(
        self, tmp_path: Path, dead_url: str
    ) -> None:
        guide_file = (
            importlib.resources.files("web_novel_scraper") / "decode_guide/decode_guide.json"
        )
        expected = [
            e["host"].removeprefix("www.") for e in json.loads(guide_file.read_text("utf-8"))
        ]
        hosts = ScraperAdapter(make_settings(tmp_path, dead_url)).supported_hosts()
        assert hosts == expected
        assert SH_HOST in hosts
        assert not any(h.startswith("www.") for h in hosts)


class TestScraperAdapter__RequestDelay:
    def test_request_delay__returns_the_hosts_configured_seconds(
        self, tmp_path: Path, dead_url: str
    ) -> None:
        adapter = ScraperAdapter(make_settings(tmp_path, dead_url))

        assert adapter.request_delay("fucknovelpia.com") == 10.0

    def test_request_delay__is_zero_for_a_host_without_one(
        self, tmp_path: Path, dead_url: str
    ) -> None:
        adapter = ScraperAdapter(make_settings(tmp_path, dead_url))

        assert adapter.request_delay(SH_HOST) == 0.0


class TestScraperAdapter__SyncToc:
    def test_sync_toc__uses_novel_id_as_scraper_title_in_scraper_dir(
        self, tmp_path: Path, flaresolverr: FakeFlareSolverr
    ) -> None:
        flaresolverr.pages.update(scribblehub_pages())
        settings = make_settings(tmp_path, flaresolverr.url)
        ScraperAdapter(settings).sync_toc("NV000000000001", SH_HOST, SH_TOC)
        meta = json.loads((settings.scraper_dir / "meta.json").read_text("utf-8"))
        novel_dir = Path(meta["NV000000000001"]["novel_base_dir"])
        assert novel_dir.is_relative_to(settings.scraper_dir)
        assert (novel_dir / "data" / "toc" / "toc_0.html").exists()

    def test_sync_toc__follows_pagination_and_returns_normalised_urls_in_toc_order(
        self, tmp_path: Path, flaresolverr: FakeFlareSolverr
    ) -> None:
        flaresolverr.pages.update(hostednovel_pages())
        result = ScraperAdapter(make_settings(tmp_path, flaresolverr.url)).sync_toc(
            "NV000000000001", HN_HOST, HN_TOC
        )
        assert result.urls == [HN_CH.format(1), HN_CH.format(2), HN_CH.format(3)]
        assert flaresolverr.requested_urls == [HN_TOC, f"{HN_TOC}?page=2"]

    def test_sync_toc__normalises_urls(
        self, tmp_path: Path, flaresolverr: FakeFlareSolverr
    ) -> None:
        flaresolverr.pages.update(scribblehub_pages())
        result = ScraperAdapter(make_settings(tmp_path, flaresolverr.url)).sync_toc(
            "NV000000000001", SH_HOST, SH_TOC
        )
        assert all(url.startswith("https://scribblehub.com/") for url in result.urls)

    def test_sync_toc__descending_host_returns_ascending_reading_order(
        self, tmp_path: Path, flaresolverr: FakeFlareSolverr
    ) -> None:
        flaresolverr.pages.update(scribblehub_pages())
        result = ScraperAdapter(make_settings(tmp_path, flaresolverr.url)).sync_toc(
            "NV000000000001", SH_HOST, SH_TOC
        )
        assert result.urls == [
            f"https://scribblehub.com/read/1-lantern/chapter/{n}" for n in (1, 2, 3, 4)
        ]

    def test_sync_toc__follows_relative_next_page_link(
        self, tmp_path: Path, flaresolverr: FakeFlareSolverr
    ) -> None:
        flaresolverr.pages.update(hostednovel_pages())
        ScraperAdapter(make_settings(tmp_path, flaresolverr.url)).sync_toc(
            "NV000000000001", HN_HOST, HN_TOC
        )
        assert f"{HN_TOC}?page=2" in flaresolverr.requested_urls

    def test_sync_toc__returns_language_and_first_toc_page_html(
        self, tmp_path: Path, flaresolverr: FakeFlareSolverr
    ) -> None:
        flaresolverr.pages.update(scribblehub_pages())
        result = ScraperAdapter(make_settings(tmp_path, flaresolverr.url)).sync_toc(
            "NV000000000001", SH_HOST, SH_TOC
        )
        assert result.language == "en-US"
        assert result.toc_html == load_html("scribblehub_toc_1.html")

    def test_sync_toc__returns_detected_title_and_cover_url(
        self, tmp_path: Path, flaresolverr: FakeFlareSolverr
    ) -> None:
        flaresolverr.pages.update(fanmtl_pages())
        result = ScraperAdapter(make_settings(tmp_path, flaresolverr.url)).sync_toc(
            "NV000000000001", FM_HOST, FM_TOC
        )
        assert result.title == "Moss Lantern Atlas"
        assert result.cover_url == "https://fanmtl.com/files/covers/moss-lantern.jpg"

    def test_sync_toc__title_without_cover_gives_no_cover_url(
        self, tmp_path: Path, flaresolverr: FakeFlareSolverr
    ) -> None:
        flaresolverr.pages.update(scribblehub_pages())
        result = ScraperAdapter(make_settings(tmp_path, flaresolverr.url)).sync_toc(
            "NV000000000001", SH_HOST, SH_TOC
        )
        assert (result.title, result.cover_url) == ("Lantern of the Ash Road", None)

    def test_sync_toc__follows_fanmtl_gt_entity_pagination(
        self, tmp_path: Path, flaresolverr: FakeFlareSolverr
    ) -> None:
        flaresolverr.pages.update(fanmtl_pages())
        result = ScraperAdapter(make_settings(tmp_path, flaresolverr.url)).sync_toc(
            "NV000000000001", FM_HOST, FM_TOC
        )
        assert flaresolverr.requested_urls == [FM_TOC, FM_PAGE_2]
        assert result.urls == [FM_CH.format(n) for n in (1, 2, 3)]

    def test_sync_toc__applies_host_request_config_with_settings_flaresolverr_url(
        self, tmp_path: Path, flaresolverr: FakeFlareSolverr
    ) -> None:
        flaresolverr.pages.update(scribblehub_pages())
        ScraperAdapter(make_settings(tmp_path, flaresolverr.url)).sync_toc(
            "NV000000000001", SH_HOST, SH_TOC
        )
        gets = [c for c in flaresolverr.commands if c["cmd"] == "request.get"]
        assert {c["maxTimeout"] for c in gets} == {60000}
        assert all(c["cookies"] == [{"name": "toc_show", "value": "10000"}] for c in gets)

    def test_sync_toc__works_again_after_scraper_dir_was_deleted(
        self, tmp_path: Path, flaresolverr: FakeFlareSolverr
    ) -> None:
        flaresolverr.pages.update(scribblehub_pages())
        settings = make_settings(tmp_path, flaresolverr.url)
        adapter = ScraperAdapter(settings)
        first = adapter.sync_toc("NV000000000001", SH_HOST, SH_TOC)
        shutil.rmtree(settings.scraper_dir)
        assert adapter.sync_toc("NV000000000001", SH_HOST, SH_TOC).urls == first.urls

    def test_sync_toc__enforces_at_least_one_request_retry(
        self,
        tmp_path: Path,
        flaresolverr: FakeFlareSolverr,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("SCRAPER_REQUEST_RETRIES", "0")
        flaresolverr.pages.update(scribblehub_pages())
        result = ScraperAdapter(make_settings(tmp_path, flaresolverr.url)).sync_toc(
            "NV000000000001", SH_HOST, SH_TOC
        )
        assert len(result.urls) == 4

    def test_sync_toc__ignores_scraper_environment_config_file(
        self,
        tmp_path: Path,
        flaresolverr: FakeFlareSolverr,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        user_config = tmp_path / "user.json"
        user_config.write_text(json.dumps({"request_config": {"flaresolver_url": "http://x:1"}}))
        monkeypatch.setenv("SCRAPER_CONFIG_FILE", str(user_config))
        flaresolverr.pages.update(scribblehub_pages())
        result = ScraperAdapter(make_settings(tmp_path, flaresolverr.url)).sync_toc(
            "NV000000000001", SH_HOST, SH_TOC
        )
        assert len(result.urls) == 4


class TestScraperAdapter__SyncToc__OnSelfReferentialLastPage:
    def test_sync_toc__raises_toc_loop_error(
        self, tmp_path: Path, flaresolverr: FakeFlareSolverr
    ) -> None:
        flaresolverr.pages.update(hostednovel_pages(loop=True))
        with pytest.raises(TocLoopError):
            ScraperAdapter(make_settings(tmp_path, flaresolverr.url)).sync_toc(
                "NV000000000001", HN_HOST, HN_TOC
            )


class TestScraperAdapter__SyncToc__OnMorePagesThanBudget:
    def test_sync_toc__raises_toc_loop_error(
        self, tmp_path: Path, flaresolverr: FakeFlareSolverr
    ) -> None:
        flaresolverr.pages.update(hostednovel_pages())
        settings = make_settings(tmp_path, flaresolverr.url, NOVELHOG_MAX_TOC_PAGES="1")
        with pytest.raises(TocLoopError):
            ScraperAdapter(settings).sync_toc("NV000000000001", HN_HOST, HN_TOC)


class TestScraperAdapter__SyncToc__OnUnreachableFlaresolverr:
    def test_sync_toc__raises_infrastructure_error(self, tmp_path: Path, dead_url: str) -> None:
        with pytest.raises(InfrastructureError):
            ScraperAdapter(make_settings(tmp_path, dead_url)).sync_toc(
                "NV000000000001", SH_HOST, SH_TOC
            )


class TestScraperAdapter__SyncToc__OnNoChapters:
    def test_sync_toc__raises_site_error(
        self, tmp_path: Path, flaresolverr: FakeFlareSolverr
    ) -> None:
        flaresolverr.pages[SH_TOC] = load_html("scribblehub_toc_empty.html")
        with pytest.raises(SiteError):
            ScraperAdapter(make_settings(tmp_path, flaresolverr.url)).sync_toc(
                "NV000000000001", SH_HOST, SH_TOC
            )


class TestScraperAdapter__SyncToc__OnUnknownPage:
    def test_sync_toc__raises_site_error(
        self, tmp_path: Path, flaresolverr: FakeFlareSolverr
    ) -> None:
        with pytest.raises(SiteError):
            ScraperAdapter(make_settings(tmp_path, flaresolverr.url)).sync_toc(
                "NV000000000001", SH_HOST, SH_TOC
            )


class TestScraperAdapter__FetchChapter:
    def test_fetch_chapter__returns_content_selector_body_and_title(
        self, tmp_path: Path, flaresolverr: FakeFlareSolverr
    ) -> None:
        flaresolverr.pages[SH_CH.format(1)] = load_html("scribblehub_chapter.html")
        chapter = ScraperAdapter(make_settings(tmp_path, flaresolverr.url)).fetch_chapter(
            "NV000000000001", SH_HOST, SH_TOC, SH_CH.format(1)
        )
        assert chapter.title == "Chapter 1: The Lamp Goes Out"
        assert chapter.body_html == (
            "<p>The wind dropped as Mira reached the ferry landing.</p>\n"
            "<p>She counted the lanterns twice and found one missing.</p>"
        )

    def test_fetch_chapter__never_prepends_the_title_to_the_body(
        self, tmp_path: Path, flaresolverr: FakeFlareSolverr
    ) -> None:
        flaresolverr.pages[SH_CH.format(1)] = load_html("scribblehub_chapter.html")
        chapter = ScraperAdapter(make_settings(tmp_path, flaresolverr.url)).fetch_chapter(
            "NV000000000001", SH_HOST, SH_TOC, SH_CH.format(1)
        )
        assert "<h4>" not in chapter.body_html
        assert "The Lamp Goes Out" not in chapter.body_html

    def test_fetch_chapter__missing_title_returns_none(
        self, tmp_path: Path, flaresolverr: FakeFlareSolverr
    ) -> None:
        flaresolverr.pages[SH_CH.format(1)] = load_html("scribblehub_chapter_no_title.html")
        chapter = ScraperAdapter(make_settings(tmp_path, flaresolverr.url)).fetch_chapter(
            "NV000000000001", SH_HOST, SH_TOC, SH_CH.format(1)
        )
        assert chapter.title is None
        assert chapter.body_html == "<p>The ferryman said nothing for a long while.</p>"

    def test_fetch_chapter__deletes_the_cached_chapter_html(
        self, tmp_path: Path, flaresolverr: FakeFlareSolverr
    ) -> None:
        flaresolverr.pages[SH_CH.format(1)] = load_html("scribblehub_chapter.html")
        settings = make_settings(tmp_path, flaresolverr.url)
        ScraperAdapter(settings).fetch_chapter("NV000000000001", SH_HOST, SH_TOC, SH_CH.format(1))
        assert list(settings.scraper_dir.rglob("chapters/*.html")) == []

    def test_fetch_chapter__deletes_the_cached_html_when_decoding_fails(
        self, tmp_path: Path, flaresolverr: FakeFlareSolverr
    ) -> None:
        flaresolverr.pages[SH_CH.format(9)] = load_html("scribblehub_chapter_no_content.html")
        settings = make_settings(tmp_path, flaresolverr.url)
        with pytest.raises(SiteError):
            ScraperAdapter(settings).fetch_chapter(
                "NV000000000001", SH_HOST, SH_TOC, SH_CH.format(9)
            )
        assert list(settings.scraper_dir.rglob("chapters/*.html")) == []


class TestScraperAdapter__FetchChapter__OnEmptyContentSelector:
    def test_fetch_chapter__raises_site_error(
        self, tmp_path: Path, flaresolverr: FakeFlareSolverr
    ) -> None:
        flaresolverr.pages[SH_CH.format(9)] = load_html("scribblehub_chapter_no_content.html")
        with pytest.raises(SiteError):
            ScraperAdapter(make_settings(tmp_path, flaresolverr.url)).fetch_chapter(
                "NV000000000001", SH_HOST, SH_TOC, SH_CH.format(9)
            )


class TestScraperAdapter__FetchChapter__OnUnreachableFlaresolverr:
    def test_fetch_chapter__raises_infrastructure_error(
        self, tmp_path: Path, dead_url: str
    ) -> None:
        with pytest.raises(InfrastructureError):
            ScraperAdapter(make_settings(tmp_path, dead_url)).fetch_chapter(
                "NV000000000001", SH_HOST, SH_TOC, SH_CH.format(1)
            )


# hiraethtranslation.com is a decode-guide host whose scraper config does not force FlareSolverr.
DIRECT_HOST = "hiraethtranslation.com"


def direct_toc(base: str) -> str:
    return (
        '<html lang="en"><body><ul>'
        f'<li class="free-chap"><a href="{base}/c/2">Chapter 2</a></li>'
        f'<li class="free-chap"><a href="{base}/c/1">Chapter 1</a></li>'
        "</ul></body></html>"
    )


class TestScraperAdapter__DirectHost:
    def test_sync_toc__host_without_forced_flaresolverr_requests_the_site_directly(
        self, tmp_path: Path, flaresolverr: FakeFlareSolverr
    ) -> None:
        with FakeSite({}) as site:
            site.pages["/toc"] = direct_toc(site.url)
            result = ScraperAdapter(make_settings(tmp_path, flaresolverr.url)).sync_toc(
                "NV000000000001", DIRECT_HOST, f"{site.url}/toc"
            )
            assert result.urls == [f"{site.url}/c/1", f"{site.url}/c/2"]
        assert flaresolverr.commands == []

    def test_sync_toc__http_error_from_the_site_raises_site_error(
        self, tmp_path: Path, flaresolverr: FakeFlareSolverr
    ) -> None:
        with FakeSite({}) as site, pytest.raises(SiteError):
            ScraperAdapter(make_settings(tmp_path, flaresolverr.url)).sync_toc(
                "NV000000000001", DIRECT_HOST, f"{site.url}/missing"
            )

    def test_sync_toc__connection_refused_raises_site_unreachable_naming_the_host(
        self, tmp_path: Path, flaresolverr: FakeFlareSolverr, dead_url: str
    ) -> None:
        with pytest.raises(SiteUnreachable, match=DIRECT_HOST):
            ScraperAdapter(make_settings(tmp_path, flaresolverr.url)).sync_toc(
                "NV000000000001", DIRECT_HOST, f"{dead_url}/toc"
            )

    def test_sync_toc__dns_failure_raises_site_unreachable_naming_the_host(
        self, tmp_path: Path, flaresolverr: FakeFlareSolverr
    ) -> None:
        with pytest.raises(SiteUnreachable, match=DIRECT_HOST):
            ScraperAdapter(make_settings(tmp_path, flaresolverr.url)).sync_toc(
                "NV000000000001", DIRECT_HOST, "http://no-such-host.invalid/toc"
            )

    def test_fetch_chapter__connection_refused_raises_site_unreachable(
        self, tmp_path: Path, flaresolverr: FakeFlareSolverr, dead_url: str
    ) -> None:
        with pytest.raises(SiteUnreachable, match=DIRECT_HOST):
            ScraperAdapter(make_settings(tmp_path, flaresolverr.url)).fetch_chapter(
                "NV000000000001", DIRECT_HOST, f"{dead_url}/toc", f"{dead_url}/c/1"
            )


class TestScraperErrors__SiteUnreachable:
    def test_site_unreachable__is_a_host_failure_not_an_infrastructure_error(self) -> None:
        assert issubclass(SiteUnreachable, HostFailureError)
        assert not issubclass(SiteUnreachable, InfrastructureError)


class TestScraperAdapter__OnUnreachableFlaresolverr__ForAForcedHost:
    def test_sync_toc__raises_infrastructure_error_that_is_not_site_unreachable(
        self, tmp_path: Path, dead_url: str
    ) -> None:
        with pytest.raises(InfrastructureError) as raised:
            ScraperAdapter(make_settings(tmp_path, dead_url)).sync_toc(
                "NV000000000001", SH_HOST, SH_TOC
            )
        assert not isinstance(raised.value, SiteUnreachable)


@pytest.fixture(params=["header", "body"])
def challenge(request: pytest.FixtureRequest) -> str:
    return str(request.param)


class TestScraperAdapter__OnCloudflareChallenge:
    def test_sync_toc__retries_the_request_through_flaresolverr_and_returns_its_result(
        self, tmp_path: Path, flaresolverr: FakeFlareSolverr, challenge: str
    ) -> None:
        with FakeSite({}, challenge=challenge) as site:
            flaresolverr.pages[f"{site.url}/toc"] = direct_toc(site.url)
            result = ScraperAdapter(make_settings(tmp_path, flaresolverr.url)).sync_toc(
                "NV000000000001", DIRECT_HOST, f"{site.url}/toc"
            )
            assert site.requests == ["/toc"]
        assert result.urls == [f"{site.url}/c/1", f"{site.url}/c/2"]
        assert flaresolverr.requested_urls == [f"{site.url}/toc"]

    def test_sync_toc__routes_later_requests_for_the_host_through_flaresolverr(
        self, tmp_path: Path, flaresolverr: FakeFlareSolverr, challenge: str
    ) -> None:
        with FakeSite({}, challenge=challenge) as site:
            flaresolverr.pages[f"{site.url}/toc"] = direct_toc(site.url)
            flaresolverr.pages[f"{site.url}/c/1"] = (
                '<html><body><ul><li class="active">One</li></ul>'
                '<div class="reading-content"><p>Hello there.</p></div></body></html>'
            )
            adapter = ScraperAdapter(make_settings(tmp_path, flaresolverr.url))
            adapter.sync_toc("NV000000000001", DIRECT_HOST, f"{site.url}/toc")
            adapter.sync_toc("NV000000000001", DIRECT_HOST, f"{site.url}/toc")
            chapter = adapter.fetch_chapter(
                "NV000000000001", DIRECT_HOST, f"{site.url}/toc", f"{site.url}/c/1"
            )
            assert site.requests == ["/toc"]
        assert flaresolverr.requested_urls == [f"{site.url}/toc"] * 2 + [f"{site.url}/c/1"]
        assert chapter.body_html == "<p>Hello there.</p>"

    def test_sync_toc__logs_flaresolverr_fallback_enabled_once_with_the_host(
        self, tmp_path: Path, flaresolverr: FakeFlareSolverr
    ) -> None:
        with FakeSite({}, challenge="header") as site, capture_logs() as logs:
            flaresolverr.pages[f"{site.url}/toc"] = direct_toc(site.url)
            adapter = ScraperAdapter(make_settings(tmp_path, flaresolverr.url))
            adapter.sync_toc("NV000000000001", DIRECT_HOST, f"{site.url}/toc")
            adapter.sync_toc("NV000000000001", DIRECT_HOST, f"{site.url}/toc")
        events = [e for e in logs if e["event"] == "flaresolverr_fallback_enabled"]
        assert [e["host"] for e in events] == ["127.0.0.1"]

    def test_sync_toc__failed_flaresolverr_retry_raises_site_error_and_keeps_requesting_directly(
        self, tmp_path: Path, flaresolverr: FakeFlareSolverr
    ) -> None:
        with FakeSite({}, challenge="header") as site:
            adapter = ScraperAdapter(make_settings(tmp_path, flaresolverr.url))
            with pytest.raises(SiteError):
                adapter.sync_toc("NV000000000001", DIRECT_HOST, f"{site.url}/toc")
            assert guard.fallback_hosts() == frozenset()
            with pytest.raises(SiteError):
                adapter.sync_toc("NV000000000001", DIRECT_HOST, f"{site.url}/toc")
            assert site.requests == ["/toc", "/toc"]

    def test_sync_toc__unreachable_flaresolverr_retry_raises_infrastructure_error(
        self, tmp_path: Path, dead_url: str
    ) -> None:
        with FakeSite({}, challenge="header") as site:
            with pytest.raises(InfrastructureError) as raised:
                ScraperAdapter(make_settings(tmp_path, dead_url)).sync_toc(
                    "NV000000000001", DIRECT_HOST, f"{site.url}/toc"
                )
        assert not isinstance(raised.value, SiteUnreachable)
        assert guard.fallback_hosts() == frozenset()


NOT_CHALLENGES = {
    "plain 403": (403, {}, "forbidden"),
    "plain 503": (503, {}, "maintenance"),
    "403 from cloudflare with an ordinary body": (403, {"Server": "cloudflare"}, "blocked"),
    "503 from cloudflare with an ordinary body": (503, {"Server": "cloudflare"}, "overloaded"),
    "challenge body without cloudflare": (503, {}, "<title>Just a moment...</title>"),
    "other cf-mitigated value": (403, {"cf-mitigated": "block"}, "blocked"),
    "challenge header on a 200": (200, {"cf-mitigated": "challenge"}, "<html></html>"),
}


class TestScraperAdapter__OnResponseThatIsNotAChallenge:
    @pytest.mark.parametrize("reply", NOT_CHALLENGES.values(), ids=NOT_CHALLENGES.keys())
    def test_sync_toc__goes_direct_without_flaresolverr_and_marks_no_host(
        self, tmp_path: Path, flaresolverr: FakeFlareSolverr,
        reply: tuple[int, dict[str, str], str],
    ) -> None:  # fmt: skip
        with FakeSite({}, reply=reply) as site, pytest.raises(SiteError):
            ScraperAdapter(make_settings(tmp_path, flaresolverr.url)).sync_toc(
                "NV000000000001", DIRECT_HOST, f"{site.url}/toc"
            )
        assert flaresolverr.commands == []
        assert guard.fallback_hosts() == frozenset()


class TestScraperAdapter__OnConnectionProblems:
    def patch_get(self, monkeypatch: pytest.MonkeyPatch, error: Exception) -> None:
        def get(*_args: object, **_kwargs: object) -> None:
            raise error

        monkeypatch.setattr(net.GuardedSession, "get", get)

    def sync(self, tmp_path: Path, flaresolverr: FakeFlareSolverr) -> None:
        ScraperAdapter(make_settings(tmp_path, flaresolverr.url)).sync_toc(
            "NV000000000001", DIRECT_HOST, "http://site.example/toc"
        )

    def test_sync_toc__connect_timeout_raises_site_unreachable(
        self, tmp_path: Path, flaresolverr: FakeFlareSolverr, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self.patch_get(monkeypatch, requests.exceptions.ConnectTimeout("timed out"))
        with pytest.raises(SiteUnreachable, match=f"{DIRECT_HOST}: connection timed out"):
            self.sync(tmp_path, flaresolverr)

    @pytest.mark.parametrize(
        "error",
        [
            requests.exceptions.ReadTimeout("slow"),
            requests.exceptions.ConnectionError("Connection aborted"),
            requests.exceptions.ChunkedEncodingError("cut off"),
        ],
        ids=["read timeout", "reset", "truncated body"],
    )
    def test_sync_toc__a_dropped_connection_raises_site_transient(
        self, tmp_path: Path, flaresolverr: FakeFlareSolverr, monkeypatch: pytest.MonkeyPatch,
        error: Exception,
    ) -> None:  # fmt: skip
        self.patch_get(monkeypatch, error)
        with pytest.raises(SiteTransient):
            self.sync(tmp_path, flaresolverr)

    @pytest.mark.parametrize(
        "error",
        [requests.exceptions.SSLError("bad certificate"), requests.exceptions.ProxyError("proxy")],
        ids=["tls", "proxy"],
    )
    def test_sync_toc__tls_and_proxy_problems_are_plain_site_errors(
        self, tmp_path: Path, flaresolverr: FakeFlareSolverr, monkeypatch: pytest.MonkeyPatch,
        error: Exception,
    ) -> None:  # fmt: skip
        self.patch_get(monkeypatch, error)
        with pytest.raises(SiteError) as raised:
            self.sync(tmp_path, flaresolverr)
        assert not isinstance(raised.value, HostFailureError)


def _sync_with_reply(
    tmp_path: Path, flaresolverr: FakeFlareSolverr, reply: tuple[int, dict[str, str], str]
) -> tuple[BaseException, list[str]]:
    with FakeSite({}, reply=reply) as site:
        with pytest.raises(SiteError) as raised:
            ScraperAdapter(make_settings(tmp_path, flaresolverr.url)).sync_toc(
                "NV000000000001", DIRECT_HOST, f"{site.url}/toc"
            )
        return raised.value, site.requests


class TestScraperAdapter__OnThrottlingResponses:
    def test_sync_toc__429_with_retry_after_seconds_raises_site_throttled_without_retrying(
        self, tmp_path: Path, flaresolverr: FakeFlareSolverr
    ) -> None:
        error, requests_made = _sync_with_reply(
            tmp_path, flaresolverr, (429, {"Retry-After": "120"}, "slow down")
        )
        assert isinstance(error, SiteThrottled)
        assert (error.kind, error.retry_after) == (FailureKind.THROTTLED, 120)
        assert requests_made == ["/toc"]

    def test_sync_toc__503_with_an_http_date_retry_after_gives_the_seconds_until_it(
        self, tmp_path: Path, flaresolverr: FakeFlareSolverr
    ) -> None:
        when = format_datetime(datetime.now(UTC) + timedelta(seconds=300), usegmt=True)
        error, _ = _sync_with_reply(tmp_path, flaresolverr, (503, {"Retry-After": when}, "busy"))
        assert isinstance(error, SiteThrottled)
        assert error.retry_after is not None
        assert 290 <= error.retry_after <= 300

    def test_sync_toc__429_without_retry_after_has_no_retry_after(
        self, tmp_path: Path, flaresolverr: FakeFlareSolverr
    ) -> None:
        error, _ = _sync_with_reply(tmp_path, flaresolverr, (429, {}, "slow down"))
        assert isinstance(error, SiteThrottled)
        assert error.retry_after is None

    def test_sync_toc__an_unparseable_retry_after_is_ignored(
        self, tmp_path: Path, flaresolverr: FakeFlareSolverr
    ) -> None:
        error, _ = _sync_with_reply(tmp_path, flaresolverr, (429, {"Retry-After": "soon"}, "x"))
        assert isinstance(error, SiteThrottled)
        assert error.retry_after is None


class TestScraperAdapter__OnServerErrors:
    @pytest.mark.parametrize("status", [500, 502])
    def test_sync_toc__a_5xx_raises_site_transient(
        self, tmp_path: Path, flaresolverr: FakeFlareSolverr, status: int
    ) -> None:
        error, _ = _sync_with_reply(tmp_path, flaresolverr, (status, {}, "oops"))
        assert isinstance(error, SiteTransient)
        assert error.kind is FailureKind.TRANSIENT

    def test_sync_toc__a_404_is_a_site_error_that_is_no_host_failure(
        self, tmp_path: Path, flaresolverr: FakeFlareSolverr
    ) -> None:
        error, _ = _sync_with_reply(tmp_path, flaresolverr, (404, {}, "gone"))
        assert not isinstance(error, HostFailureError)


class TestScraperAdapter__OnAnUnsolvedChallenge:
    def test_sync_toc__a_challenge_flaresolverr_could_not_solve_raises_site_challenged(
        self, tmp_path: Path, flaresolverr: FakeFlareSolverr
    ) -> None:
        with FakeSite({}, challenge="header") as site:
            with pytest.raises(SiteChallenged) as raised:
                ScraperAdapter(make_settings(tmp_path, flaresolverr.url)).sync_toc(
                    "NV000000000001", DIRECT_HOST, f"{site.url}/toc"
                )
        assert raised.value.kind is FailureKind.CHALLENGED


class TestScraperErrors__Kinds:
    def test_site_unreachable__is_an_unreachable_host_failure(self) -> None:
        assert SiteUnreachable("x").kind is FailureKind.UNREACHABLE


class TestScraperAdapter__SyncToc__OnExactlyBudgetPages:
    def test_sync_toc__succeeds(self, tmp_path: Path, flaresolverr: FakeFlareSolverr) -> None:
        flaresolverr.pages.update(hostednovel_pages())
        settings = make_settings(tmp_path, flaresolverr.url, NOVELHOG_MAX_TOC_PAGES="2")
        assert len(ScraperAdapter(settings).sync_toc("NV000000000001", HN_HOST, HN_TOC).urls) == 3


def guarded_settings(tmp_path: Path, flaresolverr_url: str) -> Settings:
    """Settings with the outbound guard on (no `ALLOW_PRIVATE_FETCH`)."""
    return make_settings(tmp_path, flaresolverr_url, NOVELHOG_ALLOW_PRIVATE_FETCH="false")


def resolver(table: dict[str, str]) -> Callable[[str], list[str]]:
    """A resolver that answers from `table` and fails for any other host."""

    def resolve(host: str) -> list[str]:
        if host not in table:
            msg = f"cannot resolve {host}"
            raise net.Unresolvable(msg)
        return [table[host]]

    return resolve


@pytest.fixture
def loopback_is_public(monkeypatch: pytest.MonkeyPatch) -> None:
    """Treat 127.0.0.1 as public so a fake site can stand in for a public host."""
    real = net.is_public
    monkeypatch.setattr(net, "is_public", lambda address: address == "127.0.0.1" or real(address))


CHAPTER_HTML = (
    '<html><body><ul><li class="active">One</li></ul>'
    '<div class="reading-content"><p>Hello there.</p></div></body></html>'
)


class TestScraperAdapter__OutboundGuard__DirectFetchOfNonPublicHost:
    def test_sync_toc__raises_site_error_naming_the_host_without_requesting(
        self, tmp_path: Path, flaresolverr: FakeFlareSolverr
    ) -> None:
        with FakeSite({}) as site:
            site.pages["/toc"] = direct_toc(site.url)
            with pytest.raises(SiteError, match=r"^Blocked non-public address: 127\.0\.0\.1$"):
                ScraperAdapter(guarded_settings(tmp_path, flaresolverr.url)).sync_toc(
                    "NV000000000001", DIRECT_HOST, f"{site.url}/toc"
                )
            assert site.requests == []

    def test_fetch_chapter__raises_site_error_naming_the_host_without_requesting(
        self, tmp_path: Path, flaresolverr: FakeFlareSolverr
    ) -> None:
        with FakeSite({"/c/1": CHAPTER_HTML}) as site:
            with pytest.raises(SiteError, match=r"^Blocked non-public address: 127\.0\.0\.1$"):
                ScraperAdapter(guarded_settings(tmp_path, flaresolverr.url)).fetch_chapter(
                    "NV000000000001", DIRECT_HOST, f"{site.url}/toc", f"{site.url}/c/1"
                )
            assert site.requests == []

    def test_fetch_chapter__is_allowed_with_allow_private_fetch(
        self, tmp_path: Path, flaresolverr: FakeFlareSolverr
    ) -> None:
        with FakeSite({"/c/1": CHAPTER_HTML}) as site:
            chapter = ScraperAdapter(make_settings(tmp_path, flaresolverr.url)).fetch_chapter(
                "NV000000000001", DIRECT_HOST, f"{site.url}/toc", f"{site.url}/c/1"
            )
        assert chapter.body_html == "<p>Hello there.</p>"


@pytest.mark.usefixtures("loopback_is_public")
class TestScraperAdapter__OutboundGuard__DirectFetchRedirectedToNonPublicHost:
    def test_sync_toc__raises_site_error_naming_the_redirect_host(
        self, tmp_path: Path, flaresolverr: FakeFlareSolverr
    ) -> None:
        reply = (302, {"Location": "http://private.test/toc"}, "")
        table = {"site.test": "127.0.0.1", "private.test": "10.0.0.1"}
        with FakeSite({}, reply=reply) as site:
            port = site.url.rsplit(":", 1)[1]
            adapter = ScraperAdapter(guarded_settings(tmp_path, flaresolverr.url), resolver(table))
            with pytest.raises(SiteError, match=r"^Blocked non-public address: private\.test$"):
                adapter.sync_toc("NV000000000001", DIRECT_HOST, f"http://site.test:{port}/toc")
            assert site.requests == ["/toc"]


@pytest.mark.usefixtures("loopback_is_public")
class TestScraperAdapter__OutboundGuard__DirectFetchOfPublicHostOtherThanTheNovels:
    def test_fetch_chapter__returns_the_chapter(
        self, tmp_path: Path, flaresolverr: FakeFlareSolverr
    ) -> None:
        table = {"site.test": "127.0.0.1", "api.site.test": "127.0.0.1"}
        with FakeSite({"/c/1": CHAPTER_HTML}) as site:
            port = site.url.rsplit(":", 1)[1]
            adapter = ScraperAdapter(guarded_settings(tmp_path, flaresolverr.url), resolver(table))
            chapter = adapter.fetch_chapter(
                "NV000000000001",
                DIRECT_HOST,
                f"http://site.test:{port}/toc",
                f"http://api.site.test:{port}/c/1",
            )
        assert chapter.body_html == "<p>Hello there.</p>"


class TestScraperAdapter__OutboundGuard__ViaFlareSolverr:
    def test_fetch_chapter__non_public_host_raises_site_error_before_flaresolverr_is_asked(
        self, tmp_path: Path, flaresolverr: FakeFlareSolverr
    ) -> None:
        flaresolverr.pages[SH_CH.format(1)] = load_html("scribblehub_chapter.html")
        adapter = ScraperAdapter(
            guarded_settings(tmp_path, flaresolverr.url),
            resolver({"www.scribblehub.com": "192.168.1.1"}),
        )
        with pytest.raises(SiteError, match=r"^Blocked non-public address: www\.scribblehub\.com$"):
            adapter.fetch_chapter("NV000000000001", SH_HOST, SH_TOC, SH_CH.format(1))
        assert flaresolverr.requested_urls == []

    def test_fetch_chapter__public_host_other_than_the_novels_is_fetched(
        self, tmp_path: Path, flaresolverr: FakeFlareSolverr
    ) -> None:
        url = "https://api.mystorywave.com/chapter/1"
        flaresolverr.pages[url] = load_html("scribblehub_chapter.html")
        adapter = ScraperAdapter(
            guarded_settings(tmp_path, flaresolverr.url),
            resolver({"api.mystorywave.com": "93.184.216.34"}),
        )
        chapter = adapter.fetch_chapter("NV000000000001", SH_HOST, SH_TOC, url)
        assert chapter.title == "Chapter 1: The Lamp Goes Out"
        assert flaresolverr.requested_urls == [url]

    def test_sync_toc__unresolvable_host_raises_site_unreachable(
        self, tmp_path: Path, flaresolverr: FakeFlareSolverr
    ) -> None:
        flaresolverr.pages.update(scribblehub_pages())
        adapter = ScraperAdapter(guarded_settings(tmp_path, flaresolverr.url), resolver({}))
        with pytest.raises(SiteUnreachable, match=f"{SH_HOST}: name resolution failed"):
            adapter.sync_toc("NV000000000001", SH_HOST, SH_TOC)
        assert flaresolverr.requested_urls == []
