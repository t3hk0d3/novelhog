"""The only wrapper around `web_novel_scraper`: TOC sync and chapter fetch (spec section 6.7)."""

import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path

import requests
from urllib3.exceptions import NameResolutionError, NewConnectionError

from novelhog import log, net
from novelhog.config import Settings
from novelhog.domain.errors import ValidationFailed
from novelhog.domain.urls import UrlTooLong, normalize_url
from novelhog.scraper_adapter.errors import (
    InfrastructureError,
    SiteChallenged,
    SiteError,
    SiteThrottled,
    SiteTransient,
    SiteUnreachable,
)
from novelhog.scraper_adapter.finders import find_cover_url, find_title
from novelhog.scraper_adapter.guard import (
    CloudflareChallengeError,
    ServerResponseError,
    ThrottledResponseError,
    flaresolverr_fallback,
    outbound_guard,
    page_budget,
)
from novelhog.scraper_adapter.overrides import FORCE_FLARESOLVERR_HOSTS
from novelhog.scraper_adapter.scraper import (
    Chapter,
    FlaresolverrConnectionError,
    Novel,
    ScraperConfig,
    ScraperError,
    take_over_scraper_logging,
)


@dataclass(frozen=True)
class TocResult:
    """A synced table of contents. `title` and `cover_url` are detected from the first TOC page."""

    title: str | None
    language: str | None
    cover_url: str | None
    urls: list[str]
    toc_html: str


@dataclass(frozen=True)
class FetchedChapter:
    """A downloaded chapter: its title (None when the page has none) and the content HTML."""

    title: str | None
    body_html: str


class _HtmlLang(HTMLParser):
    """Collects the `lang` attribute of the `<html>` element."""

    def __init__(self) -> None:
        super().__init__()
        self.lang: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "html" and self.lang is None:
            self.lang = next((v.strip() for k, v in attrs if k == "lang" and v), None) or None


def _html_language(html: str) -> str | None:
    parser = _HtmlLang()
    parser.feed(html)
    return parser.lang


def _unreachable_reason(exc: requests.exceptions.RequestException) -> str | None:
    """A short reason when `exc` is a refused connection, failed name lookup or connect timeout."""
    if isinstance(exc, requests.exceptions.ConnectTimeout):
        return "connection timed out"
    if isinstance(exc, requests.exceptions.ConnectionError) and exc.args:
        reason = getattr(exc.args[0], "reason", None)
        if isinstance(reason, NameResolutionError):
            return "name resolution failed"
        if isinstance(reason, NewConnectionError):
            return "connection refused"
    return None


def _is_transient(exc: requests.exceptions.RequestException) -> bool:
    """A read timeout or a connection dropped mid-way, as opposed to TLS or proxy problems."""
    if isinstance(exc, requests.exceptions.ReadTimeout | requests.exceptions.ChunkedEncodingError):
        return True
    return (
        isinstance(exc, requests.exceptions.ConnectionError)
        and not isinstance(exc, requests.exceptions.SSLError | requests.exceptions.ProxyError)
        and _unreachable_reason(exc) is None
    )


def _chain(exc: BaseException) -> Iterator[BaseException]:
    """`exc` and every exception it was raised from or during, outermost first."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def _classify(exc: BaseException, host: str) -> Exception:
    """The adapter error for a scraper failure, judged by every exception in its chain.

    FlareSolverr's own failure wins (`InfrastructureError`), then an unsolved Cloudflare challenge,
    a throttling answer, an unreachable site (refused, name resolution, connect timeout) and a
    transient one (another 5xx, read timeout, dropped connection). Anything else is a `SiteError`.
    """
    chain = list(_chain(exc))
    if any(isinstance(e, FlaresolverrConnectionError) for e in chain):
        return InfrastructureError(str(exc))
    if any(isinstance(e, CloudflareChallengeError) for e in chain):
        return SiteChallenged(str(exc))
    throttled = next((e for e in chain if isinstance(e, ThrottledResponseError)), None)
    if throttled is not None:
        return SiteThrottled(str(exc), retry_after=throttled.retry_after)
    for e in chain:
        reason = None
        if isinstance(e, net.Unresolvable):
            reason = "name resolution failed"
        elif isinstance(e, requests.exceptions.RequestException):
            reason = _unreachable_reason(e)
        if reason is not None:
            return SiteUnreachable(f"Cannot reach {host}: {reason}")
    if any(
        isinstance(e, ServerResponseError)
        or (isinstance(e, requests.exceptions.RequestException) and _is_transient(e))
        for e in chain
    ):
        return SiteTransient(str(exc))
    return SiteError(str(exc))


@contextmanager
def _translated_errors(host: str) -> Iterator[None]:
    """Turn scraper failures into `InfrastructureError`, a `HostFailure` or `SiteError`."""
    try:
        yield
    except ScraperError as exc:
        raise _classify(exc, host) from exc
    except requests.exceptions.RequestException as exc:
        # The scraper lets only its FlareSolverr session calls escape unwrapped.
        raise InfrastructureError(str(exc)) from exc


class ScraperAdapter:
    """Syncs tables of contents and fetches chapters through the scraper's `Novel` API."""

    def __init__(
        self, settings: Settings, resolver: Callable[[str], list[str]] = net.default_resolver
    ) -> None:
        self._settings = settings
        self._resolver = resolver
        take_over_scraper_logging()

    def supported_hosts(self) -> list[str]:
        """Every host of the installed decode guide, without `www.`, in guide order."""
        guide_path = Path(self._config().config_options["decode_guide_file"])
        guide = json.loads(guide_path.read_text(encoding="utf-8"))
        return list(dict.fromkeys(entry["host"].removeprefix("www.") for entry in guide))

    def sync_toc(self, novel_id: str, host: str, toc_url: str) -> TocResult:
        """Fetch every TOC page and return the chapter URLs in reading order."""
        with (
            _translated_errors(host),
            outbound_guard(
                allow_private=self._settings.allow_private_fetch, resolver=self._resolver
            ),
            flaresolverr_fallback(self._settings.flaresolverr_url),
        ):
            novel = self._open(novel_id, host, toc_url)
            with page_budget(self._settings.max_toc_pages):
                novel.sync_toc()
            first_page: str = novel.novel_data_helper.get_all_toc_fragments()[0]
            urls = self._normalised(novel.chapters_url_list)
        if not urls:
            msg = "The table of contents has no chapters"
            raise SiteError(msg)
        title = find_title(first_page)
        return TocResult(
            title=title,
            language=_html_language(first_page),
            cover_url=find_cover_url(first_page, toc_url, title),
            urls=urls,
            toc_html=first_page,
        )

    def fetch_chapter(self, novel_id: str, host: str, toc_url: str, url: str) -> FetchedChapter:
        """Download one chapter and return its title and content HTML."""
        with (
            _translated_errors(host),
            outbound_guard(
                allow_private=self._settings.allow_private_fetch, resolver=self._resolver
            ),
            flaresolverr_fallback(self._settings.flaresolverr_url),
        ):
            novel = self._open(novel_id, host, toc_url)
            chapter = Chapter(chapter_url=url)
            try:
                novel.scrap_chapter(chapter, reload_file=True)
                # The scraper invents a title when the page has none; ask the decoder instead.
                title: str | None = novel.decoder.get_chapter_title(chapter.chapter_html) or None
            finally:
                if chapter.chapter_html_filename:
                    novel.novel_data_helper.delete_chapter_html(chapter.chapter_html_filename)
            return FetchedChapter(title=title, body_html=chapter.chapter_content)

    def request_delay(self, host: str) -> float:
        """The host's own `request_time_between_requests` in seconds (0 when it sets none)."""
        config = self._config()
        config.set_host(host)
        delay = config.config_options["request_config"].get("request_time_between_requests")
        return float(delay or 0)

    def _config(self) -> ScraperConfig:
        """The scraper's per-host config, with our FlareSolverr address and no user config file."""
        scraper_dir = self._settings.scraper_dir
        return ScraperConfig(
            [
                {"config_file": str(scraper_dir / "no-config.json")},
                {"base_novels_dir": str(scraper_dir)},
                {"request_config.flaresolver_url": self._settings.flaresolverr_url},
            ]
        )

    def _open(self, novel_id: str, host: str, toc_url: str) -> Novel:
        """Create (or recreate) the novel in the scraper directory and load it ready to use."""
        config = self._config()
        # `Novel.new` does not build the decoder; saving and loading does.
        Novel.new(
            title=novel_id, scraper_config=config, toc_main_url=toc_url, host=host
        ).save_novel()
        novel = Novel.load(title=novel_id, scraper_config=config)
        novel.set_scraper_behavior(
            title_in_content="NO",
            force_flaresolver=True if host in FORCE_FLARESOLVERR_HOSTS else None,
        )
        request_config = dict(config.config_options["request_config"])
        request_config["request_retries"] = max(1, request_config["request_retries"])
        config.config_options["request_config"] = request_config
        return novel

    @staticmethod
    def _normalised(urls: list[str]) -> list[str]:
        """Normalise TOC chapter URLs; skip (and log) over-long ones, fail on other invalid ones."""
        normalised: dict[str, None] = {}
        for url in urls:
            try:
                normalised[normalize_url(url)] = None
            except UrlTooLong:
                log.get().warning("toc_url_too_long_skipped", url_prefix=url[:200])
            except ValidationFailed as exc:
                raise SiteError(str(exc)) from exc
        return list(normalised)
