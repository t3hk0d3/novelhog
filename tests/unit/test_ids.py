"""Unit tests for novelhog.ids."""

import re

import pytest

from novelhog import ids


class TestNewId__WithKnownPrefix:
    def test_new_id__returns_prefix_and_twelve_lowercase_alphanumerics(self) -> None:
        assert re.fullmatch(r"NV[a-z0-9]{12}", ids.new_id("NV"))

    def test_new_id__two_calls_return_different_values(self) -> None:
        assert ids.new_id("CH") != ids.new_id("CH")


class TestNewId__WithUnknownPrefix:
    def test_new_id__raises_value_error(self) -> None:
        with pytest.raises(ValueError):
            ids.new_id("ZZ")


class TestIsValid__OnWellFormedId:
    def test_is_valid__returns_true(self) -> None:
        assert ids.is_valid("CHk3x9q2m7b4wz", "CH") is True


class TestIsValid__OnMalformedId:
    @pytest.mark.parametrize(
        "value",
        ["NVk3x9q2m7b4wz", "CHk3x9q2m7b4w", "CHk3x9q2m7b4wzz", "CHK3X9Q2M7B4WZ", "CHk3x9q2m7b4w!"],
        ids=["wrong_prefix", "too_short", "too_long", "uppercase", "symbol"],
    )
    def test_is_valid__returns_false(self, value: str) -> None:
        assert ids.is_valid(value, "CH") is False

    def test_is_valid__trailing_newline_returns_false(self) -> None:
        assert ids.is_valid("CHk3x9q2m7b4wz\n", "CH") is False
