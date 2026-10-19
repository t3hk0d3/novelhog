"""Tests for normalising TOC chapter URLs in the scraper adapter."""

import pytest

from novelhog.scraper_adapter.adapter import ScraperAdapter
from novelhog.scraper_adapter.errors import SiteError


class TestNormalised__OnOverLongChapterUrl:
    def test_normalised__skips_the_over_long_url_and_keeps_the_rest(self) -> None:
        long_url = "https://example.com/" + "a" * 3000
        urls = ["https://example.com/1", long_url, "https://www.example.com/2/"]
        assert ScraperAdapter._normalised(urls) == [  # noqa: SLF001
            "https://example.com/1",
            "https://example.com/2",
        ]


class TestNormalised__OnOtherInvalidUrl:
    def test_normalised__raises_site_error(self) -> None:
        with pytest.raises(SiteError):
            ScraperAdapter._normalised(["ftp://example.com/1"])  # noqa: SLF001
