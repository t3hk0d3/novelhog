"""Host publishing and the adapters the TOC refresh uses (spec sections 6.1 to 6.3).

The refresh itself runs as a `novel_refresh` task (`worker/tasks/novel_refresh.py`).
"""

from typing import Protocol

from novelhog import log
from novelhog.images import FetchedImage
from novelhog.repo import Repo
from novelhog.scraper_adapter.adapter import TocResult


class TocAdapter(Protocol):
    """What the refresh needs from `ScraperAdapter`."""

    def supported_hosts(self) -> list[str]: ...

    def sync_toc(self, novel_id: str, host: str, toc_url: str) -> TocResult: ...


class CoverFetcher(Protocol):
    """What the refresh needs from `ImageFetcher`."""

    def fetch(self, url: str) -> FetchedImage | None: ...


def publish_hosts(repo: Repo, adapter: TocAdapter) -> None:
    """Publish the adapter's hosts as supported; hosts it dropped are marked unsupported."""
    report = repo.hosts.publish(adapter.supported_hosts())
    log.get().info("hosts_published", added=report.added, unsupported=report.unsupported)
