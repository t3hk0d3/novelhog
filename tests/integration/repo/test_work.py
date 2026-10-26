"""The repo half of chapter downloads and v1 recovery (spec 4, 6.3, 6.4; tasks spec 5, 8).

Downloads are `chapter_download` tasks: `repo.tasks.start` turns a chapter `downloading`,
`repo.tasks.finish` applies v1's `complete` and `fail` (or puts it back in line on `Retry`), and
`repo.tasks.recover` resets lapsed downloads. `repo.work.recover` keeps only the consumers.
"""

from typing import Any

import pytest
from sqlalchemy import insert, select, update

from novelhog.domain.chapters import ChapterState
from novelhog.domain.errors import NotFound
from novelhog.domain.tasks import Done, Failed, Retry, StartResult, TaskState
from novelhog.repo import Repo, db
from novelhog.repo.schema import chapters, events, exports, novels
from novelhog.repo.task_kinds.chapter_download import Downloaded
from novelhog.repo.tasks import NotStarted, Ready, TaskRow
from novelhog.repo.work import RecoveryReport
from tests.conftest import FakeClock
from tests.integration.helpers import export_row, novel_row
from tests.integration.task_fakes import claim_task, live_task, start_task, tasks_of
from tests.integration.worker.conftest import add_chapter

URL = "https://example.com/novel"
TTL = 300
ME = "worker-a"
OTHER = "worker-b"


@pytest.fixture
def novel_id(repo: Repo) -> str:
    repo.hosts.publish(["example.com"])
    return repo.novels.create(URL, followed=True).id


def cid(position: int) -> str:
    return f"CH{position:012d}"


def row_of(repo: Repo, chapter_id: str) -> Any:
    with db.read(repo.engine) as conn:
        return conn.execute(select(chapters).where(chapters.c.id == chapter_id)).one()


def event_count(repo: Repo) -> int:
    return repo.events.latest_seq()


def chapter_events(repo: Repo, entity_id: str) -> list[Any]:
    with db.read(repo.engine) as conn:
        return list(
            conn.execute(
                select(events).where(events.c.entity_id == entity_id).order_by(events.c.seq)
            ).all()
        )


def started(repo: Repo, novel_id: str, position: int = 1, worker: str = ME) -> Ready[Any]:
    """A chapter queued, claimed and started by `worker`."""
    add_chapter(repo, novel_id, position)
    return start_task(repo, cid(position), worker, TTL)


def done(markdown: str = "x", title: str = "t", has_images: bool = False) -> Done[Downloaded]:
    return Done(Downloaded(markdown, title, has_images))


def finish(repo: Repo, task: TaskRow, outcome: Any, worker: str = ME) -> bool:
    return repo.tasks.finish(task, worker, outcome) is not None


class TestStart:
    def test_start__owned_pending_chapter_becomes_downloading_with_events(
        self, repo: Repo, novel_id: str
    ) -> None:
        add_chapter(repo, novel_id, 1)
        claimed = claim_task(repo, cid(1), ME, TTL)
        before = event_count(repo)

        assert isinstance(repo.tasks.start(claimed, ME), Ready)

        assert row_of(repo, cid(1)).state == "downloading"
        assert event_count(repo) == before + 2  # CH + NV
        assert chapter_events(repo, cid(1))[-1].payload["state"] == "downloading"

    def test_start__dequeued_after_the_claim_is_cancelled_and_changes_nothing(
        self, repo: Repo, novel_id: str
    ) -> None:
        add_chapter(repo, novel_id, 1)
        claimed = claim_task(repo, cid(1), ME, TTL)
        repo.chapters.patch(novel_id, ChapterState.NOT_REQUESTED, [1])
        before = event_count(repo)

        assert repo.tasks.start(claimed, ME) == NotStarted(StartResult.CANCELLED)

        assert row_of(repo, cid(1)).state == "not_requested"
        assert event_count(repo) == before

    def test_start__claim_taken_by_another_worker_is_lost_and_changes_nothing(
        self, repo: Repo, novel_id: str, clock: FakeClock
    ) -> None:
        add_chapter(repo, novel_id, 1)
        claimed = claim_task(repo, cid(1), ME, TTL)
        clock.advance(seconds=TTL + 1)
        claim_task(repo, cid(1), OTHER, TTL)
        before = event_count(repo)

        assert repo.tasks.start(claimed, ME) == NotStarted(StartResult.LOST)

        assert row_of(repo, cid(1)).state == "pending"
        assert live_task(repo, cid(1)).claimed_by == OTHER
        assert event_count(repo) == before

    def test_start__deleted_novel_is_lost(self, repo: Repo, novel_id: str) -> None:
        add_chapter(repo, novel_id, 1)
        claimed = claim_task(repo, cid(1), ME, TTL)
        repo.novels.delete(novel_id)

        assert repo.tasks.start(claimed, ME) == NotStarted(StartResult.LOST)


class TestComplete:
    def test_complete__stores_content_and_marks_the_chapter_downloaded(
        self, repo: Repo, novel_id: str
    ) -> None:
        task = started(repo, novel_id).task

        assert finish(repo, task, done("# Hi", "One", has_images=True))

        row = row_of(repo, cid(1))
        assert (row.state, row.title, row.has_images) == ("downloaded", "One", True)
        assert repo.contents.get(cid(1)) == "# Hi"
        assert repo.chapters.get(cid(1)).queued_at is None
        assert live_task(repo, cid(1), TaskState.DONE).claimed_by is None

    def test_complete__appends_events_whose_counters_match_the_novel(
        self, repo: Repo, novel_id: str
    ) -> None:
        finish(repo, started(repo, novel_id).task, done())

        with db.read(repo.engine) as conn:
            newest = conn.execute(select(events).order_by(events.c.seq.desc())).first()
        assert chapter_events(repo, cid(1))[-1].payload["state"] == "downloaded"
        assert newest is not None
        view = repo.novels.get(novel_id)
        assert newest.entity_id == novel_id
        assert newest.payload["downloaded_pages"] == view.downloaded_pages == 1
        assert newest.payload["pending_pages"] == view.pending_pages == 0

    def test_complete__replaces_earlier_content_on_a_re_download(
        self, repo: Repo, novel_id: str
    ) -> None:
        finish(repo, started(repo, novel_id).task, done("old"))
        repo.chapters.patch(novel_id, ChapterState.PENDING, [1])

        finish(repo, start_task(repo, cid(1), ME, TTL).task, done("new"))

        assert repo.contents.get(cid(1)) == "new"

    def test_complete__claim_expired_and_recovered_stores_nothing(
        self, repo: Repo, novel_id: str, clock: FakeClock
    ) -> None:
        task = started(repo, novel_id).task
        clock.advance(seconds=TTL + 1)
        repo.tasks.recover()
        before = event_count(repo)

        assert not finish(repo, task, done())

        assert row_of(repo, cid(1)).state == "pending"
        assert event_count(repo) == before
        with pytest.raises(NotFound):
            repo.contents.get(cid(1))

    def test_complete__claim_expired_without_recovery_stores_nothing(
        self, repo: Repo, novel_id: str, clock: FakeClock
    ) -> None:
        task = started(repo, novel_id).task
        clock.advance(seconds=TTL + 1)

        assert not finish(repo, task, done())

        assert row_of(repo, cid(1)).state == "downloading"

    def test_complete__reclaimed_by_another_worker_stores_nothing(
        self, repo: Repo, novel_id: str, clock: FakeClock
    ) -> None:
        task = started(repo, novel_id).task
        clock.advance(seconds=TTL + 1)
        repo.tasks.recover()
        claim_task(repo, cid(1), OTHER, TTL)

        assert not finish(repo, task, done())

        assert live_task(repo, cid(1)).claimed_by == OTHER

    def test_complete__deleted_novel_stores_nothing(self, repo: Repo, novel_id: str) -> None:
        task = started(repo, novel_id).task
        repo.novels.delete(novel_id)

        assert not finish(repo, task, done())

        with pytest.raises(NotFound):
            repo.contents.get(cid(1))


class TestFail:
    def test_fail__sets_error_and_ends_the_task_failed(self, repo: Repo, novel_id: str) -> None:
        task = started(repo, novel_id).task

        assert finish(repo, task, Failed("ValueError: boom"))

        assert row_of(repo, cid(1)).state == "error"
        assert repo.chapters.get(cid(1)).queued_at is None
        failed = live_task(repo, cid(1), TaskState.FAILED)
        assert (failed.error, failed.claimed_by) == ("ValueError: boom", None)

    def test_fail__ch_event_carries_error_and_error_at(self, repo: Repo, novel_id: str) -> None:
        finish(repo, started(repo, novel_id).task, Failed("ValueError: boom"))

        payload = chapter_events(repo, cid(1))[-1].payload
        assert payload["state"] == "error"
        assert payload["error"] == "ValueError: boom"
        assert payload["error_at"] is not None
        assert repo.chapters.get(cid(1)).error == "ValueError: boom"

    def test_fail__lost_claim_changes_nothing(
        self, repo: Repo, novel_id: str, clock: FakeClock
    ) -> None:
        task = started(repo, novel_id).task
        clock.advance(seconds=TTL + 1)
        repo.tasks.recover()
        before = event_count(repo)

        assert not finish(repo, task, Failed("late"))

        assert row_of(repo, cid(1)).state == "pending"
        assert event_count(repo) == before


class TestRetry:
    def test_retry__moves_the_chapter_back_to_pending_keeping_queued_at(
        self, repo: Repo, novel_id: str, clock: FakeClock
    ) -> None:
        queued_at = clock.current
        task = started(repo, novel_id).task
        clock.advance(minutes=1)

        assert finish(repo, task, Retry(task.run_after, "InfrastructureError: dns"))

        assert row_of(repo, cid(1)).state == "pending"
        assert repo.chapters.get(cid(1)).queued_at == queued_at
        assert chapter_events(repo, cid(1))[-1].payload["state"] == "pending"
        requeued = live_task(repo, cid(1))
        assert (requeued.id, requeued.claimed_by) == (task.id, None)

    def test_retry__of_a_removed_chapter_returns_it_to_not_requested_and_cancels_the_task(
        self, repo: Repo, novel_id: str, clock: FakeClock
    ) -> None:
        task = started(repo, novel_id).task
        with db.write(repo.engine) as conn:
            conn.execute(update(chapters).values(position=None, removed_at=clock.current))

        assert finish(repo, task, Retry(task.run_after, "InfrastructureError: dns"))

        assert row_of(repo, cid(1)).state == "not_requested"
        assert [t.state for t in tasks_of(repo, cid(1))] == [TaskState.CANCELLED]

    def test_retry__by_another_worker_changes_nothing(self, repo: Repo, novel_id: str) -> None:
        task = started(repo, novel_id).task

        assert not finish(repo, task, Retry(task.run_after, "e"), worker=OTHER)

        assert row_of(repo, cid(1)).state == "downloading"


class TestRecover:
    def test_recover__lapsed_downloads_go_back_to_pending_with_events(
        self, repo: Repo, novel_id: str, clock: FakeClock
    ) -> None:
        queued = clock.current
        task = started(repo, novel_id).task
        clock.advance(seconds=TTL + 1)
        before = event_count(repo)

        report = repo.tasks.recover()

        assert row_of(repo, cid(1)).state == "pending"
        assert repo.chapters.get(cid(1)).queued_at == queued
        assert (report.requeued, report.claims_cleared) == (1, 0)
        assert event_count(repo) == before + 2
        assert chapter_events(repo, cid(1))[-1].payload["state"] == "pending"
        recovered = live_task(repo, cid(1))
        assert (recovered.id, recovered.run_after, recovered.claimed_by) == (
            task.id, task.run_after, None,
        )  # fmt: skip

    def test_recover__lapsed_claims_of_queued_downloads_are_cleared_without_events(
        self, repo: Repo, novel_id: str, clock: FakeClock
    ) -> None:
        add_chapter(repo, novel_id, 1)
        claim_task(repo, cid(1), ME, TTL)
        clock.advance(seconds=TTL + 1)
        before = event_count(repo)

        report = repo.tasks.recover()

        assert row_of(repo, cid(1)).state == "pending"
        assert live_task(repo, cid(1)).claimed_by is None
        assert report.claims_cleared == 1
        assert event_count(repo) == before

    def test_recover__removed_downloading_chapter_returns_to_not_requested(
        self, repo: Repo, novel_id: str, clock: FakeClock
    ) -> None:
        started(repo, novel_id)
        with db.write(repo.engine) as conn:
            conn.execute(update(chapters).values(position=None, removed_at=clock.current))
        clock.advance(seconds=TTL + 1)

        repo.tasks.recover()

        row = row_of(repo, cid(1))
        assert (row.state, row.position) == ("not_requested", None)
        assert [t.state for t in tasks_of(repo, cid(1))] == [TaskState.CANCELLED]

    def test_recover__recovers_downloads_of_several_novels(
        self, repo: Repo, novel_id: str, clock: FakeClock
    ) -> None:
        second = "NV000000000002"
        with db.write(repo.engine) as conn:
            conn.execute(
                insert(novels).values(**novel_row(id=second, toc_url="https://example.com/other"))
            )
        started(repo, novel_id, 1)
        started(repo, second, 2)
        clock.advance(seconds=TTL + 1)

        assert repo.tasks.recover().requeued == 2

        assert {row_of(repo, cid(1)).state, row_of(repo, cid(2)).state} == {"pending"}

    def test_recover__live_claims_are_left_alone(self, repo: Repo, novel_id: str) -> None:
        started(repo, novel_id, 1)
        add_chapter(repo, novel_id, 2)
        claim_task(repo, cid(2), ME, TTL)
        before = event_count(repo)

        report = repo.tasks.recover()

        assert (report.requeued, report.claims_cleared, report.merged) == (0, 0, 0)
        assert event_count(repo) == before
        assert row_of(repo, cid(1)).state == "downloading"
        assert live_task(repo, cid(2)).claimed_by == ME


class TestWorkRecover:
    def test_recover__frees_only_consumers(self, repo: Repo, novel_id: str) -> None:
        assert repo.work.recover() == RecoveryReport(consumers_freed=0)


class TestEventsWithAWaitingExport:
    @pytest.fixture
    def waiting_export(self, repo: Repo, novel_id: str) -> str:
        add_chapter(repo, novel_id, 1)
        with db.write(repo.engine) as conn:
            conn.execute(
                insert(exports).values(
                    **export_row(novel_id=novel_id, first_chapter_id=cid(1), last_chapter_id=cid(1))
                )
            )
        return str(export_row()["id"])

    def test_start__appends_an_ex_event_for_the_waiting_export_covering_the_chapter(
        self, repo: Repo, waiting_export: str
    ) -> None:
        start_task(repo, cid(1), ME, TTL)

        assert len(chapter_events(repo, waiting_export)) == 1

    def test_complete__appends_an_ex_event_for_the_waiting_export_covering_the_chapter(
        self, repo: Repo, waiting_export: str
    ) -> None:
        finish(repo, start_task(repo, cid(1), ME, TTL).task, done())

        assert len(chapter_events(repo, waiting_export)) == 2

    def test_recover__appends_an_ex_event_for_the_waiting_export_covering_the_chapter(
        self, repo: Repo, waiting_export: str, clock: FakeClock
    ) -> None:
        start_task(repo, cid(1), ME, TTL)
        clock.advance(seconds=TTL + 1)

        repo.tasks.recover()

        assert len(chapter_events(repo, waiting_export)) == 2
