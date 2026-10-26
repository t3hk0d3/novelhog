"""The demo library behind the documentation screenshots (`tests/fixtures/demo_backend.py`)."""

import pytest

from tests.fixtures.demo_backend import DEMO_HOST, DemoAdapter, DemoImages, cover_url, toc_url

MIN_TEXT_CHARS = 3000  # a chapter fills at least a phone screen and a half
FIRST_NOVEL = "alice-in-wonderland"
FIRST_NOVEL_CHAPTERS = 12


class TestDemoAdapter__SyncTocOfAKnownNovel:
    def test_sync_toc__returns_the_title_and_cover(self) -> None:
        toc = DemoAdapter().sync_toc("n1", DEMO_HOST, toc_url(FIRST_NOVEL))
        assert toc.title == "Alice's Adventures in Wonderland"
        assert toc.cover_url == cover_url(FIRST_NOVEL)

    def test_sync_toc__lists_every_chapter_once(self) -> None:
        toc = DemoAdapter().sync_toc("n1", DEMO_HOST, toc_url(FIRST_NOVEL))
        assert len(toc.urls) == FIRST_NOVEL_CHAPTERS
        assert len(set(toc.urls)) == FIRST_NOVEL_CHAPTERS

    def test_sync_toc__gives_each_chapter_its_title(self) -> None:
        toc = DemoAdapter().sync_toc("n1", DEMO_HOST, toc_url(FIRST_NOVEL))
        assert toc.chapter_titles[toc.urls[0]] == "Chapter I: Down the Rabbit-Hole"
        assert set(toc.chapter_titles) == set(toc.urls)


class TestDemoAdapter__SyncTocOfAnUnknownNovel:
    def test_sync_toc__raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="not in the demo library"):
            DemoAdapter().sync_toc("n1", DEMO_HOST, toc_url("no-such-novel"))


class TestDemoAdapter__FetchChapterWithText:
    def test_fetch_chapter__returns_its_title(self) -> None:
        toc = DemoAdapter().sync_toc("n1", DEMO_HOST, toc_url(FIRST_NOVEL))
        chapter = DemoAdapter().fetch_chapter("n1", DEMO_HOST, toc_url(FIRST_NOVEL), toc.urls[0])
        assert chapter.title == "Chapter I: Down the Rabbit-Hole"

    def test_fetch_chapter__returns_a_screenful_of_paragraphs(self) -> None:
        toc = DemoAdapter().sync_toc("n1", DEMO_HOST, toc_url(FIRST_NOVEL))
        chapter = DemoAdapter().fetch_chapter("n1", DEMO_HOST, toc_url(FIRST_NOVEL), toc.urls[0])
        assert chapter.body_html.count("<p>") >= 10
        assert len(chapter.body_html) >= MIN_TEXT_CHARS


class TestDemoAdapter__FetchChapterWithoutText:
    def test_fetch_chapter__raises_value_error(self) -> None:
        toc = DemoAdapter().sync_toc("n1", DEMO_HOST, toc_url(FIRST_NOVEL))
        with pytest.raises(ValueError, match="no text in the demo library"):
            DemoAdapter().fetch_chapter("n1", DEMO_HOST, toc_url(FIRST_NOVEL), toc.urls[-1])


class TestDemoImages__FetchOfACover:
    def test_fetch__returns_the_novels_jpeg(self) -> None:
        image = DemoImages().fetch(cover_url(FIRST_NOVEL))
        assert image is not None
        assert image.mime == "image/jpeg"
        assert image.data.startswith(b"\xff\xd8")


class TestDemoImages__FetchOfAnythingElse:
    def test_fetch__returns_none(self) -> None:
        assert DemoImages().fetch(f"https://{DEMO_HOST}/other.png") is None
