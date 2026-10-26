"""The worker-side chapter store: a thin facade over the stored chapter Markdown."""

from novelhog.repo import Repo


class ChapterStore:
    """Reads the Markdown of downloaded chapters."""

    def __init__(self, repo: Repo) -> None:
        self._repo = repo

    def get(self, chapter_id: str) -> str:
        """Return the chapter's Markdown; raise `NotFound` if it has no stored content."""
        return self._repo.contents.get(chapter_id)
