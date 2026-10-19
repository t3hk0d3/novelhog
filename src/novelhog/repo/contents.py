"""Chapter contents: the Markdown stored for each downloaded chapter (spec section 4)."""

from datetime import datetime

from sqlalchemy import Connection, select

from novelhog.domain.errors import NotFound
from novelhog.repo import db
from novelhog.repo.context import RepoContext
from novelhog.repo.schema import chapter_contents
from novelhog.repo.sql import dialect_insert


def put(conn: Connection, chapter_id: str, markdown: str, now: datetime) -> None:
    """Store the chapter's Markdown, replacing earlier content (internal; the caller locks)."""
    statement = dialect_insert(conn.engine, chapter_contents).values(
        chapter_id=chapter_id, markdown=markdown, fetched_at=now
    )
    conn.execute(
        statement.on_conflict_do_update(
            index_elements=["chapter_id"],
            set_={"markdown": statement.excluded.markdown, "fetched_at": now},
        )
    )


class Contents:
    """Reads stored chapter contents."""

    def __init__(self, context: RepoContext) -> None:
        self._engine = context.engine

    def get(self, chapter_id: str) -> str:
        """Return the chapter's Markdown; raise `NotFound` if it has no stored content."""
        with db.read(self._engine) as conn:
            markdown = conn.execute(
                select(chapter_contents.c.markdown).where(
                    chapter_contents.c.chapter_id == chapter_id
                )
            ).scalar_one_or_none()
        if markdown is None:
            raise NotFound("chapter_content", chapter_id)
        return str(markdown)
