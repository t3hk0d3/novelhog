"""Title and cover finders (spec section 6.7)."""

import pytest

from novelhog.scraper_adapter.finders import find_cover_url, find_title

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
