"""TOC loop guard: a per-operation page budget on the scraper's page requests (spec section 6.7)."""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

from web_novel_scraper.request_helper import RequestHelper

from novelhog.scraper_adapter.errors import TocLoopError


@dataclass
class _Budget:
    max_pages: int
    seen: set[str] = field(default_factory=set)

    def record(self, url: str) -> None:
        if url in self.seen:
            msg = f"The table of contents requested {url} twice"
            raise TocLoopError(msg)
        if len(self.seen) >= self.max_pages:
            msg = f"The table of contents has more than {self.max_pages} pages"
            raise TocLoopError(msg)
        self.seen.add(url)


_active: ContextVar[_Budget | None] = ContextVar("novelhog_page_budget", default=None)
_original_get_url_content = RequestHelper.get_url_content


def _guarded_get_url_content(self: RequestHelper, url: str) -> str:
    budget = _active.get()
    if budget is not None:
        budget.record(url)
    result: str = _original_get_url_content(self, url)
    return result


# Installed once at import; it does nothing outside a `page_budget` block.
RequestHelper.get_url_content = _guarded_get_url_content


@contextmanager
def page_budget(max_pages: int) -> Iterator[None]:
    """Count the scraper's page requests in this block; raise `TocLoopError` on a loop or overrun.

    A page counts once however often the scraper retries it. The budget follows the current
    thread (and async task), so concurrent operations do not share it.
    """
    token = _active.set(_Budget(max_pages))
    try:
        yield
    finally:
        _active.reset(token)
