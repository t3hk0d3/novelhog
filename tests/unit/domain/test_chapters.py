"""Tests for chapter state rules and selectors."""

import pytest

from novelhog.domain.chapters import (
    CLIENT_TARGETS,
    ChapterRange,
    ChapterState,
    Selector,
    resolve_selector,
    transition_allowed,
)
from novelhog.domain.errors import ValidationFailed

S = ChapterState
ALLOWED = {
    (S.NOT_REQUESTED, S.PENDING),
    (S.ERROR, S.PENDING),
    (S.DOWNLOADED, S.PENDING),
    (S.PENDING, S.NOT_REQUESTED),
}


class TestTransitionAllowed__ToPending:
    @pytest.mark.parametrize("current", [S.NOT_REQUESTED, S.ERROR, S.DOWNLOADED])
    def test_transition_allowed__returns_true(self, current: ChapterState) -> None:
        assert transition_allowed(current, S.PENDING) is True


class TestTransitionAllowed__PendingToNotRequested:
    def test_transition_allowed__returns_true(self) -> None:
        assert transition_allowed(S.PENDING, S.NOT_REQUESTED) is True


class TestTransitionAllowed__AnyOtherPair:
    @pytest.mark.parametrize(
        ("current", "target"),
        [(a, b) for a in S for b in S if (a, b) not in ALLOWED],
    )
    def test_transition_allowed__returns_false(
        self, current: ChapterState, target: ChapterState
    ) -> None:
        assert transition_allowed(current, target) is False


class TestClientTargets:
    def test_client_targets__are_pending_and_not_requested(self) -> None:
        assert CLIENT_TARGETS == {S.PENDING, S.NOT_REQUESTED}


class TestResolveSelector__OnList:
    def test_resolve_selector__returns_sorted_unique(self) -> None:
        assert resolve_selector([5, 2, 5, 3], 10) == [2, 3, 5]


class TestResolveSelector__OnRange:
    def test_resolve_selector__returns_every_position_inclusive(self) -> None:
        assert resolve_selector(ChapterRange(2, 5), 10) == [2, 3, 4, 5]

    def test_resolve_selector__accepts_full_bounds(self) -> None:
        assert resolve_selector(ChapterRange(1, 3), 3) == [1, 2, 3]


class TestResolveSelector__OnEmptyList:
    def test_resolve_selector__raises_invalid_selector(self) -> None:
        with pytest.raises(ValidationFailed) as info:
            resolve_selector([], 10)
        assert info.value.code == "invalid_selector"


class TestResolveSelector__OnOutOfBounds:
    @pytest.mark.parametrize(
        ("selector", "expected"),
        [
            ([0, 3], [0]),
            ([3, 11], [11]),
            ([11, 0, 11, -2], [-2, 0, 11]),
            (ChapterRange(0, 3), [0]),
            (ChapterRange(3, 11), [11]),
            (ChapterRange(0, 11), [0, 11]),
        ],
    )
    def test_resolve_selector__raises_with_out_of_range_details(
        self, selector: Selector, expected: list[int]
    ) -> None:
        with pytest.raises(ValidationFailed) as info:
            resolve_selector(selector, 10)
        assert info.value.code == "invalid_selector"
        assert info.value.details == {"out_of_range": expected}


class TestResolveSelector__OnReversedRange:
    def test_resolve_selector__raises_invalid_selector(self) -> None:
        with pytest.raises(ValidationFailed) as info:
            resolve_selector(ChapterRange(5, 2), 10)
        assert info.value.code == "invalid_selector"


class TestResolveSelector__OnNoChapters:
    @pytest.mark.parametrize("selector", [[1], ChapterRange(1, 1), []])
    def test_resolve_selector__raises_invalid_selector(self, selector: object) -> None:
        assert isinstance(selector, list | ChapterRange)
        with pytest.raises(ValidationFailed) as info:
            resolve_selector(selector, 0)
        assert info.value.code == "invalid_selector"
