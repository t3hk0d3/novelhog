"""The worker-side chapter store: a thin facade over the stored chapters."""

from collections.abc import Sequence

from novelhog.repo import Repo


class ChapterStore:
    """Reads the Markdown and the headings of downloaded chapters."""

    def __init__(self, repo: Repo) -> None:
        self._repo = repo

    def get(self, chapter_id: str) -> str:
        """Return the chapter's Markdown; raise `NotFound` if it has no stored content."""
        return self._repo.contents.get(chapter_id)

    def headings(self, chapter_ids: Sequence[str]) -> dict[str, tuple[int, str | None]]:
        """Return `{id: (position, title)}` for those chapters that still have a position."""
        return self._repo.chapters.headings(chapter_ids)
