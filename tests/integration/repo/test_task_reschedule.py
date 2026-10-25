"""Rescheduling real tasks to their host's next slot (host reach, `Tasks.reschedule`)."""

from datetime import timedelta

from sqlalchemy import insert

from novelhog.domain.tasks import PRIORITY_USER, TaskKind
from novelhog.repo import Repo, db
from novelhog.repo.schema import chapters, novels
from novelhog.repo.tasks import TaskQueue
from tests.conftest import FakeClock
from tests.integration.helpers import NOVEL_ID, chapter_row, insert_host, novel_row

ME = "worker-a"
TTL = 300
CHAPTER = "CH000000000001"


def _novel(repo: Repo) -> None:
    with db.write(repo.engine) as conn:
        insert_host(conn)
        conn.execute(insert(novels).values(**novel_row()))


class TestReschedule__ARefresh:
    def test_reschedule__appends_an_nv_event_with_the_new_next_refresh_at(
        self, repo: Repo, clock: FakeClock
    ) -> None:
        _novel(repo)
        queue = TaskQueue(repo.context)
        with db.write(repo.engine) as conn:
            queue.enqueue(
                conn, TaskKind.NOVEL_REFRESH, NOVEL_ID, NOVEL_ID, priority=300, run_after=clock()
            )
            queue.stamp(conn)
        task = repo.tasks.claim(ME, TTL)
        assert task is not None
        seq = repo.events.latest_seq()
        later = clock() + timedelta(minutes=5)

        assert repo.tasks.reschedule(task, ME, later) is True

        novel_events = [e for e in repo.events.since(seq) if e.entity_id == NOVEL_ID]
        assert len(novel_events) == 1
        assert (
            novel_events[0].payload["next_refresh_at"]
            == repo.novels.get(NOVEL_ID).to_json()["next_refresh_at"]
        )
        assert repo.novels.get(NOVEL_ID).next_refresh_at == later


class TestReschedule__ADownload:
    def test_reschedule__appends_no_chapter_event_and_keeps_queued_at(
        self, repo: Repo, clock: FakeClock
    ) -> None:
        _novel(repo)
        queue = TaskQueue(repo.context)
        with db.write(repo.engine) as conn:
            conn.execute(insert(chapters).values(**chapter_row(state="pending")))
            queue.enqueue(
                conn,
                TaskKind.CHAPTER_DOWNLOAD,
                CHAPTER,
                NOVEL_ID,
                priority=PRIORITY_USER,
                ordinal=1,
                run_after=clock(),
            )
            queue.stamp(conn)
        task = repo.tasks.claim(ME, TTL)
        assert task is not None and task.kind is TaskKind.CHAPTER_DOWNLOAD
        queued_at = repo.chapters.get(CHAPTER).queued_at
        seq = repo.events.latest_seq()

        assert repo.tasks.reschedule(task, ME, clock() + timedelta(minutes=5)) is True

        assert [e.entity_id for e in repo.events.since(seq)] == []
        assert repo.chapters.get(CHAPTER).queued_at == queued_at
