"""The chapters repository: list, get, patch, queue, events (spec sections 4, 5.2 and 5.4)."""

import threading
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import insert, select, update

from novelhog.domain.chapters import ChapterRange, ChapterState
from novelhog.domain.errors import NotFound, ValidationFailed
from novelhog.repo import Repo, db
from novelhog.repo import chapters as chapters_repo
from novelhog.repo import events as event_log
from novelhog.repo.chapters import error_of
from novelhog.repo.schema import chapter_contents, chapters, events, exports
from tests.conftest import FakeClock
from tests.integration.helpers import assert_no_events_after_delete, chapter_row, export_row

URL = "https://example.com/novel"


@pytest.fixture
def novel_id(repo: Repo) -> str:
    repo.hosts.publish(["example.com"])
    return repo.novels.create(URL, followed=True).id


def chapter_id(position: int) -> str:
    return f"CH{position:012d}"


def add_chapters(repo: Repo, novel_id: str, specs: list[dict[str, Any]]) -> None:
    """Insert chapters at positions 1..n with `specs[i]` overrides."""
    with db.write(repo.engine) as conn:
        for i, spec in enumerate(specs, start=1):
            row = (
                chapter_row(id=chapter_id(i), novel_id=novel_id, url=f"{URL}/{i}", position=i)
                | spec
            )
            conn.execute(insert(chapters).values(**row))


def set_state(repo: Repo, at: int, **values: Any) -> None:
    with db.write(repo.engine) as conn:
        conn.execute(update(chapters).where(chapters.c.id == chapter_id(at)).values(**values))


def chapter_events(repo: Repo, position: int) -> list[Any]:
    with db.read(repo.engine) as conn:
        statement = (
            select(events).where(events.c.entity_id == chapter_id(position)).order_by(events.c.seq)
        )
        return list(conn.execute(statement).all())


def all_events(repo: Repo) -> list[Any]:
    with db.read(repo.engine) as conn:
        return list(conn.execute(select(events).order_by(events.c.seq)).all())


def row_of(repo: Repo, position: int) -> Any:
    with db.read(repo.engine) as conn:
        return conn.execute(select(chapters).where(chapters.c.id == chapter_id(position))).one()


def write_error_event(repo: Repo, novel_id: str, position: int, message: str) -> None:
    """Move a chapter to the error state and append the CH event the worker would write."""
    set_state(repo, position, state="error")
    with db.write(repo.engine) as conn:
        event_log.emit_novel_change(
            conn,
            novel_id,
            [chapter_id(position)],
            now=repo.clock(),
            static_base_url="/static",
            errors={chapter_id(position): (message, repo.clock())},
        )


class TestList:
    def test_list__returns_positioned_chapters_ordered_by_position_with_total(
        self, repo: Repo, novel_id: str, clock: FakeClock
    ) -> None:
        add_chapters(repo, novel_id, [{}, {}, {}])
        set_state(repo, 2, position=None, removed_at=clock.current)
        items, total = repo.chapters.list(novel_id)
        assert [c.position for c in items] == [1, 3]
        assert total == 2

    def test_list__applies_offset_and_limit(self, repo: Repo, novel_id: str) -> None:
        add_chapters(repo, novel_id, [{}, {}, {}, {}])
        items, total = repo.chapters.list(novel_id, offset=1, limit=2)
        assert [c.position for c in items] == [2, 3]
        assert total == 4

    def test_list__filters_by_state_when_given(self, repo: Repo, novel_id: str) -> None:
        add_chapters(repo, novel_id, [{}, {"state": "downloaded"}, {"state": "downloaded"}])
        items, total = repo.chapters.list(novel_id, state=ChapterState.DOWNLOADED)
        assert [c.position for c in items] == [2, 3]
        assert total == 2

    def test_list__caps_limit_at_5000(self, repo: Repo, novel_id: str) -> None:
        add_chapters(repo, novel_id, [{}, {}])
        items, _ = repo.chapters.list(novel_id, limit=10**9)
        assert len(items) == 2

    def test_list__caps_limit_at_5000__bound(
        self, repo: Repo, novel_id: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        add_chapters(repo, novel_id, [{} for _ in range(5)])
        monkeypatch.setattr("novelhog.repo.chapters.MAX_LIMIT", 3)
        items, total = repo.chapters.list(novel_id, limit=10**9)
        assert (len(items), total) == (3, 5)

    def test_list__unknown_novel_raises_not_found(self, repo: Repo) -> None:
        with pytest.raises(NotFound):
            repo.chapters.list("NV000000000099")

    def test_list__malformed_novel_id_raises_not_found(self, repo: Repo) -> None:
        with pytest.raises(NotFound):
            repo.chapters.list("nope")


class TestList__Errors:
    def test_list__error_comes_from_the_newest_chapter_event_while_in_error(
        self, repo: Repo, novel_id: str
    ) -> None:
        add_chapters(repo, novel_id, [{}, {}])
        write_error_event(repo, novel_id, 1, "boom")
        items, _ = repo.chapters.list(novel_id)
        assert items[0].state is ChapterState.ERROR
        assert items[0].error == "boom"
        assert items[0].error_at == repo.clock()
        assert items[1].error is None
        assert items[1].error_at is None

    def test_list__error_is_null_after_the_chapter_left_the_error_state(
        self, repo: Repo, novel_id: str
    ) -> None:
        add_chapters(repo, novel_id, [{}])
        write_error_event(repo, novel_id, 1, "boom")
        repo.chapters.patch(novel_id, ChapterState.PENDING, [1])
        (item,), _ = repo.chapters.list(novel_id)
        assert item.state is ChapterState.PENDING
        assert item.error is None


class TestGet:
    def test_get__returns_the_chapter(self, repo: Repo, novel_id: str) -> None:
        add_chapters(repo, novel_id, [{"title": "One"}])
        view = repo.chapters.get(chapter_id(1))
        assert (view.id, view.position, view.title, view.url) == (
            chapter_id(1),
            1,
            "One",
            f"{URL}/1",
        )

    def test_get__error_chapter_carries_error_and_error_at(self, repo: Repo, novel_id: str) -> None:
        add_chapters(repo, novel_id, [{}])
        write_error_event(repo, novel_id, 1, "boom")
        view = repo.chapters.get(chapter_id(1))
        assert (view.error, view.error_at) == ("boom", repo.clock())

    def test_get__unknown_id_raises_not_found(self, repo: Repo) -> None:
        with pytest.raises(NotFound):
            repo.chapters.get("CH000000000099")

    def test_get__removed_chapter_raises_not_found(
        self, repo: Repo, novel_id: str, clock: FakeClock
    ) -> None:
        add_chapters(repo, novel_id, [{}])
        set_state(repo, 1, position=None, removed_at=clock.current)
        with pytest.raises(NotFound):
            repo.chapters.get(chapter_id(1))

    def test_get__malformed_id_raises_not_found(self, repo: Repo) -> None:
        with pytest.raises(NotFound):
            repo.chapters.get("nope")


class TestPatch__ToPending:
    @pytest.mark.parametrize("state", ["not_requested", "error", "downloaded"])
    def test_patch__sets_pending_queued_at_now_priority_0(
        self, repo: Repo, novel_id: str, clock: FakeClock, state: str
    ) -> None:
        add_chapters(repo, novel_id, [{"state": state, "priority": 1}])
        clock.advance(minutes=5)
        result = repo.chapters.patch(novel_id, ChapterState.PENDING, [1])
        row = row_of(repo, 1)
        assert (row.state, row.queued_at, row.priority) == ("pending", clock.current, 0)
        assert row.updated_at == clock.current
        assert result.updated == [1]
        assert result.skipped == []

    def test_patch__appends_one_ch_event_per_chapter_one_nv_event(
        self, repo: Repo, novel_id: str
    ) -> None:
        add_chapters(repo, novel_id, [{}, {}, {}])
        before = len(all_events(repo))
        repo.chapters.patch(novel_id, ChapterState.PENDING, ChapterRange(1, 2))
        new = all_events(repo)[before:]
        assert [e.entity_id for e in new] == [chapter_id(1), chapter_id(2), novel_id]
        assert new[0].payload["state"] == "pending"
        assert new[0].payload["novel_id"] == novel_id

    def test_patch__returns_updated_positions_ordered(self, repo: Repo, novel_id: str) -> None:
        add_chapters(repo, novel_id, [{}, {}, {}])
        result = repo.chapters.patch(novel_id, ChapterState.PENDING, [3, 1])
        assert result.updated == [1, 3]

    def test_patch__redownload_keeps_the_stored_content(
        self, repo: Repo, novel_id: str, clock: FakeClock
    ) -> None:
        add_chapters(repo, novel_id, [{"state": "downloaded"}])
        with db.write(repo.engine) as conn:
            conn.execute(
                insert(chapter_contents).values(
                    chapter_id=chapter_id(1), markdown="text", fetched_at=clock.current
                )
            )
        repo.chapters.patch(novel_id, ChapterState.PENDING, [1])
        with db.read(repo.engine) as conn:
            assert conn.execute(select(chapter_contents.c.markdown)).scalar_one() == "text"

    def test_patch__writes_ex_events_for_waiting_exports_containing_a_changed_chapter(
        self, repo: Repo, novel_id: str
    ) -> None:
        add_chapters(repo, novel_id, [{}, {}, {}, {}])
        with db.write(repo.engine) as conn:
            conn.execute(
                insert(exports).values(
                    **export_row(
                        id="EX000000000001",
                        novel_id=novel_id,
                        first_chapter_id=chapter_id(1),
                        last_chapter_id=chapter_id(2),
                    )
                )
            )
            conn.execute(
                insert(exports).values(
                    **export_row(
                        id="EX000000000002",
                        novel_id=novel_id,
                        first_chapter_id=chapter_id(3),
                        last_chapter_id=chapter_id(4),
                    )
                )
            )
        before = len(all_events(repo))
        repo.chapters.patch(novel_id, ChapterState.PENDING, [2])
        new = all_events(repo)[before:]
        assert [e.entity_id for e in new] == [chapter_id(2), novel_id, "EX000000000001"]
        payload = new[2].payload
        assert payload["progress"] == {"downloaded": 0, "total": 2}
        assert payload["blocked"] == {"errors": [], "not_queued": [1]}


class TestPatch__ToNotRequested:
    def test_patch__clears_queued_at(self, repo: Repo, novel_id: str, clock: FakeClock) -> None:
        add_chapters(repo, novel_id, [{"state": "pending", "queued_at": clock.current}])
        result = repo.chapters.patch(novel_id, ChapterState.NOT_REQUESTED, [1])
        row = row_of(repo, 1)
        assert (row.state, row.queued_at) == ("not_requested", None)
        assert result.updated == [1]

    def test_patch__clears_the_claim_of_an_unstarted_chapter(
        self, repo: Repo, novel_id: str, clock: FakeClock
    ) -> None:
        add_chapters(
            repo,
            novel_id,
            [
                {
                    "state": "pending",
                    "queued_at": clock.current,
                    "claimed_by": "w1",
                    "claimed_until": clock.current + timedelta(minutes=5),
                }
            ],
        )
        repo.chapters.patch(novel_id, ChapterState.NOT_REQUESTED, [1])
        row = row_of(repo, 1)
        assert (row.state, row.claimed_by, row.claimed_until) == ("not_requested", None, None)


def add_export(
    repo: Repo, novel_id: str, export_id: str, first: int, last: int, **values: Any
) -> None:
    with db.write(repo.engine) as conn:
        conn.execute(
            insert(exports).values(
                **export_row(
                    id=export_id,
                    novel_id=novel_id,
                    first_chapter_id=chapter_id(first),
                    last_chapter_id=chapter_id(last),
                )
                | values
            )
        )


def export_of(repo: Repo, export_id: str) -> Any:
    with db.read(repo.engine) as conn:
        return conn.execute(select(exports).where(exports.c.id == export_id)).one()


PENDING = {"state": "pending", "queued_at": export_row()["created_at"]}


class TestPatch__DequeueNeededByAWaitingExport:
    def test_patch__fails_the_export_with_the_chapter_number(
        self, repo: Repo, novel_id: str
    ) -> None:
        add_chapters(repo, novel_id, [{}, PENDING, {}])
        add_export(repo, novel_id, "EX000000000001", 1, 3)
        repo.chapters.patch(novel_id, ChapterState.NOT_REQUESTED, [2])
        row = export_of(repo, "EX000000000001")
        assert (row.state, row.error) == ("failed", "Chapter 2 was cancelled")
        assert row_of(repo, 2).state == "not_requested"

    def test_patch__lists_several_cancelled_positions_ascending(
        self, repo: Repo, novel_id: str
    ) -> None:
        add_chapters(repo, novel_id, [{}, PENDING, PENDING, PENDING])
        add_export(repo, novel_id, "EX000000000001", 1, 4)
        repo.chapters.patch(novel_id, ChapterState.NOT_REQUESTED, [4, 2, 3])
        assert export_of(repo, "EX000000000001").error == "Chapters 2, 3, 4 were cancelled"

    def test_patch__appends_the_failed_ex_event_after_the_ch_and_nv_events(
        self, repo: Repo, novel_id: str
    ) -> None:
        add_chapters(repo, novel_id, [PENDING, {}])
        add_export(repo, novel_id, "EX000000000001", 1, 2)
        before = len(all_events(repo))
        repo.chapters.patch(novel_id, ChapterState.NOT_REQUESTED, [1])
        new = all_events(repo)[before:]
        assert [e.entity_id for e in new] == [chapter_id(1), novel_id, "EX000000000001"]
        assert new[2].payload["state"] == "failed"
        assert new[2].payload["error"] == "Chapter 1 was cancelled"

    def test_patch__fails_every_waiting_export_covering_the_chapter_with_one_event_each(
        self, repo: Repo, novel_id: str
    ) -> None:
        add_chapters(repo, novel_id, [PENDING, {}])
        add_export(repo, novel_id, "EX000000000001", 1, 2)
        add_export(repo, novel_id, "EX000000000002", 1, 1)
        before = len(all_events(repo))
        repo.chapters.patch(novel_id, ChapterState.NOT_REQUESTED, [1])
        assert export_of(repo, "EX000000000001").state == "failed"
        assert export_of(repo, "EX000000000002").state == "failed"
        ex = [e.entity_id for e in all_events(repo)[before:] if e.entity_id.startswith("EX")]
        assert sorted(ex) == ["EX000000000001", "EX000000000002"]

    def test_patch__keeps_a_skip_missing_export_waiting(self, repo: Repo, novel_id: str) -> None:
        add_chapters(repo, novel_id, [PENDING, {}])
        add_export(repo, novel_id, "EX000000000001", 1, 2, skip_missing=True)
        repo.chapters.patch(novel_id, ChapterState.NOT_REQUESTED, [1])
        assert export_of(repo, "EX000000000001").state == "waiting"

    def test_patch__leaves_exports_outside_the_range_alone(self, repo: Repo, novel_id: str) -> None:
        add_chapters(repo, novel_id, [{}, {}, PENDING])
        add_export(repo, novel_id, "EX000000000001", 1, 2)
        repo.chapters.patch(novel_id, ChapterState.NOT_REQUESTED, [3])
        assert export_of(repo, "EX000000000001").state == "waiting"

    def test_patch__leaves_building_ready_and_failed_exports_alone(
        self, repo: Repo, novel_id: str
    ) -> None:
        add_chapters(repo, novel_id, [PENDING])
        add_export(
            repo,
            novel_id,
            "EX000000000001",
            1,
            1,
            state="building",
            claimed_by="w1",
            claimed_until=repo.clock() + timedelta(minutes=5),
        )
        add_export(
            repo, novel_id, "EX000000000002", 1, 1, state="ready", storage_key="k", size_bytes=1
        )
        add_export(repo, novel_id, "EX000000000003", 1, 1, state="failed", error="boom")
        repo.chapters.patch(novel_id, ChapterState.NOT_REQUESTED, [1])
        assert export_of(repo, "EX000000000001").state == "building"
        assert export_of(repo, "EX000000000002").state == "ready"
        row = export_of(repo, "EX000000000003")
        assert (row.state, row.error) == ("failed", "boom")

    def test_patch__download_does_not_fail_a_waiting_export(
        self, repo: Repo, novel_id: str
    ) -> None:
        add_chapters(repo, novel_id, [{}])
        add_export(repo, novel_id, "EX000000000001", 1, 1)
        repo.chapters.patch(novel_id, ChapterState.PENDING, [1])
        assert export_of(repo, "EX000000000001").state == "waiting"


class TestPatch__OnOtherStates:
    def test_patch__reports_unchanged_chapters_in_skipped_with_their_state(
        self, repo: Repo, novel_id: str, clock: FakeClock
    ) -> None:
        add_chapters(
            repo,
            novel_id,
            [
                {},
                {
                    "state": "downloading",
                    "queued_at": clock.current,
                    "claimed_by": "w1",
                    "claimed_until": clock.current + timedelta(minutes=5),
                },
                {"state": "pending", "queued_at": clock.current},
            ],
        )
        result = repo.chapters.patch(novel_id, ChapterState.PENDING, [1, 2, 3])
        assert result.updated == [1]
        assert result.skipped == [(2, ChapterState.DOWNLOADING), (3, ChapterState.PENDING)]
        assert row_of(repo, 2).state == "downloading"

    def test_patch__dequeue_of_a_downloading_chapter_is_skipped(
        self, repo: Repo, novel_id: str, clock: FakeClock
    ) -> None:
        add_chapters(
            repo,
            novel_id,
            [
                {
                    "state": "downloading",
                    "queued_at": clock.current,
                    "claimed_by": "w1",
                    "claimed_until": clock.current + timedelta(minutes=5),
                }
            ],
        )
        result = repo.chapters.patch(novel_id, ChapterState.NOT_REQUESTED, [1])
        assert result.updated == []
        assert result.skipped == [(1, ChapterState.DOWNLOADING)]

    def test_patch__nothing_changed_appends_no_events(self, repo: Repo, novel_id: str) -> None:
        add_chapters(repo, novel_id, [{"state": "downloaded"}])
        before = len(all_events(repo))
        repo.chapters.patch(novel_id, ChapterState.NOT_REQUESTED, [1])
        assert len(all_events(repo)) == before


class TestPatch__WithClientForbiddenTarget:
    @pytest.mark.parametrize("target", ["downloading", "downloaded", "error"])
    def test_patch__raises_invalid_target_state_and_changes_nothing(
        self, repo: Repo, novel_id: str, target: str
    ) -> None:
        add_chapters(repo, novel_id, [{}])
        before = len(all_events(repo))
        with pytest.raises(ValidationFailed) as caught:
            repo.chapters.patch(novel_id, ChapterState(target), [1])
        assert caught.value.code == "invalid_target_state"
        assert row_of(repo, 1).state == "not_requested"
        assert len(all_events(repo)) == before


class TestPatch__WithInvalidSelector:
    @pytest.mark.parametrize("selector", [[], [0], [3], ChapterRange(2, 1), ChapterRange(1, 3)])
    def test_patch__raises_validation_failed_and_changes_nothing(
        self, repo: Repo, novel_id: str, selector: Any
    ) -> None:
        add_chapters(repo, novel_id, [{}, {}])
        before = len(all_events(repo))
        with pytest.raises(ValidationFailed):
            repo.chapters.patch(novel_id, ChapterState.PENDING, selector)
        assert row_of(repo, 1).state == "not_requested"
        assert len(all_events(repo)) == before

    def test_patch__novel_without_chapters_raises_validation_failed(
        self, repo: Repo, novel_id: str
    ) -> None:
        with pytest.raises(ValidationFailed):
            repo.chapters.patch(novel_id, ChapterState.PENDING, [1])


class TestPatch__OnUnknownNovel:
    def test_patch__raises_not_found(self, repo: Repo) -> None:
        with pytest.raises(NotFound):
            repo.chapters.patch("NV000000000099", ChapterState.PENDING, [1])


class TestEvents__WrittenByAnyChapterChange:
    def test_events__nv_counters_equal_novels_get_after_the_change(
        self, repo: Repo, novel_id: str
    ) -> None:
        add_chapters(repo, novel_id, [{}, {"state": "downloaded"}, {}])
        repo.chapters.patch(novel_id, ChapterState.PENDING, [1, 2])
        nv = [e for e in all_events(repo) if e.entity_id == novel_id][-1]
        assert nv.payload == repo.novels.get(novel_id).to_json()
        assert nv.payload["pending_pages"] == 2
        assert nv.payload["downloaded_pages"] == 0

    def test_events__ex_event_matches_exports_get_shape_for_a_waiting_export(
        self, repo: Repo, novel_id: str
    ) -> None:
        add_chapters(repo, novel_id, [{"state": "downloaded"}, {}])
        with db.write(repo.engine) as conn:
            conn.execute(
                insert(exports).values(
                    **export_row(
                        novel_id=novel_id,
                        first_chapter_id=chapter_id(1),
                        last_chapter_id=chapter_id(2),
                        skip_missing=True,
                    )
                )
            )
        repo.chapters.patch(novel_id, ChapterState.PENDING, [2])
        ex = [e for e in all_events(repo) if e.entity_id == "EX000000000001"][-1]
        assert ex.payload["progress"] == {"downloaded": 1, "total": 2}
        assert ex.payload["blocked"] == {"errors": [], "not_queued": []}
        assert ex.payload["chapter_from"] == 1
        assert ex.payload["chapter_to"] == 2
        assert ex.payload["state"] == "waiting"
        assert ex.payload["url"] is None

    def test_events__no_ex_event_for_exports_that_are_not_waiting(
        self, repo: Repo, novel_id: str, clock: FakeClock
    ) -> None:
        add_chapters(repo, novel_id, [{}])
        with db.write(repo.engine) as conn:
            conn.execute(
                insert(exports).values(
                    **export_row(
                        novel_id=novel_id,
                        state="failed",
                        error="x",
                        first_chapter_id=chapter_id(1),
                        last_chapter_id=chapter_id(1),
                    )
                )
            )
        repo.chapters.patch(novel_id, ChapterState.PENDING, [1])
        assert not [e for e in all_events(repo) if e.entity_id.startswith("EX")]

    def test_events__later_snapshot_of_a_chapter_in_error_carries_the_previous_error(
        self, repo: Repo, novel_id: str, clock: FakeClock
    ) -> None:
        add_chapters(repo, novel_id, [{}])
        write_error_event(repo, novel_id, 1, "boom")
        first = chapter_events(repo, 1)[-1].payload
        clock.advance(minutes=10)
        with db.write(repo.engine) as conn:
            event_log.emit_novel_change(
                conn, novel_id, [chapter_id(1)], now=clock(), static_base_url="/static"
            )
        later = chapter_events(repo, 1)[-1].payload
        assert later["error"] == "boom"
        assert later["error_at"] == first["error_at"]
        assert later["updated_at"] == first["updated_at"]

    def test_events__requeue_writes_a_snapshot_without_the_error(
        self, repo: Repo, novel_id: str
    ) -> None:
        add_chapters(repo, novel_id, [{}])
        write_error_event(repo, novel_id, 1, "boom")
        repo.chapters.patch(novel_id, ChapterState.PENDING, [1])
        payload = chapter_events(repo, 1)[-1].payload
        assert (payload["state"], payload["error"], payload["error_at"]) == ("pending", None, None)


class TestErrorOf:
    def test_error_of__returns_message_and_time_from_the_newest_event(
        self, repo: Repo, novel_id: str
    ) -> None:
        add_chapters(repo, novel_id, [{}])
        write_error_event(repo, novel_id, 1, "boom")
        with db.read(repo.engine) as conn:
            assert error_of(conn, chapter_id(1)) == ("boom", repo.clock())

    def test_error_of__returns_none_without_events(self, repo: Repo, novel_id: str) -> None:
        add_chapters(repo, novel_id, [{}])
        with db.read(repo.engine) as conn:
            assert error_of(conn, chapter_id(1)) is None


def _race_patch_and_delete(repo: Repo, novel: str, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Hold `patch` inside its transaction (novel row locked) while `delete` runs into it."""
    has_row = threading.Event()
    outcomes: list[str] = []
    original = chapters_repo._transition

    def slow_transition(*args: Any, **kwargs: Any) -> Any:
        has_row.set()
        threading.Event().wait(1.0)  # let delete run into the held row lock
        return original(*args, **kwargs)

    monkeypatch.setattr(chapters_repo, "_transition", slow_transition)

    def patcher() -> None:
        try:
            repo.chapters.patch(novel, ChapterState.PENDING, ChapterRange(1, 2))
            outcomes.append("patch:ok")
        except BaseException as error:  # noqa: BLE001  (reported through the outcomes)
            outcomes.append(f"patch:{error!r}")
        finally:
            has_row.set()

    def deleter() -> None:
        try:
            has_row.wait(timeout=30)
            repo.novels.delete(novel)
            outcomes.append("delete:ok")
        except BaseException as error:  # noqa: BLE001  (reported through the outcomes)
            outcomes.append(f"delete:{error!r}")

    threads = [threading.Thread(target=f, daemon=True) for f in (patcher, deleter)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
        assert not t.is_alive()
    return sorted(outcomes)


class TestPatch__RacingADelete:
    """Both writers lock the novel row first, so neither deadlocks nor breaks a foreign key."""

    def test_patch__and_delete_serialise_without_deadlock_or_fk_violation(
        self, repo: Repo, novel_id: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        add_chapters(repo, novel_id, [{}, {}])
        assert _race_patch_and_delete(repo, novel_id, monkeypatch) == ["delete:ok", "patch:ok"]
        with db.read(repo.engine) as conn:
            assert conn.execute(select(chapters).where(chapters.c.novel_id == novel_id)).all() == []
        assert_no_events_after_delete(repo.engine, novel_id)
