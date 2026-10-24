"""The TOC diff by URL (spec sections 4 and 6.2)."""

import threading
from typing import Any

import pytest
from sqlalchemy import insert, select, update

from novelhog.repo import Repo, db
from novelhog.repo import events as event_log
from novelhog.repo.chapters import TocDiff, apply_toc
from novelhog.repo.novels import lock_novel
from novelhog.repo.schema import chapter_contents, chapters, events, exports
from tests.conftest import FakeClock
from tests.integration.helpers import assert_no_events_after_delete, export_row

BASE = "https://example.com/novel"


def u(n: int) -> str:
    return f"{BASE}/{n}"


@pytest.fixture
def novel_id(repo: Repo) -> str:
    repo.hosts.publish(["example.com"])
    return repo.novels.create(BASE, followed=True).id


def sync(
    repo: Repo, novel_id: str, urls: list[str], *, initial: bool = False, followed: bool = True
) -> TocDiff:
    """Run the row-writing half of a sync transaction, then its events."""
    now = repo.clock()
    with db.write(repo.engine) as conn:
        assert lock_novel(conn, novel_id)
        diff = apply_toc(conn, novel_id, urls, initial=initial, followed=followed, now=now)
        event_log.emit_novel_change(
            conn,
            novel_id,
            diff.changed_ids,
            now=now,
            static_base_url="/static",
            released=diff.new,
        )
    return diff


def rows(repo: Repo, novel_id: str) -> dict[str, Any]:
    with db.read(repo.engine) as conn:
        found = conn.execute(select(chapters).where(chapters.c.novel_id == novel_id)).all()
    return {r.url: r for r in found}


def chapter_events(repo: Repo) -> list[Any]:
    with db.read(repo.engine) as conn:
        statement = select(events).where(events.c.entity_id.like("CH%")).order_by(events.c.seq)
        return list(conn.execute(statement).all())


class TestApplyToc__OnTheInitialImport:
    def test_apply_toc__inserts_every_url_not_requested_unreleased_at_its_position(
        self, repo: Repo, novel_id: str
    ) -> None:
        sync(repo, novel_id, [u(1), u(2), u(3)], initial=True)
        found = rows(repo, novel_id)
        assert {url: (r.position, r.state, r.released_at) for url, r in found.items()} == {
            u(1): (1, "not_requested", None),
            u(2): (2, "not_requested", None),
            u(3): (3, "not_requested", None),
        }

    def test_apply_toc__returns_no_new_positions(self, repo: Repo, novel_id: str) -> None:
        diff = sync(repo, novel_id, [u(1), u(2)], initial=True)
        assert diff.new == []
        assert len(diff.changed_ids) == 2

    def test_apply_toc__initial_import_of_a_followed_novel_does_not_queue(
        self, repo: Repo, novel_id: str
    ) -> None:
        sync(repo, novel_id, [u(1)], initial=True, followed=True)
        row = rows(repo, novel_id)[u(1)]
        assert (row.state, row.queued_at, row.priority) == ("not_requested", None, 0)


class TestApplyToc__OnLaterSyncWithNewUrls:
    def test_apply_toc__followed_novel_queues_new_chapters_with_priority_1(
        self, repo: Repo, novel_id: str, clock: FakeClock
    ) -> None:
        sync(repo, novel_id, [u(1)], initial=True)
        clock.advance(hours=6)
        diff = sync(repo, novel_id, [u(1), u(2), u(3)])
        found = rows(repo, novel_id)
        for url in (u(2), u(3)):
            r = found[url]
            assert (r.state, r.queued_at, r.priority, r.released_at) == (
                "pending",
                clock.current,
                1,
                clock.current,
            )
        assert diff.new == [2, 3]

    def test_apply_toc__unfollowed_novel_adds_not_requested_released_chapters(
        self, repo: Repo, novel_id: str, clock: FakeClock
    ) -> None:
        sync(repo, novel_id, [u(1)], initial=True, followed=False)
        clock.advance(hours=6)
        diff = sync(repo, novel_id, [u(1), u(2)], followed=False)
        r = rows(repo, novel_id)[u(2)]
        assert (r.state, r.queued_at, r.released_at) == ("not_requested", None, clock.current)
        assert diff.new == [2]

    def test_apply_toc__nv_event_carries_the_released_positions(
        self, repo: Repo, novel_id: str, clock: FakeClock
    ) -> None:
        sync(repo, novel_id, [u(1)], initial=True)
        clock.advance(hours=6)
        sync(repo, novel_id, [u(1), u(2)])
        with db.read(repo.engine) as conn:
            nv = conn.execute(
                select(events).where(events.c.entity_id == novel_id).order_by(events.c.seq.desc())
            ).first()
        assert nv is not None
        assert nv.payload["released"] == [2]
        assert nv.payload["total_pages"] == 2
        assert nv.payload["pending_pages"] == 1


class TestApplyToc__WithAUrlInsertedInTheMiddle:  # RF-4
    def test_apply_toc__existing_rows_keep_ids_states_and_content_later_rows_shift(
        self, repo: Repo, novel_id: str, clock: FakeClock
    ) -> None:
        sync(repo, novel_id, [u(1), u(2), u(3)], initial=True)
        before = rows(repo, novel_id)
        with db.write(repo.engine) as conn:
            conn.execute(
                update(chapters).where(chapters.c.id == before[u(3)].id).values(state="downloaded")
            )
            conn.execute(
                insert(chapter_contents).values(
                    chapter_id=before[u(3)].id, markdown="three", fetched_at=clock.current
                )
            )
        clock.advance(hours=6)
        diff = sync(repo, novel_id, [u(1), u(9), u(2), u(3)])
        after = rows(repo, novel_id)
        assert {url: after[url].id for url in (u(1), u(2), u(3))} == {
            url: before[url].id for url in (u(1), u(2), u(3))
        }
        assert (after[u(1)].position, after[u(9)].position) == (1, 2)
        assert (after[u(2)].position, after[u(3)].position) == (3, 4)
        assert after[u(3)].state == "downloaded"
        assert diff.moved == [before[u(2)].id, before[u(3)].id]
        assert diff.new == [2]
        with db.read(repo.engine) as conn:
            assert conn.execute(select(chapter_contents.c.markdown)).scalar_one() == "three"


class TestApplyToc__WithAUrlRemoved:  # RF-4
    def test_apply_toc__keeps_state_and_content_clears_position_and_moves_later_rows_up(
        self, repo: Repo, novel_id: str, clock: FakeClock
    ) -> None:
        sync(repo, novel_id, [u(1), u(2), u(3)], initial=True)
        before = rows(repo, novel_id)
        with db.write(repo.engine) as conn:
            conn.execute(
                update(chapters).where(chapters.c.id == before[u(2)].id).values(state="downloaded")
            )
        clock.advance(hours=6)
        diff = sync(repo, novel_id, [u(1), u(3)])
        after = rows(repo, novel_id)
        removed = after[u(2)]
        assert (removed.position, removed.removed_at, removed.state) == (
            None,
            clock.current,
            "downloaded",
        )
        assert after[u(3)].position == 2
        assert diff.removed == [before[u(2)].id]
        assert diff.moved == [before[u(3)].id]

    def test_apply_toc__removed_pending_chapter_is_dequeued_and_its_claim_released(
        self, repo: Repo, novel_id: str, clock: FakeClock
    ) -> None:
        sync(repo, novel_id, [u(1), u(2)], initial=True)
        before = rows(repo, novel_id)
        with db.write(repo.engine) as conn:
            conn.execute(
                update(chapters)
                .where(chapters.c.id == before[u(2)].id)
                .values(
                    state="pending",
                    queued_at=clock.current,
                    claimed_by="w1",
                    claimed_until=clock.current.replace(year=2030),
                )
            )
        diff = sync(repo, novel_id, [u(1)])
        r = rows(repo, novel_id)[u(2)]
        assert (r.state, r.queued_at, r.claimed_by, r.claimed_until, r.position) == (
            "not_requested",
            None,
            None,
            None,
            None,
        )
        assert diff.dequeued == [before[u(2)].id]
        payload = chapter_events(repo)[-1].payload
        assert payload["state"] == "not_requested"
        assert payload["position"] is None

    def test_apply_toc__dequeuing_a_needed_chapter_does_not_fail_a_waiting_export(
        self, repo: Repo, novel_id: str, clock: FakeClock
    ) -> None:
        sync(repo, novel_id, [u(1), u(2)], initial=True)
        before = rows(repo, novel_id)
        with db.write(repo.engine) as conn:
            conn.execute(
                update(chapters)
                .where(chapters.c.id == before[u(2)].id)
                .values(state="pending", queued_at=clock.current)
            )
            conn.execute(
                insert(exports).values(
                    **export_row(
                        novel_id=novel_id,
                        first_chapter_id=before[u(1)].id,
                        last_chapter_id=before[u(2)].id,
                    )
                )
            )
        sync(repo, novel_id, [u(1)])
        with db.read(repo.engine) as conn:
            assert conn.execute(select(exports.c.state)).scalar_one() == "waiting"

    def test_apply_toc__removed_downloading_chapter_keeps_state_and_claim(
        self, repo: Repo, novel_id: str, clock: FakeClock
    ) -> None:
        sync(repo, novel_id, [u(1), u(2)], initial=True)
        before = rows(repo, novel_id)
        with db.write(repo.engine) as conn:
            conn.execute(
                update(chapters)
                .where(chapters.c.id == before[u(2)].id)
                .values(
                    state="downloading",
                    queued_at=clock.current,
                    claimed_by="w1",
                    claimed_until=clock.current.replace(year=2030),
                )
            )
        diff = sync(repo, novel_id, [u(1)])
        r = rows(repo, novel_id)[u(2)]
        assert (r.state, r.claimed_by, r.position) == ("downloading", "w1", None)
        assert diff.dequeued == []


class TestApplyToc__WithUrlsReordered:  # RF-4
    def test_apply_toc__positions_follow_the_toc_and_states_stay_with_urls(
        self, repo: Repo, novel_id: str, clock: FakeClock
    ) -> None:
        sync(repo, novel_id, [u(1), u(2), u(3)], initial=True)
        before = rows(repo, novel_id)
        with db.write(repo.engine) as conn:
            conn.execute(
                update(chapters).where(chapters.c.id == before[u(1)].id).values(state="downloaded")
            )
        clock.advance(hours=6)
        diff = sync(repo, novel_id, [u(3), u(1), u(2)])
        after = rows(repo, novel_id)
        assert [after[u(n)].position for n in (3, 1, 2)] == [1, 2, 3]
        assert after[u(1)].state == "downloaded"
        assert after[u(1)].id == before[u(1)].id
        assert sorted(diff.moved) == sorted(before[u(n)].id for n in (1, 2, 3))
        assert diff.new == []


class TestApplyToc__WhenARemovedUrlComesBack:
    def test_apply_toc__restores_position_clears_removed_at_and_reports_returned(
        self, repo: Repo, novel_id: str, clock: FakeClock
    ) -> None:
        sync(repo, novel_id, [u(1), u(2)], initial=True)
        original = rows(repo, novel_id)[u(2)]
        sync(repo, novel_id, [u(1)])
        clock.advance(hours=6)
        diff = sync(repo, novel_id, [u(1), u(2)])
        r = rows(repo, novel_id)[u(2)]
        assert (r.id, r.position, r.removed_at) == (original.id, 2, None)
        assert diff.returned == [original.id]
        assert diff.new == []
        assert diff.moved == []


class TestApplyToc__WithAUrlListedTwice:
    def test_apply_toc__keeps_the_first_position_only(self, repo: Repo, novel_id: str) -> None:
        sync(repo, novel_id, [u(1), u(2), u(1), u(3)], initial=True)
        found = rows(repo, novel_id)
        assert len(found) == 3
        assert [found[u(n)].position for n in (1, 2, 3)] == [1, 2, 3]


class TestApplyToc__Normalisation:
    def test_apply_toc__urls_are_compared_after_normalize_url(
        self, repo: Repo, novel_id: str, clock: FakeClock
    ) -> None:
        sync(repo, novel_id, ["HTTPS://WWW.Example.com/novel/1/", u(2)], initial=True)
        before = rows(repo, novel_id)
        assert set(before) == {u(1), u(2)}
        clock.advance(hours=6)
        diff = sync(repo, novel_id, [u(2) + "/", "https://example.com/novel/1#frag"])
        after = rows(repo, novel_id)
        assert after[u(1)].id == before[u(1)].id
        assert (after[u(2)].position, after[u(1)].position) == (1, 2)
        assert diff.new == []


class TestApplyToc__Events:
    def test_apply_toc__appends_a_ch_event_for_every_inserted_moved_removed_or_returned(
        self, repo: Repo, novel_id: str, clock: FakeClock
    ) -> None:
        sync(repo, novel_id, [u(1), u(2), u(3)], initial=True)
        sync(repo, novel_id, [u(1), u(2)])  # removes 3
        before = len(chapter_events(repo))
        clock.advance(hours=6)
        # 4 inserted at the front, 1 and 2 moved, 3 returned
        diff = sync(repo, novel_id, [u(4), u(1), u(2), u(3)])
        new_events = chapter_events(repo)[before:]
        ids = {e.entity_id for e in new_events}
        rows_now = rows(repo, novel_id)
        assert ids == {rows_now[u(n)].id for n in (1, 2, 3, 4)}
        assert len(new_events) == 4
        assert len(diff.new) == 1 and len(diff.returned) == 1 and len(diff.moved) == 2

    def test_apply_toc__unchanged_toc_appends_no_events_and_reports_nothing(
        self, repo: Repo, novel_id: str, clock: FakeClock
    ) -> None:
        sync(repo, novel_id, [u(1), u(2)], initial=True)
        before = len(chapter_events(repo))
        diff = sync(repo, novel_id, [u(1), u(2)])
        assert len(chapter_events(repo)) == before
        assert diff == TocDiff(new=[], moved=[], removed=[], returned=[])

    def test_apply_toc__ch_events_come_before_the_nv_event(self, repo: Repo, novel_id: str) -> None:
        sync(repo, novel_id, [u(1), u(2)], initial=True)
        with db.read(repo.engine) as conn:
            seqs = conn.execute(select(events.c.entity_id).order_by(events.c.seq)).scalars().all()
        assert seqs[-3:] == [rows(repo, novel_id)[u(1)].id, rows(repo, novel_id)[u(2)].id, novel_id]


def _export(repo: Repo, novel_id: str, export_id: str, first: str, last: str) -> None:
    with db.write(repo.engine) as conn:
        conn.execute(
            insert(exports).values(
                **export_row(
                    id=export_id, novel_id=novel_id, first_chapter_id=first, last_chapter_id=last
                )
            )
        )


def _ex_events(repo: Repo, export_id: str) -> list[Any]:
    with db.read(repo.engine) as conn:
        return list(conn.execute(select(events).where(events.c.entity_id == export_id)).all())


class TestApplyToc__ExEvents:
    def test_apply_toc__emits_ex_for_an_export_whose_end_chapter_was_removed(
        self, repo: Repo, novel_id: str
    ) -> None:
        sync(repo, novel_id, [u(1), u(2), u(3)], initial=True)
        found = rows(repo, novel_id)
        _export(repo, novel_id, "EX000000000001", found[u(2)].id, found[u(3)].id)
        sync(repo, novel_id, [u(1), u(2)])
        (event,) = _ex_events(repo, "EX000000000001")
        assert event.payload["chapter_to"] is None

    def test_apply_toc__emits_ex_for_an_export_whose_range_holds_a_changed_chapter(
        self, repo: Repo, novel_id: str
    ) -> None:
        sync(repo, novel_id, [u(1), u(2), u(3)], initial=True)
        found = rows(repo, novel_id)
        _export(repo, novel_id, "EX000000000001", found[u(1)].id, found[u(3)].id)
        sync(repo, novel_id, [u(1), u(9), u(2), u(3)])
        (event,) = _ex_events(repo, "EX000000000001")
        assert event.payload["progress"]["total"] == 4

    def test_apply_toc__skips_an_export_whose_range_holds_no_changed_chapter(
        self, repo: Repo, novel_id: str
    ) -> None:
        sync(repo, novel_id, [u(1), u(2), u(3)], initial=True)
        found = rows(repo, novel_id)
        _export(repo, novel_id, "EX000000000001", found[u(1)].id, found[u(2)].id)
        sync(repo, novel_id, [u(1), u(2), u(3), u(4)])
        assert _ex_events(repo, "EX000000000001") == []


class TestApplyToc__RemovedOrder:
    def test_apply_toc__removed_chapters_are_reported_by_old_position(
        self, repo: Repo, novel_id: str
    ) -> None:
        sync(repo, novel_id, [u(n) for n in range(1, 6)], initial=True)
        found = rows(repo, novel_id)
        diff = sync(repo, novel_id, [u(3)])
        assert diff.removed == [found[u(n)].id for n in (1, 2, 4, 5)]


class TestApplyToc__RacingADelete:
    """Both writers lock the novel row first, so neither deadlocks nor breaks a foreign key."""

    def test_apply_toc__sync_and_delete_serialise_without_deadlock_or_fk_violation(
        self, repo: Repo, novel_id: str
    ) -> None:
        sync(repo, novel_id, [u(1), u(2)], initial=True)
        has_row = threading.Event()
        outcomes: list[str] = []

        def syncer() -> None:
            try:
                now = repo.clock()
                with db.write(repo.engine) as conn:
                    if not lock_novel(conn, novel_id):
                        outcomes.append("sync:gone")
                        return
                    has_row.set()
                    threading.Event().wait(1.0)  # let delete run into the held row lock
                    diff = apply_toc(
                        conn, novel_id, [u(1), u(3), u(4)], initial=False, followed=True, now=now
                    )
                    event_log.emit_novel_change(
                        conn,
                        novel_id,
                        diff.changed_ids,
                        now=now,
                        static_base_url="/static",
                        released=diff.new,
                    )
                outcomes.append("sync:ok")
            except BaseException as error:  # noqa: BLE001  (reported through the outcomes)
                outcomes.append(f"sync:{error!r}")
            finally:
                has_row.set()

        def deleter() -> None:
            try:
                has_row.wait(timeout=30)
                repo.novels.delete(novel_id)
                outcomes.append("delete:ok")
            except BaseException as error:  # noqa: BLE001  (reported through the outcomes)
                outcomes.append(f"delete:{error!r}")

        threads = [threading.Thread(target=f, daemon=True) for f in (syncer, deleter)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
            assert not t.is_alive()
        assert sorted(outcomes) == ["delete:ok", "sync:ok"]
        with db.read(repo.engine) as conn:
            assert conn.execute(select(chapters)).all() == []
        assert_no_events_after_delete(repo.engine, novel_id)
