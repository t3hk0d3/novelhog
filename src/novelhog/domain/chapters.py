"""Chapter states, client-allowed transitions and chapter selectors (spec section 5.4)."""

from dataclasses import dataclass
from enum import StrEnum

from novelhog.domain.errors import ValidationFailed


class ChapterState(StrEnum):
    """Lifecycle state of a chapter."""

    NOT_REQUESTED = "not_requested"
    PENDING = "pending"
    DOWNLOADING = "downloading"
    DOWNLOADED = "downloaded"
    ERROR = "error"


CLIENT_TARGETS: frozenset[ChapterState] = frozenset(
    {ChapterState.PENDING, ChapterState.NOT_REQUESTED}
)

_ALLOWED: frozenset[tuple[ChapterState, ChapterState]] = frozenset(
    {
        (ChapterState.NOT_REQUESTED, ChapterState.PENDING),
        (ChapterState.ERROR, ChapterState.PENDING),
        (ChapterState.DOWNLOADED, ChapterState.PENDING),
        (ChapterState.PENDING, ChapterState.NOT_REQUESTED),
    }
)


def transition_allowed(current: ChapterState, target: ChapterState) -> bool:
    """Report whether the API may move a chapter from `current` to `target`."""
    return (current, target) in _ALLOWED


@dataclass(frozen=True)
class ChapterRange:
    """An inclusive range of chapter positions."""

    start: int
    end: int


Selector = list[int] | ChapterRange


def resolve_selector(selector: Selector, total: int) -> list[int]:
    """Return the sorted, unique positions a selector names; every one must be in 1..total."""
    if total < 1:
        raise ValidationFailed("no_chapters", "The novel has no chapters")
    if isinstance(selector, ChapterRange):
        if selector.start > selector.end:
            raise ValidationFailed("invalid_selector", "Range start is after its end")
        positions = [selector.start, selector.end]
    else:
        if not selector:
            raise ValidationFailed("invalid_selector", "The chapter list is empty")
        positions = selector
    out_of_range = sorted({p for p in positions if p < 1 or p > total})
    if out_of_range:
        raise ValidationFailed(
            "invalid_selector",
            "Chapter positions are outside 1.." + str(total),
            {"out_of_range": out_of_range},
        )
    if isinstance(selector, ChapterRange):
        return list(range(selector.start, selector.end + 1))
    return sorted(set(selector))
