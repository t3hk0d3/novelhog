"""Tests for the host failure policy."""

from datetime import timedelta

import pytest

from novelhog.domain.hosts import (
    CHAPTER_MAX_RETRIES,
    HOST_PAUSE_CAP,
    FailureKind,
    chapter_retry_delay,
    pause_for,
)

MINUTE = timedelta(minutes=1)


class TestPauseFor__Throttled:
    def test_pause_for__pauses_for_retry_after_when_given(self) -> None:
        assert pause_for(FailureKind.THROTTLED, 1, retry_after=120) == timedelta(seconds=120)

    def test_pause_for__caps_retry_after_at_the_pause_cap(self) -> None:
        assert pause_for(FailureKind.THROTTLED, 1, retry_after=86400) == HOST_PAUSE_CAP

    def test_pause_for__starts_at_one_minute_without_retry_after(self) -> None:
        assert pause_for(FailureKind.THROTTLED, 1, retry_after=None) == MINUTE

    def test_pause_for__doubles_with_each_failure(self) -> None:
        assert pause_for(FailureKind.THROTTLED, 3, retry_after=None) == 4 * MINUTE


class TestPauseFor__Challenged:
    def test_pause_for__starts_at_fifteen_minutes(self) -> None:
        assert pause_for(FailureKind.CHALLENGED, 1, retry_after=None) == 15 * MINUTE

    def test_pause_for__doubles_and_caps_at_one_hour(self) -> None:
        assert pause_for(FailureKind.CHALLENGED, 2, retry_after=None) == 30 * MINUTE
        assert pause_for(FailureKind.CHALLENGED, 5, retry_after=None) == HOST_PAUSE_CAP


class TestPauseFor__Unreachable:
    def test_pause_for__does_not_pause_on_the_first_failure(self) -> None:
        assert pause_for(FailureKind.UNREACHABLE, 1, retry_after=None) is None

    def test_pause_for__pauses_five_minutes_on_the_second(self) -> None:
        assert pause_for(FailureKind.UNREACHABLE, 2, retry_after=None) == 5 * MINUTE

    def test_pause_for__doubles_from_there(self) -> None:
        assert pause_for(FailureKind.UNREACHABLE, 3, retry_after=None) == 10 * MINUTE

    def test_pause_for__caps_at_one_hour(self) -> None:
        assert pause_for(FailureKind.UNREACHABLE, 40, retry_after=None) == HOST_PAUSE_CAP


class TestPauseFor__WithoutHostEffect:
    @pytest.mark.parametrize("kind", [FailureKind.TRANSIENT, FailureKind.PERMANENT])
    def test_pause_for__never_pauses(self, kind: FailureKind) -> None:
        assert pause_for(kind, 10, retry_after=30) is None


class TestChapterRetryDelay:
    @pytest.mark.parametrize(("retries", "minutes"), [(0, 1), (1, 2), (2, 4), (3, 8), (4, 16)])
    def test_chapter_retry_delay__doubles_from_one_minute(self, retries: int, minutes: int) -> None:
        assert chapter_retry_delay(retries) == minutes * MINUTE

    def test_chapter_max_retries__is_five(self) -> None:
        assert CHAPTER_MAX_RETRIES == 5
