"""The fanmtl.com next-page override (spec section 6.7)."""

from novelhog.scraper_adapter.overrides import FORCE_FLARESOLVERR_HOSTS, fanmtl_com, register_all
from novelhog.scraper_adapter.scraper import ProcessorRegistry

LINK = '<ul><li><a href="/e/extend/fy.php?page=1&amp;wjm=abc">{}</a></li></ul>'
NEXT_URL = "https://www.fanmtl.com/e/extend/fy.php?page=1&wjm=abc"


def process(html: str) -> str | None:
    return fanmtl_com.FanmtlNextPageProcessor().process(html)


class TestFanmtlNextPage__WithGreaterThanLink:
    def test_process__returns_absolute_next_page_url(self) -> None:
        assert process(LINK.format(">")) == NEXT_URL


class TestFanmtlNextPage__WithEntityLink:
    def test_process__returns_absolute_next_page_url(self) -> None:
        assert process(LINK.format("&gt;")) == NEXT_URL


class TestFanmtlNextPage__OnLastPage:
    def test_process__returns_none(self) -> None:
        html = '<a href="/e/extend/fy.php?page=0&amp;wjm=abc">&lt;</a>'
        assert process(html) is None


class TestRegisterAll__OnRepeatedCalls:
    def test_register_all__installs_override_and_stays_idempotent(self) -> None:
        register_all()
        register_all()
        processor = ProcessorRegistry.get_processor("fanmtl.com", "next_page")
        assert isinstance(processor, fanmtl_com.FanmtlNextPageProcessor)


class TestFanmtlOverride__ForcesFlaresolverr:
    def test_force_flaresolverr_hosts__include_fanmtl(self) -> None:
        assert fanmtl_com.HOST in FORCE_FLARESOLVERR_HOSTS
