"""Tests for the HTML to Markdown converter (spec section 8.1)."""

import base64
import re

import pytest
from markdown_it import MarkdownIt

from novelhog.html_to_markdown import Conversion, convert
from novelhog.images import FetchedImage

_MD = MarkdownIt("commonmark")


def _no_images(url: str) -> FetchedImage | None:
    return None


def md(html: str) -> str:
    return convert(html, _no_images).markdown


def rendered(markdown: str) -> str:
    """Render with markdown-it-py, dropping block newlines and normalising ``<br />``."""
    html: str = _MD.render(markdown)
    return html.replace("\n", "").replace("<br />", "<br>")


def inline_text(markdown: str) -> str:
    """Return the literal text of a single rendered paragraph, asserting it holds only text."""
    tokens = _MD.parse(markdown)
    assert [t.type for t in tokens] == ["paragraph_open", "inline", "paragraph_close"]
    children = tokens[1].children or []
    assert all(c.type in {"text", "text_special"} for c in children), children
    return "".join(c.content for c in children)


class TestConvert__OnAllowedInlineTags:
    def test_convert__em_and_i_become_single_asterisks(self) -> None:
        assert md("<p><em>a</em> and <i>b</i></p>") == "*a* and *b*"

    def test_convert__strong_and_b_become_double_asterisks(self) -> None:
        assert md("<p><strong>a</strong> and <b>b</b></p>") == "**a** and **b**"

    def test_convert__br_becomes_hard_line_break(self) -> None:
        out = md("<p>one<br>two</p>")
        assert rendered(out) == "<p>one<br>two</p>"

    def test_convert__marker_stays_outside_surrounding_spaces(self) -> None:
        out = md("<p>a<em> b </em>c</p>")
        assert rendered(out) == "<p>a <em>b</em> c</p>"

    def test_convert__trailing_br_leaves_no_stray_backslash(self) -> None:
        assert md("<p>one<br></p>") == "one"

    def test_convert__nested_emphasis_is_not_doubled(self) -> None:
        assert md("<p><b>a <b>b</b></b></p>") == "**a b**"


class TestConvert__OnParagraphs:
    def test_convert__separates_paragraphs_with_one_blank_line(self) -> None:
        assert md("<p>a</p>\n\n\n<p>b</p>") == "a\n\nb"

    def test_convert__drops_empty_paragraphs(self) -> None:
        assert md("<p>a</p><p></p><p> \n </p><p><br></p><p>b</p>") == "a\n\nb"

    def test_convert__keeps_chapter_headings_as_plain_paragraphs(self) -> None:
        out = md("<p>Chapter 29 The Return</p><p>text</p>")
        assert out == "Chapter 29 The Return\n\ntext"
        assert rendered(out) == "<p>Chapter 29 The Return</p><p>text</p>"

    def test_convert__converts_bare_p_fragments_without_wrapper(self) -> None:
        assert md("<p>a<p>b</p>") == "a\n\nb"

    def test_convert__keeps_loose_text_as_a_paragraph(self) -> None:
        assert md("plain <em>text</em><p>next</p>") == "plain *text*\n\nnext"

    def test_convert__collapses_whitespace(self) -> None:
        assert md("<p>a \n\t b</p>") == "a b"


class TestConvert__OnDroppedTags:
    def test_convert__removes_unknown_tags_and_keeps_text(self) -> None:
        assert md("<div><p>a <span>b</span> <a href='x'>c</a></p><p>d</p></div>") == "a b c\n\nd"

    @pytest.mark.parametrize("tag", ["script", "style", "noscript", "iframe"])
    def test_convert__removes_tag_with_contents(self, tag: str) -> None:
        assert md(f"<p>a</p><{tag}>secret <b>x</b></{tag}><p>b</p>") == "a\n\nb"

    def test_convert__removes_nested_noscript_entirely(self) -> None:
        assert md("<noscript><noscript>x</noscript>y</noscript><p>z</p>") == "z"


class TestConvert__OnEscaping:
    @pytest.mark.parametrize("char", list("\\*_#>[]`|~"))
    def test_convert__keeps_metacharacter_literal(self, char: str) -> None:
        text = f"a {char}b{char} c"
        assert inline_text(md(f"<p>{text}</p>")) == text

    @pytest.mark.parametrize("prefix", ["-", "+", "1.", "12)", "=", "---", "- - x"])
    def test_convert__leading_marker_does_not_start_block(self, prefix: str) -> None:
        text = f"{prefix} item"
        assert inline_text(md(f"<p>{text}</p>")) == text

    def test_convert__leading_marker_after_br_does_not_start_list(self) -> None:
        out = md("<p>a<br>- b</p>")
        assert rendered(out) == "<p>a<br>- b</p>"

    def test_convert__angle_bracket_and_entity_stay_literal(self) -> None:
        assert inline_text(md("<p>&lt;b&gt;x&lt;/b&gt; &amp;amp; &amp; y</p>")) == (
            "<b>x</b> &amp; & y"
        )

    def test_convert__escapes_inside_emphasis_and_items(self) -> None:
        out = md("<p><em>*a*</em></p><ul><li>- b</li></ul>")
        assert rendered(out) == "<p>*<em>a</em>*</p><ul><li>- b</li></ul>"


class TestConvert__OnLists:
    def test_convert__ul_becomes_dash_items(self) -> None:
        assert md("<ul><li>a</li><li>b</li></ul>") == "- a\n- b"

    def test_convert__ol_becomes_one_dot_items(self) -> None:
        assert md("<ol><li>a</li><li>b</li></ol>") == "1. a\n1. b"

    def test_convert__ol_keeps_start(self) -> None:
        out = md('<ol start="5"><li>a</li><li>b</li></ol>')
        assert out.startswith("5. a")
        assert rendered(out) == '<ol start="5"><li>a</li><li>b</li></ol>'

    def test_convert__ol_ignores_invalid_start(self) -> None:
        assert md('<ol start="x"><li>a</li></ol>') == "1. a"

    def test_convert__nested_lists_indent_four_spaces_per_level(self) -> None:
        out = md("<ul><li>a<ul><li>b<ol><li>c</li></ol></li></ul></li></ul>")
        assert out == "- a\n    - b\n        1. c"

    def test_convert__keeps_continuation_paragraphs_of_item(self) -> None:
        out = md("<ul><li><p>a</p><p>b</p></li><li>c</li></ul>")
        assert out == "- a\n\n    b\n- c"
        assert rendered(out) == "<ul><li><p>a</p><p>b</p></li><li><p>c</p></li></ul>"

    def test_convert__paragraph_after_nested_list_is_not_lazy_continuation(self) -> None:
        out = md("<ul><li>a<ul><li>b</li></ul><p>c</p></li></ul>")
        assert rendered(out) == "<ul><li><p>a</p><ul><li>b</li></ul><p>c</p></li></ul>"

    def test_convert__item_starting_with_nested_list_keeps_structure(self) -> None:
        out = md("<ul><li><ul><li>a</li><li>b</li></ul></li></ul>")
        assert rendered(out) == "<ul><li><ul><li>a</li><li>b</li></ul></li></ul>"

    def test_convert__nested_ol_with_start_stays_nested(self) -> None:
        out = md('<ul><li>a<ol start="3"><li>b</li></ol></li></ul>')
        assert rendered(out) == '<ul><li><p>a</p><ol start="3"><li>b</li></ol></li></ul>'

    def test_convert__dl_becomes_bold_term_then_definition(self) -> None:
        out = md("<dl><dt>Term</dt><dd>Meaning</dd><dt><b>T2</b></dt><dd>M2</dd></dl>")
        assert out == "**Term**\n\nMeaning\n\n**T2**\n\nM2"

    def test_convert__drops_empty_items(self) -> None:
        assert md("<ul><li>a</li><li> </li><li></li><li>b</li></ul>") == "- a\n- b"

    def test_convert__drops_empty_dd_and_dt(self) -> None:
        assert md("<dl><dt>T</dt><dd></dd><dt> </dt></dl>") == "**T**"

    def test_convert__drops_lists_left_empty(self) -> None:
        assert md("<p>a</p><ul><li></li></ul><ol></ol><ul><li><ul></ul></li></ul>") == "a"

    def test_convert__unclosed_items_close_implicitly(self) -> None:
        assert md("<ul><li>a<li>b</ul>") == "- a\n- b"

    def test_convert__rendering_gives_same_list_structure(self) -> None:
        out = md(
            "<p>intro</p><ul><li>a<ul><li>b</li><li>c</li></ul></li><li>d</li></ul>"
            "<ol><li>e</li></ol>"
        )
        assert rendered(out) == (
            "<p>intro</p><ul><li>a<ul><li>b</li><li>c</li></ul></li><li>d</li></ul>"
            "<ol><li>e</li></ol>"
        )


class TestConvert__OnImages:
    def test_convert__embeds_fetched_image(self) -> None:
        data = b"\x89PNGdata"
        seen: list[str] = []

        def fetch(url: str) -> FetchedImage | None:
            seen.append(url)
            return FetchedImage(data=data, mime="image/png")

        result = convert('<p>a <img src="https://x/y.png" alt="My [pic]"> b</p>', fetch)
        encoded = base64.b64encode(data).decode()
        assert result.markdown == f"a ![My \\[pic\\]](data:image/png;base64,{encoded}) b"
        assert seen == ["https://x/y.png"]

    def test_convert__sets_has_images_when_embedded(self) -> None:
        result = convert('<img src="u">', lambda _: FetchedImage(b"x", "image/gif"))
        assert result.has_images is True
        assert result.warnings == []

    def test_convert__unavailable_image_writes_literal_text(self) -> None:
        result = convert('<p>a <img src="https://x/y.png"> b</p>', _no_images)
        assert result.markdown == "a [image unavailable] b"

    def test_convert__unavailable_image_adds_warning(self) -> None:
        result = convert('<img src="https://x/y.png">', _no_images)
        assert len(result.warnings) == 1
        assert "https://x/y.png" in result.warnings[0]

    def test_convert__unavailable_image_keeps_has_images_false(self) -> None:
        assert convert('<img src="u">', _no_images).has_images is False

    def test_convert__image_without_src_is_unavailable_without_fetching(self) -> None:
        seen: list[str] = []

        def fetch(url: str) -> FetchedImage | None:
            seen.append(url)
            return None

        result = convert("<p><img alt='x'></p>", fetch)
        assert seen == []
        assert result.markdown == "[image unavailable]"
        assert len(result.warnings) == 1


class TestConvert__OnFetcherFailure:
    def test_convert__raising_fetcher_is_treated_as_unavailable(self) -> None:
        def fetch(url: str) -> FetchedImage | None:
            raise OSError(url)

        result = convert('<p>a <img src="https://x/y.png"> b</p>', fetch)
        assert result.markdown == "a [image unavailable] b"
        assert result.has_images is False
        assert len(result.warnings) == 1


class TestConvert__OnXmlInvalidCharacters:
    @pytest.mark.parametrize(
        "char",
        ["\x00", "\x08", "\x0b", "\x0c", "\x0e", "\x1f", "\ud800", "\udfff", "\ufffe", "\uffff"],
    )
    def test_convert__removes_xml_invalid_character(self, char: str) -> None:
        assert md(f"<p>a{char}b<em>c{char}</em></p>") == "ab*c*"

    def test_convert__removes_from_character_references(self) -> None:
        assert md("<p>a&#1;b&#x8;c</p>") == "abc"


class TestConvert__OnEmptyInput:
    @pytest.mark.parametrize("html", ["", "  \n", "<p></p>", "<script>x</script>"])
    def test_convert__returns_empty_markdown(self, html: str) -> None:
        assert convert(html, _no_images) == Conversion(markdown="", has_images=False, warnings=[])


class TestConvert__OnHostileInput:
    def test_convert__survives_very_deep_nesting(self) -> None:
        assert md("<b>" * 20000 + "x") == "**x**"
        assert md("<div>" * 20000 + "x") == "x"


class TestConvert__OnBlankParagraphs:
    @pytest.mark.parametrize(
        "spacer",
        ["&nbsp;", "&nbsp;&nbsp;", "&#8203;", "\ufeff", " &nbsp; &#8203; ", "<em>&nbsp;</em>"],
    )
    def test_convert__drops_paragraph_with_only(self, spacer: str) -> None:
        assert md(f"<p>a</p><p>{spacer}</p><p>b</p>") == "a\n\nb"

    def test_convert__keeps_nbsp_inside_real_text(self) -> None:
        assert md("<p>a&nbsp;b</p>") == "a\u00a0b"

    def test_convert__drops_blank_list_item(self) -> None:
        assert md("<ul><li>&nbsp;</li><li>a</li></ul>") == "- a"


def emphasis_html(html: str) -> str:
    """Convert, render, and assert that no literal asterisk reaches the reader."""
    out = rendered(md(html))
    plain = re.sub(r"<[^>]+>", "", out)
    assert "*" not in plain, (out, plain)
    return out


class TestConvert__OnEmphasisEdges:
    @pytest.mark.parametrize(
        ("html", "expected"),
        [
            ("<p><b>a</b><b>b</b></p>", "<p><strong>ab</strong></p>"),
            ("<p><em>a</em><em>b</em></p>", "<p><em>ab</em></p>"),
            ("<p><b>a</b> <b>b</b></p>", "<p><strong>a</strong> <strong>b</strong></p>"),
            ("<p>Dragon<i>'s</i> lair</p>", "<p>Dragon'<em>s</em> lair</p>"),
            ('<p>He said<em>"no"</em>then</p>', "<p>He said&quot;<em>no</em>&quot;then</p>"),
            ('<p><strong>"x"</strong>y</p>', "<p>&quot;<strong>x</strong>&quot;y</p>"),
            ("<p><em>word,</em>next</p>", "<p><em>word</em>,next</p>"),
            ("<p>他说<em>“不”</em>然后</p>", "<p>他说“<em>不</em>”然后</p>"),
            ("<p>a<em> b </em>c</p>", "<p>a <em>b</em> c</p>"),
            ("<p><b>(x)</b></p>", "<p>(<strong>x</strong>)</p>"),
            ("<p><b>a<br></b>b</p>", "<p><strong>a</strong><br>b</p>"),
            ("<p><b>x <i>y</i> z</b></p>", "<p><strong>x <em>y</em> z</strong></p>"),
            ("<p><i>a</i><b>b</b></p>", "<p><em>a</em><strong>b</strong></p>"),
            ("<p><b>a</b><i>b</i></p>", "<p><strong>a</strong><em>b</em></p>"),
        ],
    )
    def test_convert__renders_emphasis_without_literal_asterisks(
        self, html: str, expected: str
    ) -> None:
        assert emphasis_html(html) == expected

    @pytest.mark.parametrize(
        "html", ["<p><b>-</b></p>", "<p><em> </em>x<b></b></p>", "<p><b>!!</b></p>"]
    )
    def test_convert__run_without_word_characters_is_plain_text(self, html: str) -> None:
        assert "<em>" not in emphasis_html(html)
        assert "<strong>" not in emphasis_html(html)

    def test_convert__emphasis_around_image_is_plain_text(self) -> None:
        result = convert("<p><b><img src=u></b>x</p>", lambda _: FetchedImage(b"x", "image/png"))
        assert "**" not in result.markdown
        assert "*" not in re.sub(r"<[^>]+>", "", rendered(result.markdown))


class TestConvert__OnEmphasisAcrossBlocks:
    def test_convert__wrapping_emphasis_applies_inside_each_paragraph(self) -> None:
        assert md("<em><p>one</p><p>two</p></em>") == "*one*\n\n*two*"

    def test_convert__unclosed_emphasis_does_not_swallow_paragraphs(self) -> None:
        assert md("<i>x<p>a</p><p>b</p>") == "*x*\n\n*a*\n\n*b*"

    def test_convert__emphasis_wrapping_list_applies_inside_items(self) -> None:
        out = md("<b>x<ul><li>a</li></ul></b>")
        assert out == "**x**\n\n- **a**"
        assert rendered(out) == "<p><strong>x</strong></p><ul><li><strong>a</strong></li></ul>"

    def test_convert__definition_term_with_bold_inside_is_not_doubled(self) -> None:
        assert md("<dl><dt>a <b>b</b></dt></dl>") == "**a b**"


class TestConvert__OnBlockLevelDroppedTags:
    @pytest.mark.parametrize(
        ("html", "expected"),
        [
            ("<div>line one</div><div>line two</div>", "line one\n\nline two"),
            ("<h3>Chapter 1</h3>Text starts", "Chapter 1\n\nText starts"),
            ("<p>a</p><blockquote>quoted</blockquote>tail", "a\n\nquoted\n\ntail"),
            ("<table><tr><td>a</td><td>b</td></tr></table>", "a\n\nb"),
            ("<table><tr><th>a</th><th>b</th></tr><tr><td>c</td></tr></table>", "a\n\nb\n\nc"),
            ("a<hr>b", "a\n\nb"),
            ("<section>a</section><article>b</article><pre>c</pre>", "a\n\nb\n\nc"),
            ("<header>a</header><footer>b</footer>", "a\n\nb"),
            ("a<span>b</span>c", "abc"),
        ],
    )
    def test_convert__block_tag_ends_paragraph(self, html: str, expected: str) -> None:
        assert md(html) == expected


class TestConvert__OnConsecutiveBreaks:
    def test_convert__double_br_ends_paragraph(self) -> None:
        assert md("<p>a<br><br>b</p>") == "a\n\nb"

    def test_convert__double_br_with_whitespace_between_ends_paragraph(self) -> None:
        assert md("a<br> \n <br/>b<br><br><br>c") == "a\n\nb\n\nc"

    def test_convert__single_br_stays_a_hard_break(self) -> None:
        assert md("<p>a<br>b</p>") == "a\\\nb"

    def test_convert__trailing_double_br_leaves_no_empty_paragraph(self) -> None:
        assert md("<p>a<br><br></p><p>b</p>") == "a\n\nb"
