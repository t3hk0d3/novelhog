"""The worker-side chapter store."""

import pytest

from novelhog.chapter_store import ChapterStore
from novelhog.domain.errors import NotFound
from novelhog.repo import Repo, db
from novelhog.repo import contents as contents_repo
from tests.integration.worker.conftest import HOST, URL, add_chapter


@pytest.fixture
def novel_id(repo: Repo) -> str:
    repo.hosts.publish([HOST])
    return repo.novels.create(URL, followed=False).id


class TestChapterStore__Get:
    def test_get__returns_the_stored_markdown(self, repo: Repo, novel_id: str) -> None:
        chapter_id = add_chapter(repo, novel_id, 1, state="downloaded", queued_at=None)
        with db.write(repo.engine) as conn:
            contents_repo.put(conn, chapter_id, "# Hello\n\nWorld", repo.clock())

        assert ChapterStore(repo).get(chapter_id) == "# Hello\n\nWorld"

    def test_get__chapter_without_content_raises_not_found(
        self,
        repo: Repo,
        novel_id: str,
    ) -> None:
        chapter_id = add_chapter(repo, novel_id, 1)

        with pytest.raises(NotFound):
            ChapterStore(repo).get(chapter_id)
