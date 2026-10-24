"""The exports repository: create, read, delete, and builds as tasks (start, finish, recovery)."""

import threading
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import insert, select, update

from novelhog.domain.chapters import ChapterState
from novelhog.domain.errors import Conflict, NotFound, ValidationFailed
from novelhog.domain.exports import ExportState, Format
from novelhog.domain.tasks import Done, Failed, Outcome, TaskState
from novelhog.repo import Repo, db
from novelhog.repo.schema import chapters, events, exports, novels
from novelhog.repo.task_kinds.export_build import BuildResult
from novelhog.repo.tasks import Finished, Skipped
from tests.conftest import FakeClock
from tests.integration.helpers import chapter_row, trash_keys
from tests.integration.task_fakes import force_claim, live_task, start_build, tasks_of
from tests.integration.worker.conftest import add_chapter

URL = "https://example.com/novel"
TTL = 300
ME = "worker-a"
OTHER = "worker-b"
EPUB = Format.EPUB


def cid(position: int) -> str:
    return f"CH{position:012d}"


@pytest.fixture
def novel_id(repo: Repo) -> str:
    repo.hosts.publish(["example.com"])
    novel = repo.novels.create(URL, followed=False).id
    with db.write(repo.engine) as conn:
        conn.execute(update(novels).where(novels.c.id == novel).values(title="My Novel"))
    return novel


def add_chapters(repo: Repo, novel_id: str, states: list[str]) -> None:
    """Insert chapters at positions 1..n with the given states and their download tasks."""
    claim = {"claimed_by": "w", "claimed_until": repo.clock() + timedelta(days=1)}
    for position, state in enumerate(states, start=1):
        add_chapter(
            repo, novel_id, position, state=state, **(claim if state == "downloading" else {})
        )


def set_state(repo: Repo, at: int, **values: Any) -> None:
    with db.write(repo.engine) as conn:
        conn.execute(update(chapters).where(chapters.c.id == cid(at)).values(**values))


def remove(repo: Repo, at: int, **values: Any) -> None:
    """Remove a chapter upstream: no position, `removed_at` set."""
    set_state(repo, at, position=None, removed_at=repo.clock(), **values)


def state_of(repo: Repo, position: int) -> str:
    with db.read(repo.engine) as conn:
        return str(
            conn.execute(
                select(chapters.c.state).where(chapters.c.id == cid(position))
            ).scalar_one()
        )


def ex_events(repo: Repo, export_id: str) -> list[dict[str, Any]]:
    with db.read(repo.engine) as conn:
        rows = conn.execute(
            select(events.c.payload).where(events.c.entity_id == export_id).order_by(events.c.seq)
        ).scalars()
        return [dict(r) for r in rows]


def export_row(repo: Repo, export_id: str) -> Any:
    with db.read(repo.engine) as conn:
        return conn.execute(select(exports).where(exports.c.id == export_id)).one()


def build_claim(repo: Repo, novel_id: str) -> str:
    """Create an all-downloaded export, start its build as `ME` and return its id."""
    add_chapters(repo, novel_id, ["downloaded", "downloaded"])
    view, _ = repo.exports.create(novel_id, 1, 2, EPUB)
    start_build(repo, view.id, ME, TTL)
    return view.id


def finish_build(
    repo: Repo, export_id: str, outcome: Outcome[BuildResult], worker: str = ME
) -> Finished | None:
    """Finish the export's running build as `worker` with `outcome`."""
    return repo.tasks.finish(live_task(repo, export_id, TaskState.RUNNING), worker, outcome)


def ready(key: str, size: int = 1) -> Done[BuildResult]:
    return Done(BuildResult(key, size))


class TestCreate__WithEveryChapterDownloaded:
    def test_create__stores_end_chapters_and_inserts_waiting_with_filename(
        self, repo: Repo, novel_id: str
    ) -> None:
        add_chapters(repo, novel_id, ["downloaded"] * 3)
        view, created = repo.exports.create(novel_id, 2, 3, EPUB)
        assert created
        row = export_row(repo, view.id)
        assert (row.first_chapter_id, row.last_chapter_id) == (cid(2), cid(3))
        assert row.state == "waiting"
        assert row.waited is False
        assert row.filename == "My Novel Chapters 2-3.epub"
        assert row.mime == "application/epub+zip"
        assert view.state is ExportState.WAITING

    def test_create__queues_nothing(self, repo: Repo, novel_id: str) -> None:
        add_chapters(repo, novel_id, ["downloaded"] * 3)
        repo.exports.create(novel_id, 1, 3, EPUB)
        assert [state_of(repo, p) for p in (1, 2, 3)] == ["downloaded"] * 3

    def test_create__appends_exactly_one_ex_event(self, repo: Repo, novel_id: str) -> None:
        add_chapters(repo, novel_id, ["downloaded"] * 3)
        view, _ = repo.exports.create(novel_id, 1, 3, EPUB)
        (payload,) = ex_events(repo, view.id)
        assert payload["id"] == view.id
        assert payload["state"] == "waiting"


class TestCreate__WithMissingChapters:
    def test_create__moves_not_requested_and_error_chapters_to_pending(
        self, repo: Repo, novel_id: str
    ) -> None:
        add_chapters(repo, novel_id, ["downloaded", "not_requested", "error", "downloading"])
        repo.exports.create(novel_id, 1, 4, EPUB)
        assert [state_of(repo, p) for p in (1, 2, 3, 4)] == [
            "downloaded",
            "pending",
            "pending",
            "downloading",
        ]
        assert repo.chapters.get(cid(2)).queued_at == repo.clock()

    def test_create__inserts_waiting_with_waited_set(self, repo: Repo, novel_id: str) -> None:
        add_chapters(repo, novel_id, ["downloaded", "not_requested"])
        view, _ = repo.exports.create(novel_id, 1, 2, EPUB)
        assert view.waited is True
        assert export_row(repo, view.id).waited is True

    def test_create__waited_when_range_is_only_pending(self, repo: Repo, novel_id: str) -> None:
        add_chapters(repo, novel_id, ["pending", "pending"])
        view, _ = repo.exports.create(novel_id, 1, 2, EPUB)
        assert view.waited is True

    def test_create__emits_chapter_events_and_one_ex_event_for_the_new_export(
        self, repo: Repo, novel_id: str
    ) -> None:
        add_chapters(repo, novel_id, ["not_requested", "error"])
        before = repo.events.latest_seq()
        view, _ = repo.exports.create(novel_id, 1, 2, EPUB)
        with db.read(repo.engine) as conn:
            rows = conn.execute(
                select(events.c.entity_id).where(events.c.seq > before).order_by(events.c.seq)
            ).scalars()
            entities = list(rows)
        assert entities == [cid(1), cid(2), novel_id, view.id]
        assert len(ex_events(repo, view.id)) == 1


class TestCreate__WithSkipMissing:
    def test_create__queues_nothing_and_inserts_waiting_with_waited_unset(
        self, repo: Repo, novel_id: str
    ) -> None:
        add_chapters(repo, novel_id, ["downloaded", "not_requested", "error"])
        view, created = repo.exports.create(novel_id, 1, 3, EPUB, skip_missing=True)
        assert created
        assert view.waited is False
        assert view.skip_missing is True
        assert [state_of(repo, p) for p in (1, 2, 3)] == ["downloaded", "not_requested", "error"]
        assert len(ex_events(repo, view.id)) == 1

    def test_create__raises_nothing_to_export_when_nothing_is_downloaded(
        self, repo: Repo, novel_id: str
    ) -> None:
        add_chapters(repo, novel_id, ["not_requested", "pending"])
        with pytest.raises(ValidationFailed) as error:
            repo.exports.create(novel_id, 1, 2, EPUB, skip_missing=True)
        assert error.value.code == "nothing_to_export"
        with db.read(repo.engine) as conn:
            assert conn.execute(select(exports.c.id)).first() is None


class TestCreate__WithAnIdenticalActiveExport:
    @pytest.mark.parametrize("state", ["waiting", "building"])
    def test_create__returns_it_with_created_false_and_inserts_nothing(
        self, repo: Repo, novel_id: str, state: str
    ) -> None:
        add_chapters(repo, novel_id, ["downloaded", "downloaded"])
        first, _ = repo.exports.create(novel_id, 1, 2, EPUB)
        if state == "building":
            start_build(repo, first.id, ME, TTL)
        seq = repo.events.latest_seq()
        again, created = repo.exports.create(novel_id, 1, 2, EPUB)
        assert not created
        assert again.id == first.id
        assert repo.events.latest_seq() == seq

    def test_create__skip_missing_makes_a_different_export(self, repo: Repo, novel_id: str) -> None:
        add_chapters(repo, novel_id, ["downloaded", "downloaded"])
        first, _ = repo.exports.create(novel_id, 1, 2, EPUB)
        other, created = repo.exports.create(novel_id, 1, 2, EPUB, skip_missing=True)
        assert created
        assert other.id != first.id

    def test_create__a_finished_export_does_not_block_a_new_one(
        self, repo: Repo, novel_id: str
    ) -> None:
        export_id = build_claim(repo, novel_id)
        assert finish_build(repo, export_id, Failed("boom")) is not None
        again, created = repo.exports.create(novel_id, 1, 2, EPUB)
        assert created
        assert again.id != export_id


class TestCreate__WithTwoIdenticalCreatesRacing:
    def test_create__one_creates_and_the_other_returns_the_same_export(
        self, repo: Repo, novel_id: str
    ) -> None:
        add_chapters(repo, novel_id, ["downloaded", "downloaded"])
        barrier = threading.Barrier(2)
        results: list[tuple[str, bool]] = []
        errors: list[BaseException] = []

        def racer() -> None:
            try:
                barrier.wait()
                view, created = repo.exports.create(novel_id, 1, 2, EPUB)
                results.append((view.id, created))
            except BaseException as error:  # noqa: BLE001  (checked in the main thread)
                errors.append(error)

        threads = [threading.Thread(target=racer, daemon=True) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        assert not errors
        assert len(results) == 2
        assert results[0][0] == results[1][0]
        assert sorted(created for _, created in results) == [False, True]


class TestCreate__WithBadInput:
    @pytest.mark.parametrize(("start", "end"), [(0, 2), (1, 4), (3, 2), (-1, 1)])
    def test_create__raises_invalid_selector(
        self, repo: Repo, novel_id: str, start: int, end: int
    ) -> None:
        add_chapters(repo, novel_id, ["downloaded"] * 3)
        with pytest.raises(ValidationFailed) as error:
            repo.exports.create(novel_id, start, end, EPUB)
        assert error.value.code == "invalid_selector"

    def test_create__raises_invalid_selector_for_a_novel_without_chapters(
        self, repo: Repo, novel_id: str
    ) -> None:
        with pytest.raises(ValidationFailed) as error:
            repo.exports.create(novel_id, 1, 1, EPUB)
        assert error.value.code == "invalid_selector"

    @pytest.mark.parametrize("bad", ["NV000000000099", "nonsense"])
    def test_create__raises_not_found_on_an_unknown_novel(self, repo: Repo, bad: str) -> None:
        with pytest.raises(NotFound):
            repo.exports.create(bad, 1, 1, EPUB)


class TestReads:
    def test_get__chapter_from_and_to_are_the_current_positions_of_the_end_chapters(
        self, repo: Repo, novel_id: str
    ) -> None:
        add_chapters(repo, novel_id, ["downloaded"] * 4)
        view, _ = repo.exports.create(novel_id, 2, 4, EPUB)
        assert (view.chapter_from, view.chapter_to) == (2, 4)
        remove(repo, 1)
        for at in (2, 3, 4):
            set_state(repo, at, position=at - 1)
        moved = repo.exports.get(novel_id, view.id)
        assert (moved.chapter_from, moved.chapter_to) == (1, 3)

    def test_get__chapter_from_and_to_are_null_when_removed(
        self, repo: Repo, novel_id: str
    ) -> None:
        add_chapters(repo, novel_id, ["downloaded"] * 3)
        view, _ = repo.exports.create(novel_id, 1, 3, EPUB)
        remove(repo, 3)
        gone = repo.exports.get(novel_id, view.id)
        assert gone.chapter_to is None
        assert gone.chapter_from == 1

    def test_get__progress_counts_downloaded_chapters_of_the_range(
        self, repo: Repo, novel_id: str
    ) -> None:
        add_chapters(repo, novel_id, ["downloaded", "pending", "downloaded", "downloaded"])
        view, _ = repo.exports.create(novel_id, 1, 3, EPUB)
        assert (view.progress.downloaded, view.progress.total) == (2, 3)

    def test_get__blocked_lists_error_and_not_requested_positions_while_waiting(
        self, repo: Repo, novel_id: str
    ) -> None:
        add_chapters(repo, novel_id, ["downloaded", "error", "not_requested", "pending"])
        view, _ = repo.exports.create(novel_id, 1, 4, EPUB)
        set_state(repo, 2, state="error")
        set_state(repo, 3, state="not_requested")
        blocked = repo.exports.get(novel_id, view.id).blocked
        assert blocked.errors == (2,)
        assert blocked.not_queued == (3,)

    def test_get__blocked_is_empty_with_skip_missing(self, repo: Repo, novel_id: str) -> None:
        add_chapters(repo, novel_id, ["downloaded", "error", "not_requested"])
        view, _ = repo.exports.create(novel_id, 1, 3, EPUB, skip_missing=True)
        assert view.blocked.errors == ()
        assert view.blocked.not_queued == ()

    def test_get__a_chapter_inserted_upstream_inside_the_range_is_counted(
        self, repo: Repo, novel_id: str
    ) -> None:
        add_chapters(repo, novel_id, ["downloaded", "downloaded", "downloaded"])
        view, _ = repo.exports.create(novel_id, 1, 3, EPUB)
        set_state(repo, 3, position=4)
        with db.write(repo.engine) as conn:
            conn.execute(
                insert(chapters).values(
                    **chapter_row(
                        id=cid(9), novel_id=novel_id, url=f"{URL}/9", position=3, state="pending"
                    )
                )
            )
        grown = repo.exports.get(novel_id, view.id)
        assert (grown.progress.downloaded, grown.progress.total) == (3, 4)
        assert grown.chapter_to == 4

    def test_get__url_is_set_only_when_ready(self, repo: Repo, novel_id: str) -> None:
        export_id = build_claim(repo, novel_id)
        assert repo.exports.get(novel_id, export_id).url is None
        key = f"exports/{novel_id}/{export_id}/My Novel Chapters 1-2.epub"
        assert finish_build(repo, export_id, ready(key, 10)) is not None
        view = repo.exports.get(novel_id, export_id)
        assert (
            view.url == f"/static/exports/{novel_id}/{export_id}/My%20Novel%20Chapters%201-2.epub"
        )
        assert view.size_bytes == 10

    def test_get__unknown_or_foreign_export_raises_not_found(
        self, repo: Repo, novel_id: str
    ) -> None:
        add_chapters(repo, novel_id, ["downloaded"])
        view, _ = repo.exports.create(novel_id, 1, 1, EPUB)
        with pytest.raises(NotFound):
            repo.exports.get(novel_id, "EX000000000099")
        with pytest.raises(NotFound):
            repo.exports.get("NV000000000099", view.id)
        with pytest.raises(NotFound):
            repo.exports.get(novel_id, "garbage")

    def test_list__returns_newest_first(self, repo: Repo, novel_id: str, clock: FakeClock) -> None:
        add_chapters(repo, novel_id, ["downloaded"] * 3)
        a, _ = repo.exports.create(novel_id, 1, 1, EPUB)
        clock.advance(seconds=1)
        b, _ = repo.exports.create(novel_id, 1, 2, EPUB)
        assert [v.id for v in repo.exports.list(novel_id)] == [b.id, a.id]

    def test_list__raises_not_found_on_an_unknown_novel(self, repo: Repo) -> None:
        with pytest.raises(NotFound):
            repo.exports.list("NV000000000099")


class TestDelete:
    @pytest.mark.parametrize("state", ["waiting", "failed"])
    def test_delete__removes_the_row_keeps_queued_chapters_and_appends_ex_deleted(
        self, repo: Repo, novel_id: str, state: str
    ) -> None:
        add_chapters(repo, novel_id, ["not_requested", "downloaded"])
        view, _ = repo.exports.create(novel_id, 1, 2, EPUB)
        if state == "failed":
            set_failed(repo, view.id)
        repo.exports.delete(novel_id, view.id)
        with pytest.raises(NotFound):
            repo.exports.get(novel_id, view.id)
        assert state_of(repo, 1) == "pending"
        assert ex_events(repo, view.id)[-1] == {
            "id": view.id,
            "novel_id": novel_id,
            "deleted": True,
        }
        assert trash_keys(repo.engine) == []

    def test_delete__ready_export_adds_its_key_to_the_trash(
        self, repo: Repo, novel_id: str
    ) -> None:
        export_id = build_claim(repo, novel_id)
        assert finish_build(repo, export_id, ready("exports/k.epub")) is not None
        repo.exports.delete(novel_id, export_id)
        assert trash_keys(repo.engine) == ["exports/k.epub"]

    def test_delete__building_export_raises_conflict(self, repo: Repo, novel_id: str) -> None:
        export_id = build_claim(repo, novel_id)
        with pytest.raises(Conflict) as error:
            repo.exports.delete(novel_id, export_id)
        assert error.value.code == "export_building"
        assert export_row(repo, export_id).state == "building"

    def test_delete__unknown_foreign_or_malformed_export_raises_not_found(
        self, repo: Repo, novel_id: str
    ) -> None:
        add_chapters(repo, novel_id, ["downloaded"])
        view, _ = repo.exports.create(novel_id, 1, 1, EPUB)
        with pytest.raises(NotFound):
            repo.exports.delete(novel_id, "EX000000000099")
        with pytest.raises(NotFound):
            repo.exports.delete("NV000000000099", view.id)
        with pytest.raises(NotFound):
            repo.exports.delete(novel_id, "x")


def set_failed(repo: Repo, export_id: str) -> None:
    with db.write(repo.engine) as conn:
        conn.execute(
            update(exports).where(exports.c.id == export_id).values(state="failed", error="x")
        )


class TestStartBuild:
    def test_start__runs_the_oldest_buildable_export_first_and_sets_building(
        self, repo: Repo, novel_id: str, clock: FakeClock
    ) -> None:
        add_chapters(repo, novel_id, ["downloaded", "downloaded", "downloaded"])
        older, _ = repo.exports.create(novel_id, 1, 2, EPUB)
        clock.advance(seconds=1)
        repo.exports.create(novel_id, 2, 3, EPUB)
        repo.tasks.claim(OTHER, TTL)  # the novel's refresh, due first

        claimed = repo.tasks.claim(ME, TTL)
        assert claimed is not None
        assert repo.tasks.start(claimed, ME) is not None

        assert claimed.entity_id == older.id
        assert export_row(repo, older.id).state == "building"

    def test_start__returns_the_claim_with_chapters_in_order(
        self, repo: Repo, novel_id: str
    ) -> None:
        add_chapters(repo, novel_id, ["downloaded", "downloaded", "downloaded", "downloaded"])
        with db.write(repo.engine) as conn:
            conn.execute(
                update(novels).where(novels.c.id == novel_id).values(language="en", cover_key="c")
            )
        view, _ = repo.exports.create(novel_id, 2, 4, EPUB)
        claim = start_build(repo, view.id, ME).work
        assert claim.novel_id == novel_id
        assert claim.fmt is EPUB
        assert claim.filename == view.filename
        assert (claim.novel_title, claim.language, claim.cover_key) == ("My Novel", "en", "c")
        assert claim.chapter_ids == [cid(2), cid(3), cid(4)]

    def test_start__appends_an_ex_event_with_building(self, repo: Repo, novel_id: str) -> None:
        export_id = build_claim(repo, novel_id)
        assert ex_events(repo, export_id)[-1]["state"] == "building"
        assert len(ex_events(repo, export_id)) == 2

    def test_create__queues_no_build_for_an_export_with_a_chapter_not_downloaded(
        self, repo: Repo, novel_id: str
    ) -> None:
        add_chapters(repo, novel_id, ["downloaded", "pending", "downloaded"])
        view, _ = repo.exports.create(novel_id, 1, 3, EPUB)
        assert tasks_of(repo, view.id) == []

    def test_start__takes_a_skip_missing_export_with_chapters_missing(
        self, repo: Repo, novel_id: str
    ) -> None:
        add_chapters(repo, novel_id, ["downloaded", "error", "downloaded"])
        view, _ = repo.exports.create(novel_id, 1, 3, EPUB, skip_missing=True)
        assert start_build(repo, view.id, ME).work.chapter_ids == [cid(1), cid(3)]

    def test_start__ignores_removed_chapters_inside_the_range(
        self, repo: Repo, novel_id: str
    ) -> None:
        add_chapters(repo, novel_id, ["downloaded", "downloaded", "downloaded"])
        view, _ = repo.exports.create(novel_id, 1, 3, EPUB)
        remove(repo, 2, state="not_requested")
        assert start_build(repo, view.id, ME).work.chapter_ids == [cid(1), cid(3)]

    def test_start__returns_none_chapter_ids_when_an_end_lost_its_position(
        self, repo: Repo, novel_id: str
    ) -> None:
        add_chapters(repo, novel_id, ["downloaded", "pending", "downloaded"])
        view, _ = repo.exports.create(novel_id, 1, 3, EPUB, skip_missing=True)
        remove(repo, 3)
        assert start_build(repo, view.id, ME).work.chapter_ids is None

    def test_claim__finds_nothing_while_the_build_runs(self, repo: Repo, novel_id: str) -> None:
        repo.tasks.claim(OTHER, TTL)  # the novel's refresh
        build_claim(repo, novel_id)
        assert repo.tasks.claim(OTHER, TTL) is None


class TestFinishBuild:
    def test_finish__sets_ready(self, repo: Repo, novel_id: str, clock: FakeClock) -> None:
        export_id = build_claim(repo, novel_id)
        clock.advance(seconds=5)
        assert finish_build(repo, export_id, ready("exports/a.epub", 5_000_000_000)) is not None
        row = export_row(repo, export_id)
        assert (row.state, row.storage_key, row.size_bytes) == (
            "ready",
            "exports/a.epub",
            5_000_000_000,
        )
        assert row.ready_at == clock.current

    def test_finish__removes_a_trash_row_for_its_key(self, repo: Repo, novel_id: str) -> None:
        export_id = build_claim(repo, novel_id)
        repo.trash.discard(["exports/a.epub"])  # an earlier owner that lost its claim

        assert finish_build(repo, export_id, ready("exports/a.epub")) is not None

        assert trash_keys(repo.engine) == []

    def test_finish__lost_claim_leaves_the_trash_alone(self, repo: Repo, novel_id: str) -> None:
        export_id = build_claim(repo, novel_id)
        repo.trash.discard(["exports/a.epub"])

        assert finish_build(repo, export_id, ready("exports/a.epub"), OTHER) is None

        assert trash_keys(repo.engine) == ["exports/a.epub"]

    def test_finish__appends_an_ex_event(self, repo: Repo, novel_id: str) -> None:
        export_id = build_claim(repo, novel_id)
        assert finish_build(repo, export_id, ready("k")) is not None
        last = ex_events(repo, export_id)[-1]
        assert last["state"] == "ready"
        assert last["url"] == "/static/k"

    @pytest.mark.parametrize("lost", ["other_worker", "expired", "recovered"])
    def test_finish__returns_none_and_changes_nothing_when_the_claim_was_lost(
        self, repo: Repo, novel_id: str, clock: FakeClock, lost: str
    ) -> None:
        export_id = build_claim(repo, novel_id)
        task = live_task(repo, export_id, TaskState.RUNNING)
        worker = ME
        if lost == "other_worker":
            worker = OTHER
        else:
            clock.advance(seconds=TTL + 1)
            if lost == "recovered":
                repo.tasks.recover()
        seq = repo.events.latest_seq()
        assert repo.tasks.finish(task, worker, ready("k")) is None
        assert export_row(repo, export_id).state != "ready"
        assert repo.events.latest_seq() == seq


class TestFailBuild:
    def test_finish__failed_sets_failed_with_the_error(self, repo: Repo, novel_id: str) -> None:
        export_id = build_claim(repo, novel_id)
        assert finish_build(repo, export_id, Failed("ValueError: boom")) is not None
        row = export_row(repo, export_id)
        assert (row.state, row.error) == ("failed", "ValueError: boom")
        last = ex_events(repo, export_id)[-1]
        assert (last["state"], last["error"]) == ("failed", "ValueError: boom")

    def test_finish__failed_changes_nothing_when_the_claim_was_lost(
        self, repo: Repo, novel_id: str
    ) -> None:
        export_id = build_claim(repo, novel_id)
        seq = repo.events.latest_seq()
        assert finish_build(repo, export_id, Failed("boom"), OTHER) is None
        assert export_row(repo, export_id).state == "building"
        assert repo.events.latest_seq() == seq


class TestRecoverBuild:
    def test_recover__moves_a_lapsed_building_export_back_to_waiting_with_an_ex_event(
        self, repo: Repo, novel_id: str, clock: FakeClock
    ) -> None:
        export_id = build_claim(repo, novel_id)
        clock.advance(seconds=TTL + 1)
        report = repo.tasks.recover()
        assert report.requeued == 1
        assert export_row(repo, export_id).state == "waiting"
        assert live_task(repo, export_id).claimed_by is None
        assert ex_events(repo, export_id)[-1]["state"] == "waiting"

    def test_recover__leaves_live_claims_alone(
        self, repo: Repo, novel_id: str, clock: FakeClock
    ) -> None:
        export_id = build_claim(repo, novel_id)
        clock.advance(seconds=TTL - 1)
        assert repo.tasks.recover().requeued == 0
        assert export_row(repo, export_id).state == "building"


class TestChapterChangeEvents:
    def test_affected_waiting__still_emits_ex_events_for_chapter_changes(
        self, repo: Repo, novel_id: str
    ) -> None:
        add_chapters(repo, novel_id, ["not_requested", "downloaded"])
        view, _ = repo.exports.create(novel_id, 1, 2, EPUB, skip_missing=True)
        before = len(ex_events(repo, view.id))
        repo.chapters.patch(
            novel_id,
            ChapterState.PENDING,
            [1],
        )
        assert len(ex_events(repo, view.id)) == before + 1


class TestStartBuild__WhenTheRangeChangedAfterTheClaim:
    @pytest.fixture
    def skipped(self, repo: Repo, novel_id: str, request: pytest.FixtureRequest) -> tuple[str, int]:
        """A build claimed, then its range made incomplete before it starts: (export, seq)."""
        add_chapters(repo, novel_id, ["downloaded", "downloaded", "downloaded"])
        view, _ = repo.exports.create(novel_id, 1, 3, EPUB)
        claimed = force_claim(
            repo, live_task(repo, view.id).id, ME, repo.clock() + timedelta(seconds=TTL)
        )
        if request.param == "redownload":
            set_state(repo, 2, state="pending")
        else:
            set_state(repo, 3, position=4)
            add_chapters_at(repo, novel_id, 3)
        seq = repo.events.latest_seq()
        assert repo.tasks.start(claimed, ME) == Skipped("not buildable")
        return view.id, seq

    @pytest.mark.parametrize("skipped", ["redownload", "sync_insert"], indirect=True)
    def test_start__skips_the_build_and_leaves_the_export_waiting(
        self, repo: Repo, skipped: tuple[str, int]
    ) -> None:
        export_id, seq = skipped
        assert export_row(repo, export_id).state == "waiting"
        assert [e.entity_id for e in repo.events.since(seq)] == [tasks_of(repo, export_id)[0].id]
        assert tasks_of(repo, export_id)[0].state is TaskState.SKIPPED


def add_chapters_at(repo: Repo, novel_id: str, position: int) -> None:
    """Insert one `pending` chapter at `position` (a sync inserting upstream)."""
    with db.write(repo.engine) as conn:
        conn.execute(
            insert(chapters).values(
                **chapter_row(
                    id=cid(90 + position),
                    novel_id=novel_id,
                    url=f"{URL}/new{position}",
                    position=position,
                    state="pending",
                )
            )
        )
