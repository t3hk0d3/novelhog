"""fanmtl.com overrides: the next-page link is now written `&gt;` (2.13.1's rule only matches `>`).

The site is behind Cloudflare: its pages go direct with the worker's clearance (`guard`).
"""

import re
from urllib.parse import urljoin

from novelhog.scraper_adapter.scraper import CustomProcessor, ProcessorRegistry

HOST = "fanmtl.com"
_BASE = "https://www.fanmtl.com/"
_NEXT_LINK = re.compile(r'href="([^"]+page=\d+[^"]*)"\s*>\s*(?:&gt;|>)\s*</a', re.IGNORECASE)


class FanmtlNextPageProcessor(CustomProcessor):  # type: ignore[misc]
    """Finds the link whose text is `>` or `&gt;` and returns its absolute URL."""

    def process(self, html: str) -> str | None:
        """Return the next TOC page URL, or None on the last page."""
        match = _NEXT_LINK.search(html)
        if match is None:
            return None
        href: str = match.group(1)
        return urljoin(_BASE, href.replace("&amp;", "&"))


def register() -> None:
    """Replace the scraper's fanmtl next-page processor with ours (idempotent)."""
    ProcessorRegistry.register(HOST, "next_page", FanmtlNextPageProcessor())
