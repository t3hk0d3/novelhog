"""Scraper adapter behaviour against a fake FlareSolverr (spec section 6.7)."""

import importlib.resources
import json
import shutil
import socket
from collections.abc import Iterator
from pathlib import Path

import pytest

from novelhog import config
from novelhog.config import Settings
from novelhog.scraper_adapter import ScraperAdapter
from novelhog.scraper_adapter.errors import InfrastructureError, SiteError, TocLoopError
from tests.fixtures.fake_flaresolverr import FakeFlareSolverr, load_html

SH_HOST = "scribblehub.com"
SH_TOC = "https://scribblehub.com/series/1/lantern/"
SH_TOC_2 = f"{SH_TOC}?toc=2"
SH_CH = "https://www.scribblehub.com/read/1-lantern/chapter/{}/"
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


def make_settings(tmp_path: Path, flaresolverr_url: str, **overrides: str) -> Settings:
    env = {
        "NOVELHOG_SCRAPER_DIR": str(tmp_path / "scraper"),
        "NOVELHOG_FLARESOLVERR_URL": flaresolverr_url,
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
        assert (result.title, result.cover_url) == (None, None)

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
