"""Adaptive refresh interval (spec section 6.2)."""

import random


def next_interval(current: int, *, found_new: bool, failed: bool, min_s: int, max_s: int) -> int:
    """Return the next refresh interval: reset on new chapters, otherwise double up to `max_s`."""
    if found_new and not failed:
        return min_s
    return min(current * 2, max_s)


def jittered(interval: int, rng: random.Random) -> int:
    """Return `interval` randomised by up to plus or minus 10 percent."""
    low = -(-interval * 9 // 10)
    high = interval * 11 // 10
    return rng.randint(low, high)
