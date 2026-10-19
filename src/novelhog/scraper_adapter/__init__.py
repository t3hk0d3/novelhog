"""The scraper adapter: the only code allowed to import `web_novel_scraper` (spec section 6.7)."""

from novelhog.scraper_adapter.adapter import FetchedChapter, ScraperAdapter, TocResult

__all__ = ["FetchedChapter", "ScraperAdapter", "TocResult"]
