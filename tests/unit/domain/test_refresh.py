"""Tests for the adaptive refresh interval."""

import random

from novelhog.domain.refresh import jittered, next_interval

H = 3600
D = 24 * H
MIN, MAX = 6 * H, 30 * D


class TestNextInterval__OnNewChapters:
    def test_next_interval__returns_min(self) -> None:
        assert next_interval(8 * D, found_new=True, failed=False, min_s=MIN, max_s=MAX) == MIN


class TestNextInterval__OnNothingOrFailure:
    def test_next_interval__doubles_when_nothing_found(self) -> None:
        assert next_interval(MIN, found_new=False, failed=False, min_s=MIN, max_s=MAX) == 2 * MIN

    def test_next_interval__doubles_when_failed(self) -> None:
        assert next_interval(MIN, found_new=False, failed=True, min_s=MIN, max_s=MAX) == 2 * MIN

    def test_next_interval__is_capped_at_max(self) -> None:
        assert next_interval(20 * D, found_new=False, failed=False, min_s=MIN, max_s=MAX) == MAX


class TestNextInterval__WalkingFromSixHours:
    def test_next_interval__yields_the_documented_sequence(self) -> None:
        seen: list[int] = []
        current = MIN
        for _ in range(8):
            current = next_interval(current, found_new=False, failed=False, min_s=MIN, max_s=MAX)
            seen.append(current)
        assert seen == [12 * H, D, 2 * D, 4 * D, 8 * D, 16 * D, 30 * D, 30 * D]


class TestJittered:
    def test_jittered__stays_within_ten_percent(self) -> None:
        rng = random.Random(1)  # noqa: S311
        draws = [jittered(MIN, rng) for _ in range(1000)]
        assert all(0.9 * MIN <= d <= 1.1 * MIN for d in draws)
        assert len(set(draws)) > 1

    def test_jittered__is_deterministic_for_a_seed(self) -> None:
        first = [jittered(MIN, random.Random(7)) for _ in range(3)]  # noqa: S311
        second = [jittered(MIN, random.Random(7)) for _ in range(3)]  # noqa: S311
        assert first == second
