"""Title, chapter title and cover finders (spec section 6.7)."""

import pytest

from novelhog.scraper_adapter.finders import (
    clean_chapter_title,
    find_chapter_titles,
    find_cover_url,
    find_title,
)

TOC = "https://example.org/series/12/lantern/"


def page(head: str = "", body: str = "") -> str:
    return f"<html><head>{head}</head><body>{body}</body></html>"


class TestFindTitle__Sources:
    def test_find_title__prefers_og_title(self) -> None:
        html = page(
            '<meta property="og:title" content="Og Name"><title>Other</title>', "<h1>H</h1>"
        )
        assert find_title(html) == "Og Name"

    def test_find_title__uses_title_without_dash_suffix(self) -> None:
        assert find_title(page("<title>Glass Orchard - Example Site</title>")) == "Glass Orchard"

    def test_find_title__uses_title_without_pipe_suffix(self) -> None:
        assert find_title(page("<title>Glass Orchard | Example Site</title>")) == "Glass Orchard"

    def test_find_title__keeps_inner_dash_of_title(self) -> None:
        assert find_title(page("<title>Glass-Orchard</title>")) == "Glass-Orchard"

    def test_find_title__falls_back_to_first_h1(self) -> None:
        assert find_title(page(body="<h1>First</h1><h1>Second</h1>")) == "First"

    def test_find_title__skips_empty_og_title(self) -> None:
        html = page('<meta property="og:title" content=" ">', "<h1>Fallback</h1>")
        assert find_title(html) == "Fallback"


class TestFindTitle__TitleExtendingTheH1:
    def test_find_title__drops_words_the_title_adds_after_the_h1(self) -> None:
        html = page(
            "<title>Star Forge Titan Novel Read Online</title>"
            '<h1 class="novel-title">Star Forge Titan</h1>'
        )
        assert find_title(html) == "Star Forge Titan"

    def test_find_title__keeps_the_title_when_the_h1_is_only_a_word_prefix(self) -> None:
        html = page("<title>Starlight Road</title><h1>Star</h1>")
        assert find_title(html) == "Starlight Road"

    def test_find_title__keeps_the_title_when_it_does_not_start_with_the_h1(self) -> None:
        html = page("<title>Glass Orchard</title><h1>Chapter List</h1>")
        assert find_title(html) == "Glass Orchard"

    def test_find_title__og_title_still_wins(self) -> None:
        html = page(
            '<meta property="og:title" content="Glass Orchard">'
            "<title>Star Forge Titan Novel Read Online</title><h1>Star Forge Titan</h1>"
        )
        assert find_title(html) == "Glass Orchard"


class TestFindTitle__Cleaning:
    def test_find_title__strips_xml_invalid_characters_and_whitespace(self) -> None:
        html = page('<meta property="og:title" content="  Glass\x0b Orchard\x00 ">')
        assert find_title(html) == "Glass Orchard"


class TestFindTitle__Details:
    def test_find_title__collapses_whitespace_in_og_title(self) -> None:
        assert find_title(page('<meta property="og:title" content="A\n\n  B">')) == "A B"

    def test_find_title__skips_empty_h1(self) -> None:
        assert find_title(page(body="<h1> </h1><h1>Real</h1>")) == "Real"


class TestFindersFailSoft:
    def test_finders__return_none_and_log_when_parsing_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(_html: str) -> None:
            raise RuntimeError("boom")

        monkeypatch.setattr("novelhog.scraper_adapter.finders._parse", boom)
        assert find_title("<html>") is None
        assert find_cover_url("<html>", TOC, None) is None


class TestFindTitle__NothingMatches:
    def test_find_title__returns_none(self) -> None:
        assert find_title(page(body="<p>text</p>")) is None


class TestFindCoverUrl__Sources:
    def test_find_cover_url__prefers_og_image(self) -> None:
        html = page(
            '<meta property="og:image" content="https://cdn.example/og.jpg">'
            '<meta name="twitter:image" content="https://cdn.example/tw.jpg">',
            '<div class="cover"><img src="/c.jpg"></div>',
        )
        assert find_cover_url(html, TOC, None) == "https://cdn.example/og.jpg"

    def test_find_cover_url__uses_twitter_image(self) -> None:
        html = page('<meta name="twitter:image" content="https://cdn.example/tw.jpg">')
        assert find_cover_url(html, TOC, None) == "https://cdn.example/tw.jpg"

    def test_find_cover_url__uses_schema_org_itemprop(self) -> None:
        html = page(
            body='<img itemprop="image" src="/schema.jpg"><figure><img src="/f.jpg"></figure>'
        )
        assert find_cover_url(html, TOC, None) == "https://example.org/schema.jpg"

    def test_find_cover_url__uses_schema_org_json_ld(self) -> None:
        ld = '{"@type": "Book", "image": {"url": "https://cdn.example/ld.jpg"}}'
        html = page(f'<script type="application/ld+json">{ld}</script>')
        assert find_cover_url(html, TOC, None) == "https://cdn.example/ld.jpg"

    def test_find_cover_url__uses_first_string_of_json_ld_image_list(self) -> None:
        ld = (
            '{"@type": "Book", "image": ["https://cdn.example/a.jpg", "https://cdn.example/b.jpg"]}'
        )
        html = page(f'<script type="application/ld+json">{ld}</script>')
        assert find_cover_url(html, TOC, None) == "https://cdn.example/a.jpg"

    def test_find_cover_url__uses_first_image_object_url_of_json_ld_list(self) -> None:
        ld = '{"image": [{"@type": "ImageObject", "url": "https://cdn.example/y.jpg"}]}'
        html = page(f'<script type="application/ld+json">{ld}</script>')
        assert find_cover_url(html, TOC, None) == "https://cdn.example/y.jpg"

    def test_find_cover_url__survives_deeply_nested_json_ld(self) -> None:
        for ld in ("[" * 500 + "]" * 500, '{"@graph":' * 500 + "1" + "}" * 500):
            html = page(
                f'<script type="application/ld+json">{ld}</script>',
                '<figure><img src="/f.jpg"></figure>',
            )
            assert find_cover_url(html, TOC, None) == "https://example.org/f.jpg"

    def test_find_cover_url__prefers_data_src_over_placeholder_src(self) -> None:
        html = page(
            body='<figure><img src="data:image/gif;base64,AAAA" data-src="/r.jpg"></figure>'
        )
        assert find_cover_url(html, TOC, None) == "https://example.org/r.jpg"

    def test_find_cover_url__ignores_broken_json_ld(self) -> None:
        html = page(
            '<script type="application/ld+json">{nope</script>',
            '<figure><img src="/f.jpg"></figure>',
        )
        assert find_cover_url(html, TOC, None) == "https://example.org/f.jpg"

    def test_find_cover_url__uses_common_container_selectors(self) -> None:
        for container in (
            '<div class="cover">',
            '<div class="book x">',
            '<div class="novel-cover">',
            "<figure>",
        ):
            closing = "</figure>" if container == "<figure>" else "</div>"
            html = page(body=f'<img src="/o.jpg">{container}<i><img src="/c.jpg"></i>{closing}')
            assert find_cover_url(html, TOC, None) == "https://example.org/c.jpg", container

    def test_find_cover_url__uses_first_img_whose_alt_equals_title(self) -> None:
        html = page(body='<img src="/a.jpg" alt="Banner"><img src="/b.jpg" alt="Glass Orchard">')
        assert find_cover_url(html, TOC, "Glass Orchard") == "https://example.org/b.jpg"


class TestFindCoverUrl__Resolution:
    def test_find_cover_url__resolves_relative_url_against_toc_url(self) -> None:
        html = page('<meta property="og:image" content="../img/c.jpg">')
        assert find_cover_url(html, TOC, None) == "https://example.org/series/12/img/c.jpg"

    def test_find_cover_url__resolves_protocol_relative_url(self) -> None:
        html = page('<meta property="og:image" content="//cdn.example/c.jpg">')
        assert find_cover_url(html, TOC, None) == "https://cdn.example/c.jpg"

    def test_find_cover_url__skips_data_uris(self) -> None:
        html = page(
            '<meta property="og:image" content="data:image/png;base64,AAAA">',
            '<figure><img src="/f.jpg"></figure>',
        )
        assert find_cover_url(html, TOC, None) == "https://example.org/f.jpg"


class TestFindCoverUrl__NothingMatches:
    def test_find_cover_url__returns_none(self) -> None:
        html = page(body='<img src="/a.jpg" alt="Banner">')
        assert find_cover_url(html, TOC, "Glass Orchard") is None


FANMTL_LINK = (
    '<a href="/novel/x_1.html" title="X Chapter 1"><span class="chapter-no">1</span>'
    '<strong class="chapter-title"> Chapter 1: Paper Tide </strong>'
    '<time class="chapter-update">2 days ago</time></a>'
)


class TestFindChapterTitles__Sources:
    def test_find_chapter_titles__takes_the_element_whose_class_names_a_title(self) -> None:
        titles = find_chapter_titles([page(body=FANMTL_LINK)], TOC)
        assert titles == {"https://example.org/novel/x_1.html": "Chapter 1: Paper Tide"}

    def test_find_chapter_titles__takes_the_text_of_a_text_only_link(self) -> None:
        html = page(body='<a href="https://example.org/c/2">Chapter 2:\n  Copper Fog</a>')
        assert find_chapter_titles([html], TOC) == {
            "https://example.org/c/2": "Chapter 2: Copper Fog"
        }

    def test_find_chapter_titles__skips_a_link_with_markup_but_no_title_element(self) -> None:
        html = page(body='<a href="/c/3"><span>3</span><time>today</time></a>')
        assert find_chapter_titles([html], TOC) == {}

    def test_find_chapter_titles__collects_every_page(self) -> None:
        pages = [page(body='<a href="/c/1">One</a>'), page(body='<a href="/c/2">Two</a>')]
        assert find_chapter_titles(pages, TOC) == {
            "https://example.org/c/1": "One",
            "https://example.org/c/2": "Two",
        }


class TestFindChapterTitles__Urls:
    def test_find_chapter_titles__resolves_and_normalises_hrefs(self) -> None:
        html = page(body='<a href="https://www.example.org/c/1/">One</a><a href="../2">Two</a>')
        assert find_chapter_titles([html], TOC) == {
            "https://example.org/c/1": "One",
            "https://example.org/series/12/2": "Two",
        }

    def test_find_chapter_titles__first_link_to_a_url_wins(self) -> None:
        html = page(body='<a href="/c/1">One</a><a href="/c/1">Again</a>')
        assert find_chapter_titles([html], TOC) == {"https://example.org/c/1": "One"}

    def test_find_chapter_titles__skips_links_without_a_usable_url(self) -> None:
        html = page(body='<a href="mailto:x@example.org">Mail</a><a>No href</a>')
        assert find_chapter_titles([html], TOC) == {}


class TestFindChapterTitles__SeveralLinksToOneChapter:
    def test_find_chapter_titles__a_title_element_beats_earlier_link_text(self) -> None:
        html = page(body=f'<a href="/novel/x_1.html">Chapter 1</a> {FANMTL_LINK}')
        assert find_chapter_titles([html], TOC) == {
            "https://example.org/novel/x_1.html": "Chapter 1: Paper Tide"
        }

    def test_find_chapter_titles__later_link_text_does_not_replace_a_title_element(self) -> None:
        html = page(body=f'{FANMTL_LINK} <a href="/novel/x_1.html">Chapter 1</a>')
        assert find_chapter_titles([html], TOC) == {
            "https://example.org/novel/x_1.html": "Chapter 1: Paper Tide"
        }


class TestFindChapterTitles__FailSoft:
    def test_find_chapter_titles__logs_and_gives_nothing_when_a_page_is_not_text(self) -> None:
        assert find_chapter_titles([None], TOC) == {}  # type: ignore[list-item]


class TestCleanChapterTitle:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Dog blood&#039;s life", "Dog blood's life"),
            ("Tom &amp; Jerry", "Tom & Jerry"),
            ("  Chapter\n 1:\tStart  ", "Chapter 1: Start"),
            ("Bad\x00 char", "Bad char"),
            ("Plain", "Plain"),
        ],
    )
    def test_clean_chapter_title__gives_plain_text(self, raw: str, expected: str) -> None:
        assert clean_chapter_title(raw) == expected

    @pytest.mark.parametrize("raw", [None, "", "  ", "\x00"])
    def test_clean_chapter_title__gives_none_when_nothing_is_left(self, raw: str | None) -> None:
        assert clean_chapter_title(raw) is None
