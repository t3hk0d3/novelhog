"""Tests for text sanitising."""

import pytest

from novelhog.domain.text import strip_xml_invalid


class TestStripXmlInvalid__OnInvalidCharacters:
    @pytest.mark.parametrize(
        "char",
        ["\x00", "\x08", "\x0b", "\x0c", "\x0e", "\x1f", "\ud800", "\udfff", "￾", "￿"],
    )
    def test_strip_xml_invalid__removes(self, char: str) -> None:
        assert strip_xml_invalid(f"a{char}b") == "ab"


class TestStripXmlInvalid__OnValidText:
    @pytest.mark.parametrize("text", ["a\tb\nc\rd", "smile \U0001f600", "Роман \x7f"])
    def test_strip_xml_invalid__keeps(self, text: str) -> None:
        assert strip_xml_invalid(text) == text
