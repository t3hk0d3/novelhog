"""Chapter contents storage (spec section 4)."""

import pytest
from sqlalchemy import insert

from novelhog.domain.errors import NotFound
from novelhog.repo import Repo, db
from novelhog.repo import contents as contents_repo
from novelhog.repo.schema import chapters
from tests.conftest import FakeClock
from tests.integration.helpers import NOVEL_ID, chapter_row, insert_novel_with_chapter

CHAPTER_ID = chapter_row()["id"]


@pytest.fixture
def chapter(repo: Repo) -> str:
    with db.write(repo.engine) as conn:
        insert_novel_with_chapter(conn)
    return str(CHAPTER_ID)


class TestGet:
    def test_get__returns_the_stored_markdown(
        self, repo: Repo, chapter: str, clock: FakeClock
    ) -> None:
        with db.write(repo.engine) as conn:
            contents_repo.put(conn, chapter, "# Hello", clock.current)
        assert repo.contents.get(chapter) == "# Hello"

    def test_get__chapter_without_content_raises_not_found(self, repo: Repo, chapter: str) -> None:
        with pytest.raises(NotFound):
            repo.contents.get(chapter)

    def test_get__unknown_chapter_raises_not_found(self, repo: Repo) -> None:
        with pytest.raises(NotFound):
            repo.contents.get("CH000000000099")


class TestPut:
    def test_put__replaces_earlier_content(
        self, repo: Repo, chapter: str, clock: FakeClock
    ) -> None:
        with db.write(repo.engine) as conn:
            contents_repo.put(conn, chapter, "old", clock.current)
            contents_repo.put(conn, chapter, "new", clock.advance(seconds=5))
        assert repo.contents.get(chapter) == "new"

    def test_put__keeps_other_chapters_content(
        self, repo: Repo, chapter: str, clock: FakeClock
    ) -> None:
        with db.write(repo.engine) as conn:
            conn.execute(
                insert(chapters).values(
                    **chapter_row(id="CH000000000002", novel_id=NOVEL_ID, url="u/2", position=2)
                )
            )
            contents_repo.put(conn, chapter, "one", clock.current)
            contents_repo.put(conn, "CH000000000002", "two", clock.current)
        assert repo.contents.get(chapter) == "one"
