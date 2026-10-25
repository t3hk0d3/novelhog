"""Task repair: the entity/task pairs of tasks spec section 2 that do not hold (section 8).

Each broken pair is made by writing rows directly. `repo.tasks.repair` scans for every problem
of the section 8 table and fixes each one in its own transaction; the worker runs it at most every
`TASK_REPAIR_INTERVAL` and logs `task_repaired` for each fix.
"""

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import delete, insert, select, update
from structlog.testing import capture_logs

from novelhog.config import Settings
from novelhog.domain.exports import Format
from novelhog.domain.payloads import format_time
from novelhog.domain.tasks import (
    PRIORITY_BUILD,
    PRIORITY_REFRESH,
    PRIORITY_USER,
    TASK_REPAIR_INTERVAL,
    TaskKind,
    TaskState,
)
from novelhog.repo import Repo, db
from novelhog.repo.schema import chapters, event_clock, events, exports, novels, tasks
from novelhog.repo.tasks import Repair, TaskRow
from novelhog.storage import LocalStaticStorage
from novelhog.worker.loop import Worker
from tests.conftest import FakeClock
from tests.integration.helpers import chapter_row, export_row
from tests.integration.task_fakes import live_task, tasks_of
from tests.integration.worker.conftest import URL, FakeAdapter, FakeFetcher, RecordingStop

DOWNLOAD = TaskKind.CHAPTER_DOWNLOAD
BUILD = TaskKind.EXPORT_BUILD
REFRESH = TaskKind.NOVEL_REFRESH


def _chapter(repo: Repo, novel_id: str, at: int, state: str, *, positioned: bool = True) -> str:
    """Insert a chapter row only, without the task its state needs."""
    chapter_id = f"CH{at:012d}"
    row = chapter_row(
        id=chapter_id,
        novel_id=novel_id,
        url=f"{URL}/{at}",
        position=at if positioned else None,
        removed_at=None if positioned else repo.clock(),
        state=state,
        updated_at=repo.clock(),
    )
    with db.write(repo.engine) as conn:
        conn.execute(insert(chapters).values(**row))
    return chapter_id


def _export(repo: Repo, novel_id: str, chapter_id: str, state: str, **values: Any) -> str:
    """Insert an export row of one chapter only, without the task its state needs."""
    export_id = "EX000000000001"
    row = export_row(
        id=export_id,
        novel_id=novel_id,
        first_chapter_id=chapter_id,
        last_chapter_id=chapter_id,
        state=state,
        created_at=repo.clock() - timedelta(minutes=5),
    )
    with db.write(repo.engine) as conn:
        conn.execute(insert(exports).values(**(row | values)))
    return export_id


def _task(  # noqa: PLR0913
    repo: Repo,
    kind: TaskKind,
    entity_id: str,
    novel_id: str,
    state: TaskState,
    *,
    claimed_until: datetime | None = None,
) -> str:
    """Insert a task row directly; a running one is claimed by `other` until `claimed_until`."""
    now = repo.clock()
    task_id = f"TK{len(_all_tasks(repo)) + 1:012d}"
    values: dict[str, Any] = {
        "id": task_id,
        "kind": kind.value,
        "entity_id": entity_id,
        "novel_id": novel_id,
        "state": state.value,
        "priority": PRIORITY_USER,
        "run_after": now,
        "change_seq": 1,
        "created_at": now,
        "updated_at": now,
    }
    if state is TaskState.RUNNING:
        until = claimed_until if claimed_until is not None else now + timedelta(minutes=5)
        values |= {"attempts": 1, "started_at": now, "claimed_by": "other", "claimed_until": until}
    with db.write(repo.engine) as conn:
        conn.execute(insert(tasks).values(**values))
    return task_id


def _all_tasks(repo: Repo) -> list[TaskRow]:
    with db.read(repo.engine) as conn:
        return [TaskRow.from_row(row) for row in conn.execute(select(tasks).order_by(tasks.c.id))]


def _drop_tasks(repo: Repo, entity_id: str) -> None:
    with db.write(repo.engine) as conn:
        conn.execute(delete(tasks).where(tasks.c.entity_id == entity_id))


def _state(repo: Repo, table: Any, entity_id: str) -> str:
    with db.read(repo.engine) as conn:
        return str(conn.execute(select(table.c.state).where(table.c.id == entity_id)).scalar_one())


def _events_of(repo: Repo, entity_id: str) -> list[dict[str, Any]]:
    with db.read(repo.engine) as conn:
        rows = conn.execute(
            select(events.c.payload).where(events.c.entity_id == entity_id).order_by(events.c.seq)
        )
        return [dict(payload) for payload in rows.scalars()]


def _clock_value(repo: Repo) -> int:
    with db.read(repo.engine) as conn:
        return int(conn.execute(select(event_clock.c.seq)).scalar_one())


def _refreshed(repo: Repo, novel_id: str, at: datetime, interval: int) -> None:
    with db.write(repo.engine) as conn:
        conn.execute(
            update(novels)
            .where(novels.c.id == novel_id)
            .values(last_refreshed_at=at, refresh_interval=interval)
        )


class TestRepair__ChapterPendingWithoutTask:
    def test_repair__enqueues_a_download_at_user_priority(
        self, repo: Repo, novel_id: str, clock: FakeClock
    ) -> None:
        chapter_id = _chapter(repo, novel_id, 3, "pending")

        repo.tasks.repair()

        task = live_task(repo, chapter_id)
        assert (task.kind, task.priority, task.ordinal) == (DOWNLOAD, PRIORITY_USER, 3)
        assert (task.run_after, task.emit_event) == (clock(), False)

    def test_repair__reports_chapter_pending_without_task(self, repo: Repo, novel_id: str) -> None:
        chapter_id = _chapter(repo, novel_id, 3, "pending")

        repairs = repo.tasks.repair()

        task = live_task(repo, chapter_id)
        assert repairs == [Repair("chapter_pending_without_task", task.id, chapter_id)]

    def test_repair__appends_the_chapter_event_with_its_queued_at(
        self, repo: Repo, novel_id: str, clock: FakeClock
    ) -> None:
        chapter_id = _chapter(repo, novel_id, 3, "pending")

        repo.tasks.repair()

        [event] = _events_of(repo, chapter_id)
        assert (event["state"], event["queued_at"]) == ("pending", format_time(clock()))


class TestRepair__ChapterPendingWithoutPosition:
    def test_repair__moves_the_chapter_to_not_requested(self, repo: Repo, novel_id: str) -> None:
        chapter_id = _chapter(repo, novel_id, 3, "pending", positioned=False)

        repo.tasks.repair()

        assert _state(repo, chapters, chapter_id) == "not_requested"

    def test_repair__cancels_its_queued_task(self, repo: Repo, novel_id: str) -> None:
        chapter_id = _chapter(repo, novel_id, 3, "pending", positioned=False)
        task_id = _task(repo, DOWNLOAD, chapter_id, novel_id, TaskState.QUEUED)

        repairs = repo.tasks.repair()

        assert [task.state for task in tasks_of(repo, chapter_id)] == [TaskState.CANCELLED]
        assert repairs == [Repair("chapter_pending_without_position", task_id, chapter_id)]

    def test_repair__reports_no_task_when_none_was_queued(self, repo: Repo, novel_id: str) -> None:
        chapter_id = _chapter(repo, novel_id, 3, "pending", positioned=False)

        assert repo.tasks.repair() == [Repair("chapter_pending_without_position", None, chapter_id)]

    def test_repair__appends_the_chapter_event(self, repo: Repo, novel_id: str) -> None:
        chapter_id = _chapter(repo, novel_id, 3, "pending", positioned=False)

        repo.tasks.repair()

        [event] = _events_of(repo, chapter_id)
        assert (event["state"], event["position"], event["queued_at"]) == (
            "not_requested", None, None,
        )  # fmt: skip


class TestRepair__ChapterDownloadingWithoutTask:
    def test_repair__puts_a_positioned_chapter_back_to_pending_and_enqueues_it(
        self, repo: Repo, novel_id: str
    ) -> None:
        chapter_id = _chapter(repo, novel_id, 3, "downloading")

        repairs = repo.tasks.repair()

        task = live_task(repo, chapter_id)
        assert _state(repo, chapters, chapter_id) == "pending"
        assert (task.priority, task.ordinal) == (PRIORITY_USER, 3)
        assert repairs == [Repair("chapter_downloading_without_task", task.id, chapter_id)]

    def test_repair__moves_an_unpositioned_chapter_to_not_requested(
        self, repo: Repo, novel_id: str
    ) -> None:
        chapter_id = _chapter(repo, novel_id, 3, "downloading", positioned=False)

        repairs = repo.tasks.repair()

        assert _state(repo, chapters, chapter_id) == "not_requested"
        assert tasks_of(repo, chapter_id) == []
        assert repairs == [Repair("chapter_downloading_without_task", None, chapter_id)]

    def test_repair__appends_the_chapter_event(self, repo: Repo, novel_id: str) -> None:
        chapter_id = _chapter(repo, novel_id, 3, "downloading")

        repo.tasks.repair()

        assert [event["state"] for event in _events_of(repo, chapter_id)] == ["pending"]


class TestRepair__ExportBuildableWithoutTask:
    def test_repair__enqueues_the_build_with_emit_event(self, repo: Repo, novel_id: str) -> None:
        chapter_id = _chapter(repo, novel_id, 1, "downloaded")
        export_id = _export(repo, novel_id, chapter_id, "waiting")

        repairs = repo.tasks.repair()

        task = live_task(repo, export_id)
        assert (task.kind, task.priority, task.emit_event) == (BUILD, PRIORITY_BUILD, True)
        assert task.run_after == repo.clock() - timedelta(minutes=5)  # the export's created_at
        assert repairs == [Repair("export_buildable_without_task", task.id, export_id)]

    def test_repair__appends_the_build_task_event(self, repo: Repo, novel_id: str) -> None:
        chapter_id = _chapter(repo, novel_id, 1, "downloaded")
        export_id = _export(repo, novel_id, chapter_id, "waiting")

        repo.tasks.repair()

        task = live_task(repo, export_id)
        [event] = _events_of(repo, task.id)
        assert (event["id"], event["state"]) == (task.id, "queued")

    def test_repair__leaves_a_waiting_export_that_is_not_buildable(
        self, repo: Repo, novel_id: str
    ) -> None:
        chapter_id = _chapter(repo, novel_id, 1, "not_requested")
        export_id = _export(repo, novel_id, chapter_id, "waiting")

        assert repo.tasks.repair() == []
        assert tasks_of(repo, export_id) == []


class TestRepair__ExportBuildingWithoutTask:
    def test_repair__puts_the_export_back_to_waiting_and_enqueues_its_build(
        self, repo: Repo, novel_id: str
    ) -> None:
        chapter_id = _chapter(repo, novel_id, 1, "downloaded")
        export_id = _export(repo, novel_id, chapter_id, "building")

        repairs = repo.tasks.repair()

        task = live_task(repo, export_id)
        assert _state(repo, exports, export_id) == "waiting"
        assert repairs == [Repair("export_building_without_task", task.id, export_id)]

    def test_repair__enqueues_no_build_when_the_export_is_not_buildable(
        self, repo: Repo, novel_id: str
    ) -> None:
        chapter_id = _chapter(repo, novel_id, 1, "not_requested")
        export_id = _export(repo, novel_id, chapter_id, "building")

        repairs = repo.tasks.repair()

        assert _state(repo, exports, export_id) == "waiting"
        assert tasks_of(repo, export_id) == []
        assert repairs == [Repair("export_building_without_task", None, export_id)]

    def test_repair__appends_the_export_event(self, repo: Repo, novel_id: str) -> None:
        chapter_id = _chapter(repo, novel_id, 1, "not_requested")
        export_id = _export(repo, novel_id, chapter_id, "building")

        repo.tasks.repair()

        assert [event["state"] for event in _events_of(repo, export_id)] == ["waiting"]


class TestRepair__NovelWithoutRefresh:
    def test_repair__enqueues_a_refreshed_novel_at_its_last_refresh_plus_interval(
        self, repo: Repo, novel_id: str, clock: FakeClock
    ) -> None:
        last = clock() - timedelta(hours=1)
        _refreshed(repo, novel_id, last, 7200)
        _drop_tasks(repo, novel_id)

        repairs = repo.tasks.repair()

        task = live_task(repo, novel_id)
        assert (task.kind, task.priority) == (REFRESH, PRIORITY_REFRESH)
        assert (task.run_after, task.emit_event) == (last + timedelta(seconds=7200), False)
        assert repairs == [Repair("novel_without_refresh", task.id, novel_id)]

    def test_repair__enqueues_a_never_refreshed_novel_now_with_emit_event(
        self, repo: Repo, novel_id: str, clock: FakeClock
    ) -> None:
        _drop_tasks(repo, novel_id)

        repo.tasks.repair()

        task = live_task(repo, novel_id)
        assert (task.run_after, task.emit_event) == (clock(), True)

    def test_repair__appends_the_novel_event_with_its_next_refresh_at(
        self, repo: Repo, novel_id: str, clock: FakeClock
    ) -> None:
        last = clock() - timedelta(hours=1)
        _refreshed(repo, novel_id, last, 7200)
        _drop_tasks(repo, novel_id)
        before = len(_events_of(repo, novel_id))

        repo.tasks.repair()

        novel_events = _events_of(repo, novel_id)
        assert len(novel_events) == before + 1
        expected = format_time(last + timedelta(seconds=7200))
        assert novel_events[-1]["next_refresh_at"] == expected


class TestRepair__QueuedTaskOfAFinishedOrGoneEntity:
    @pytest.mark.parametrize("state", ["not_requested", "downloaded", "error"])
    def test_repair__cancels_the_download_of_a_finished_chapter(
        self, repo: Repo, novel_id: str, state: str
    ) -> None:
        chapter_id = _chapter(repo, novel_id, 1, state)
        task_id = _task(repo, DOWNLOAD, chapter_id, novel_id, TaskState.QUEUED)

        repairs = repo.tasks.repair()

        assert [task.state for task in tasks_of(repo, chapter_id)] == [TaskState.CANCELLED]
        assert repairs == [Repair("queued_task_not_needed", task_id, chapter_id)]

    def test_repair__appends_the_event_of_the_finished_chapter(
        self, repo: Repo, novel_id: str
    ) -> None:
        chapter_id = _chapter(repo, novel_id, 1, "downloaded")
        _task(repo, DOWNLOAD, chapter_id, novel_id, TaskState.QUEUED)

        repo.tasks.repair()

        [event] = _events_of(repo, chapter_id)
        assert (event["state"], event["queued_at"]) == ("downloaded", None)

    def test_repair__cancels_the_download_of_a_gone_chapter(
        self, repo: Repo, novel_id: str
    ) -> None:
        task_id = _task(repo, DOWNLOAD, "CH000000000099", novel_id, TaskState.QUEUED)

        repairs = repo.tasks.repair()

        assert [task.state for task in tasks_of(repo, "CH000000000099")] == [TaskState.CANCELLED]
        assert repairs == [Repair("queued_task_not_needed", task_id, "CH000000000099")]

    @pytest.mark.parametrize(
        ("state", "values"),
        [
            ("ready", {"storage_key": "exports/a.epub", "size_bytes": 1}),
            ("failed", {"error": "boom"}),
        ],
    )
    def test_repair__cancels_the_build_of_a_finished_export(
        self, repo: Repo, novel_id: str, state: str, values: dict[str, Any]
    ) -> None:
        chapter_id = _chapter(repo, novel_id, 1, "downloaded")
        export_id = _export(repo, novel_id, chapter_id, state, **values)
        task_id = _task(repo, BUILD, export_id, novel_id, TaskState.QUEUED)

        repairs = repo.tasks.repair()

        assert [task.state for task in tasks_of(repo, export_id)] == [TaskState.CANCELLED]
        assert repairs == [Repair("queued_task_not_needed", task_id, export_id)]

    def test_repair__cancels_the_build_of_a_gone_export(self, repo: Repo, novel_id: str) -> None:
        task_id = _task(repo, BUILD, "EX000000000099", novel_id, TaskState.QUEUED)

        repairs = repo.tasks.repair()

        assert [task.state for task in tasks_of(repo, "EX000000000099")] == [TaskState.CANCELLED]
        assert repairs == [Repair("queued_task_not_needed", task_id, "EX000000000099")]


class TestRepair__WhenTheConditionNoLongerHolds:
    def test_repair__a_fix_whose_condition_was_fixed_meanwhile_does_nothing(
        self, repo: Repo, novel_id: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        chapter_id = _chapter(repo, novel_id, 3, "pending")
        handler = repo.tasks._handlers[DOWNLOAD]  # noqa: SLF001
        scan = handler.problems

        def scan_then_dequeue(conn: Any) -> Any:
            found = scan(conn)
            with db.write(repo.engine) as other:  # the user dequeues it before the fix
                other.execute(
                    update(chapters)
                    .where(chapters.c.id == chapter_id)
                    .values(state="not_requested")
                )
            return found

        monkeypatch.setattr(handler, "problems", scan_then_dequeue)
        before = _clock_value(repo)

        assert repo.tasks.repair() == []
        assert tasks_of(repo, chapter_id) == []
        assert _events_of(repo, chapter_id) == []
        assert _clock_value(repo) == before


class TestRepair__RunningTasks:
    def test_repair__does_not_touch_a_syncing_novel(self, repo: Repo, novel_id: str) -> None:
        _drop_tasks(repo, novel_id)
        task_id = _task(repo, REFRESH, novel_id, novel_id, TaskState.RUNNING)

        assert repo.tasks.repair() == []
        assert [task.id for task in tasks_of(repo, novel_id)] == [task_id]

    def test_repair__does_not_reset_a_building_export_with_a_valid_claim(
        self, repo: Repo, novel_id: str
    ) -> None:
        chapter_id = _chapter(repo, novel_id, 1, "downloaded")
        export_id = _export(repo, novel_id, chapter_id, "building")
        _task(repo, BUILD, export_id, novel_id, TaskState.RUNNING)

        assert repo.tasks.repair() == []
        assert _state(repo, exports, export_id) == "building"

    @pytest.mark.parametrize("entity", ["downloaded", "gone"])
    def test_repair__never_cancels_a_running_task_of_a_finished_or_gone_entity(
        self, repo: Repo, novel_id: str, entity: str
    ) -> None:
        chapter_id = (
            _chapter(repo, novel_id, 1, "downloaded")
            if entity == "downloaded"
            else "CH000000000099"
        )
        task_id = _task(repo, DOWNLOAD, chapter_id, novel_id, TaskState.RUNNING)

        assert repo.tasks.repair() == []
        assert [(task.id, task.state) for task in tasks_of(repo, chapter_id)] == [
            (task_id, TaskState.RUNNING)
        ]

    def test_repair__does_not_touch_a_chapter_removed_mid_download(
        self, repo: Repo, novel_id: str
    ) -> None:
        chapter_id = _chapter(repo, novel_id, 1, "downloading", positioned=False)
        _task(repo, DOWNLOAD, chapter_id, novel_id, TaskState.RUNNING)

        assert repo.tasks.repair() == []
        assert _state(repo, chapters, chapter_id) == "downloading"


class TestRepair__OnAConsistentDatabase:
    def test_repair__produces_no_repairs_and_writes_nothing(
        self, repo: Repo, novel_id: str
    ) -> None:
        _chapter(repo, novel_id, 1, "downloaded")
        _chapter(repo, novel_id, 2, "not_requested")
        repo.exports.create(novel_id, 1, 2, Format.EPUB)  # queues chapter 2; the export waits
        repo.exports.create(novel_id, 1, 1, Format.EPUB)  # buildable: its build is queued
        before = (_clock_value(repo), _all_tasks(repo))

        assert repo.tasks.repair() == []
        assert (_clock_value(repo), _all_tasks(repo)) == before


class _Ticks:
    """Counts the calls of a patched method and returns `result` (or raises it)."""

    def __init__(self, result: object) -> None:
        self.calls = 0
        self.result = result

    def __call__(self) -> object:
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


@pytest.fixture
def worker(
    repo: Repo,
    settings: Settings,
    tmp_path: Path,
    clock: FakeClock,
) -> Worker:
    storage = LocalStaticStorage(tmp_path / "static", "/static")
    return Worker(
        repo, FakeAdapter(), FakeFetcher(), storage, {}, settings, stop=RecordingStop(), clock=clock
    )


class TestStep__Repair:
    def test_step__repairs_at_most_every_interval_while_the_runner_has_work_every_step(
        self, repo: Repo, worker: Worker, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repair = _Ticks([])
        monkeypatch.setattr(repo.tasks, "repair", repair)
        monkeypatch.setattr(worker._runner, "step", lambda: True)  # noqa: SLF001

        assert worker.step() == "task"
        assert repair.calls == 1
        clock.advance(seconds=TASK_REPAIR_INTERVAL.total_seconds() - 1)
        assert worker.step() == "task"
        assert repair.calls == 1
        clock.advance(seconds=1)
        assert worker.step() == "task"
        assert repair.calls == 2

    def test_step__logs_task_repaired_with_the_problem_for_each_fix(
        self, repo: Repo, novel_id: str, worker: Worker
    ) -> None:
        chapter_id = _chapter(repo, novel_id, 3, "pending")
        export_id = _export(repo, novel_id, chapter_id, "building")

        with capture_logs() as logs:
            worker.step()

        repaired = [entry for entry in logs if entry["event"] == "task_repaired"]
        assert [(entry["problem"], entry["entity_id"], entry["kind"]) for entry in repaired] == [
            ("chapter_pending_without_task", chapter_id, DOWNLOAD.value),
            ("export_building_without_task", export_id, BUILD.value),
        ]
        task = next(t for t in tasks_of(repo, chapter_id) if t.kind is DOWNLOAD)
        assert (repaired[0]["task_id"], repaired[0]["attempts"]) == (task.id, task.attempts)

    def test_step__logs_a_repair_failure_and_continues_the_step(
        self, repo: Repo, worker: Worker, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(repo.tasks, "repair", _Ticks(RuntimeError("boom")))
        ran: list[bool] = []

        def runner_step() -> bool:
            ran.append(True)
            return False

        monkeypatch.setattr(worker._runner, "step", runner_step)  # noqa: SLF001

        with capture_logs() as logs:
            assert worker.step() == "idle"

        assert "task_repair_failed" in [entry["event"] for entry in logs]
        assert ran == [True]
