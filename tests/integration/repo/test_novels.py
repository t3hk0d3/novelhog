"""The novels repository (spec sections 4, 5.2 and 5.3)."""

import threading
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import Connection, insert, select, update

from novelhog.domain.errors import Conflict, NotFound, ServiceUnavailable, ValidationFailed
from novelhog.domain.tasks import PRIORITY_REFRESH, TaskKind, TaskState
from novelhog.repo import Repo, db
from novelhog.repo import events as event_log
from novelhog.repo.novels import lock_novel
from novelhog.repo.schema import (
    chapter_contents,
    chapters,
    events,
    exports,
    novels,
    storage_trash,
    tasks,
)
from novelhog.repo.tasks import Ready, TaskRow
from tests.conftest import FakeClock
from tests.integration.helpers import chapter_row, export_row

URL = "https://example.com/novel"
MIN_INTERVAL = 21600


@pytest.fixture
def published(repo: Repo) -> Repo:
    """The repo with `example.com` published."""
    repo.hosts.publish(["example.com"])
    return repo


def _events(repo: Repo) -> list[Any]:
    """The entity events (`NV`, `CH`, `EX`) in seq order; `TK` events are left out."""
    with db.read(repo.engine) as conn:
        statement = select(events).where(events.c.entity_id.not_like("TK%"))
        return list(conn.execute(statement.order_by(events.c.seq)).all())


def _task_events(repo: Repo) -> list[Any]:
    with db.read(repo.engine) as conn:
        statement = select(events).where(events.c.entity_id.like("TK%"))
        return list(conn.execute(statement.order_by(events.c.seq)).all())


def _refresh_tasks(repo: Repo, novel_id: str) -> list[TaskRow]:
    """The novel's refresh tasks in `id` order."""
    with db.read(repo.engine) as conn:
        statement = select(tasks).where(
            tasks.c.kind == TaskKind.NOVEL_REFRESH.value, tasks.c.entity_id == novel_id
        )
        return [TaskRow.from_row(row) for row in conn.execute(statement.order_by(tasks.c.id))]


def _start_refresh(repo: Repo, worker: str = "w", ttl: int = 300) -> TaskRow:
    """Claim and start the due refresh task through the public task path."""
    claimed = repo.tasks.claim(worker, ttl)
    assert claimed is not None
    started = repo.tasks.start(claimed, worker)
    assert isinstance(started, Ready)
    return started.task


def _row(repo: Repo, novel_id: str) -> Any:
    with db.read(repo.engine) as conn:
        return conn.execute(select(novels).where(novels.c.id == novel_id)).one()


def _add_chapters(repo: Repo, novel_id: str, specs: list[dict[str, Any]]) -> None:
    with db.write(repo.engine) as conn:
        for i, spec in enumerate(specs, start=1):
            row = (
                chapter_row(id=f"CH{i:012d}", novel_id=novel_id, url=f"{URL}/{i}", position=i)
                | spec
            )
            conn.execute(insert(chapters).values(**row))


class TestCreate__OnSupportedHost:
    def test_create__inserts_normalised_novel_with_defaults(
        self, published: Repo, clock: FakeClock
    ) -> None:
        view = published.novels.create("HTTPS://WWW.Example.com/novel/", followed=False)
        row = _row(published, view.id)
        assert row.toc_url == URL
        assert row.title is None
        assert row.host == "example.com"
        assert row.followed is False
        assert row.refresh_interval == MIN_INTERVAL
        assert view.next_refresh_at == clock.current
        assert row.created_at == clock.current

    def test_create__inserts_a_queued_refresh_task_due_now_that_emits_events(
        self, published: Repo, clock: FakeClock
    ) -> None:
        view = published.novels.create(URL, followed=True)

        [task] = _refresh_tasks(published, view.id)
        assert (task.state, task.run_after, task.priority, task.emit_event, task.novel_id) == (
            TaskState.QUEUED,
            clock.current,
            PRIORITY_REFRESH,
            True,
            view.id,
        )

    def test_create__appends_a_task_event_after_the_novel_event(self, published: Repo) -> None:
        view = published.novels.create(URL, followed=True)

        [task] = _refresh_tasks(published, view.id)
        [nv] = _events(published)
        [tk] = _task_events(published)
        assert (tk.entity_id, tk.payload) == (task.id, task.to_view().to_json())
        assert tk.seq > nv.seq

    def test_create__appends_one_novel_event_with_the_payload(self, published: Repo) -> None:
        view = published.novels.create(URL, followed=True)
        (event,) = _events(published)
        assert event.entity_id == view.id
        assert event.novel_id == view.id
        assert event.payload == view.to_json()

    def test_create__returns_importing_not_syncing_with_zero_counters(
        self, published: Repo
    ) -> None:
        view = published.novels.create(URL, followed=True)
        assert (view.importing, view.syncing) == (True, False)
        assert (
            view.total_pages,
            view.downloaded_pages,
            view.pending_pages,
            view.error_pages,
            view.new_pages,
        ) == (0, 0, 0, 0, 0)
        assert view.cover_url is None


class TestCreate__OnExistingUrl:
    def test_create__raises_novel_exists_with_the_id(self, published: Repo) -> None:
        first = published.novels.create(URL, followed=True)
        with pytest.raises(Conflict) as caught:
            published.novels.create("https://www.EXAMPLE.com/novel/", followed=True)
        assert caught.value.code == "novel_exists"
        assert caught.value.details == {"novel_id": first.id}


class TestCreate__OnUnsupportedHost:
    def test_create__raises_unsupported_host_listing_supported_hosts(self, published: Repo) -> None:
        with pytest.raises(ValidationFailed) as caught:
            published.novels.create("https://other.org/novel", followed=True)
        assert caught.value.code == "unsupported_host"
        assert caught.value.details == {"hosts": ["example.com"]}

    def test_create__rejects_a_host_that_was_dropped(self, published: Repo) -> None:
        published.hosts.publish(["other.com"])
        with pytest.raises(ValidationFailed) as caught:
            published.novels.create(URL, followed=True)
        assert caught.value.code == "unsupported_host"


class TestCreate__OnInvalidUrl:
    @pytest.mark.parametrize(
        "url", ["ftp://example.com/novel", "https://example.com:8443/novel", "not a url"]
    )
    def test_create__raises_invalid_url(self, published: Repo, url: str) -> None:
        with pytest.raises(ValidationFailed) as caught:
            published.novels.create(url, followed=True)
        assert caught.value.code == "invalid_url"


class TestCreate__BeforeAnyHostWasPublished:
    def test_create__raises_hosts_not_published(self, repo: Repo) -> None:
        with pytest.raises(ServiceUnavailable) as caught:
            repo.novels.create(URL, followed=True)
        assert caught.value.code == "hosts_not_published"


class TestGetAndList:
    def test_get__counts_chapters_with_a_position(self, published: Repo, clock: FakeClock) -> None:
        novel = published.novels.create(URL, followed=True)
        released = clock.current
        _add_chapters(
            published,
            novel.id,
            [
                {"state": "downloaded"},
                {"state": "pending"},
                {"state": "downloading"},
                {"state": "error"},
                {"state": "not_requested"},
                {"state": "downloaded", "position": None, "removed_at": released},
            ],
        )
        view = published.novels.get(novel.id)
        assert (
            view.total_pages,
            view.downloaded_pages,
            view.pending_pages,
            view.error_pages,
        ) == (5, 1, 2, 1)

    def test_get__new_pages_counts_released_after_seen_or_all_released_when_unseen(
        self, published: Repo, clock: FakeClock
    ) -> None:
        novel = published.novels.create(URL, followed=True)
        t0 = clock.current
        _add_chapters(
            published,
            novel.id,
            [
                {"released_at": None},
                {"released_at": t0 - timedelta(hours=2)},
                {"released_at": t0 + timedelta(hours=1)},
            ],
        )
        assert published.novels.get(novel.id).new_pages == 2
        with db.write(published.engine) as conn:
            conn.execute(update(novels).values(seen_at=t0 - timedelta(hours=1)))
        assert published.novels.get(novel.id).new_pages == 1

    def test_get__syncing_only_while_the_running_refresh_holds_its_claim(
        self, published: Repo, clock: FakeClock
    ) -> None:
        novel = published.novels.create(URL, followed=True)
        _start_refresh(published, ttl=300)
        assert published.novels.get(novel.id).syncing is True
        clock.advance(minutes=6)
        assert published.novels.get(novel.id).syncing is False

    def test_get__importing_until_refreshed_or_failed(
        self, published: Repo, clock: FakeClock
    ) -> None:
        novel = published.novels.create(URL, followed=True)
        assert published.novels.get(novel.id).importing is True
        with db.write(published.engine) as conn:
            conn.execute(update(novels).values(last_refreshed_at=clock.current))
        assert published.novels.get(novel.id).importing is False
        with db.write(published.engine) as conn:
            conn.execute(update(novels).values(last_refreshed_at=None, import_error="boom"))
        view = published.novels.get(novel.id)
        assert (view.importing, view.import_error) == (False, "boom")

    def test_get__cover_url_is_the_static_url_or_none(self, published: Repo) -> None:
        novel = published.novels.create(URL, followed=True)
        assert published.novels.get(novel.id).cover_url is None
        with db.write(published.engine) as conn:
            conn.execute(update(novels).values(cover_key="covers/a b.jpg"))
        assert published.novels.get(novel.id).cover_url == "/static/covers/a%20b.jpg"

    def test_list__orders_by_created_at_descending(self, published: Repo, clock: FakeClock) -> None:
        a = published.novels.create(URL + "/a", followed=True)
        clock.advance(seconds=1)
        b = published.novels.create(URL + "/b", followed=True)
        clock.advance(seconds=1)
        c = published.novels.create(URL + "/c", followed=True)
        assert [n.id for n in published.novels.list()] == [c.id, b.id, a.id]

    @pytest.mark.parametrize("novel_id", ["NV000000000099", "garbage", "CH000000000001", "NV\x00"])
    def test_get__unknown_or_malformed_id_raises_not_found(
        self, published: Repo, novel_id: str
    ) -> None:
        with pytest.raises(NotFound):
            published.novels.get(novel_id)


class TestUpdate:
    def test_update__followed_changes_only_the_flag_and_appends_an_event(
        self, published: Repo, clock: FakeClock
    ) -> None:
        novel = published.novels.create(URL, followed=True)
        clock.advance(seconds=5)
        view = published.novels.update(novel.id, followed=False)
        assert view.followed is False
        assert view.next_refresh_at == novel.next_refresh_at
        assert view.title is None
        events_ = _events(published)
        assert len(events_) == 2
        assert events_[-1].payload == view.to_json()

    def test_update__seen_sets_seen_at_and_zeroes_new_pages(
        self, published: Repo, clock: FakeClock
    ) -> None:
        novel = published.novels.create(URL, followed=True)
        _add_chapters(published, novel.id, [{"released_at": clock.current}])
        assert published.novels.get(novel.id).new_pages == 1
        clock.advance(seconds=5)
        view = published.novels.update(novel.id, seen=True)
        assert _row(published, novel.id).seen_at == clock.current
        assert view.new_pages == 0
        assert len(_events(published)) == 2

    def test_update__title_is_trimmed_and_cleaned_and_appends_an_event(
        self, published: Repo
    ) -> None:
        novel = published.novels.create(URL, followed=True)
        view = published.novels.update(novel.id, title="  My\x00 Novel \n")
        assert view.title == "My Novel"
        assert _row(published, novel.id).title == "My Novel"
        assert len(_events(published)) == 2

    @pytest.mark.parametrize("title", ["", "   ", "\x00\x01"])
    def test_update__empty_title_raises_invalid_title(self, published: Repo, title: str) -> None:
        novel = published.novels.create(URL, followed=True)
        with pytest.raises(ValidationFailed) as caught:
            published.novels.update(novel.id, title=title)
        assert caught.value.code == "invalid_title"
        assert len(_events(published)) == 1

    def test_update__unknown_id_raises_not_found(self, published: Repo) -> None:
        with pytest.raises(NotFound):
            published.novels.update("NV000000000099", followed=False)

    @pytest.mark.parametrize("novel_id", ["garbage", "NV\x00"])
    def test_update__malformed_id_raises_not_found(self, published: Repo, novel_id: str) -> None:
        with pytest.raises(NotFound):
            published.novels.update(novel_id, followed=False)

    def test_update__unknown_id_wins_over_an_invalid_title(self, published: Repo) -> None:
        with pytest.raises(NotFound):
            published.novels.update("NV000000000099", title=" ")


class TestRequestSync:
    @pytest.fixture
    def idle(self, published: Repo, clock: FakeClock) -> str:
        """A novel whose next refresh is a day away."""
        novel = published.novels.create(URL, followed=True)
        with db.write(published.engine) as conn:
            conn.execute(update(tasks).values(run_after=clock.current + timedelta(days=1)))
        clock.advance(hours=2)
        return novel.id

    def test_request_sync__makes_the_queued_refresh_due_now_and_appends_an_event(
        self, published: Repo, idle: str, clock: FakeClock
    ) -> None:
        view = published.novels.request_sync(idle)

        [task] = _refresh_tasks(published, idle)
        assert (task.state, task.run_after) == (TaskState.QUEUED, clock.current)
        assert view.next_refresh_at == clock.current
        assert _events(published)[-1].payload == view.to_json()
        assert len(_events(published)) == 2

    def test_request_sync__appends_a_task_event_for_the_merged_task(
        self, published: Repo, idle: str
    ) -> None:
        published.novels.request_sync(idle)

        [task] = _refresh_tasks(published, idle)
        assert _task_events(published)[-1].payload == task.to_view().to_json()

    def test_request_sync__during_a_sync_queues_one_successor_due_now(
        self, published: Repo, clock: FakeClock
    ) -> None:
        novel = published.novels.create(URL, followed=True)
        running = _start_refresh(published)
        clock.advance(minutes=1)

        view = published.novels.request_sync(novel.id)

        tasks_ = _refresh_tasks(published, novel.id)
        assert sorted((t.state, t.run_after) for t in tasks_) == sorted(
            [(TaskState.RUNNING, running.run_after), (TaskState.QUEUED, clock.current)]
        )
        assert (view.next_refresh_at, view.syncing) == (clock.current, True)

    @pytest.mark.parametrize("novel_id", ["NV000000000099", "garbage", "NV\x00"])
    def test_request_sync__unknown_or_malformed_id_raises_not_found(
        self, published: Repo, novel_id: str
    ) -> None:
        with pytest.raises(NotFound):
            published.novels.request_sync(novel_id)


class TestDelete:
    @pytest.fixture
    def populated(self, published: Repo, clock: FakeClock) -> str:
        novel = published.novels.create(URL, followed=True)
        _add_chapters(published, novel.id, [{"state": "downloaded"}, {"state": "downloaded"}])
        with db.write(published.engine) as conn:
            conn.execute(update(novels).values(cover_key="covers/a.jpg"))
            for chapter_id in ("CH000000000001", "CH000000000002"):
                conn.execute(
                    insert(chapter_contents).values(
                        chapter_id=chapter_id, markdown="text", fetched_at=clock.current
                    )
                )
            conn.execute(
                insert(exports).values(
                    **export_row(
                        id="EX000000000001",
                        novel_id=novel.id,
                        state="ready",
                        storage_key="exports/one.epub",
                        size_bytes=3,
                        ready_at=clock.current,
                        first_chapter_id="CH000000000001",
                        last_chapter_id="CH000000000002",
                    )
                )
            )
            conn.execute(
                insert(exports).values(
                    **export_row(
                        id="EX000000000002",
                        novel_id=novel.id,
                        state="waiting",
                        first_chapter_id="CH000000000001",
                        last_chapter_id="CH000000000002",
                    )
                )
            )
        return novel.id

    def test_delete__removes_novel_chapters_contents_exports_and_tasks(
        self, published: Repo, populated: str
    ) -> None:
        _start_refresh(published)
        published.novels.request_sync(populated)

        published.novels.delete(populated)

        with db.read(published.engine) as conn:
            for table in (novels, chapters, chapter_contents, exports, tasks):
                assert conn.execute(select(table)).all() == []

    def test_delete__trashes_cover_and_ready_export_keys(
        self, published: Repo, populated: str
    ) -> None:
        published.novels.delete(populated)
        with db.read(published.engine) as conn:
            keys = {r.key for r in conn.execute(select(storage_trash)).all()}
        assert keys == {"covers/a.jpg", "exports/one.epub"}

    def test_delete__appends_one_deleted_event(self, published: Repo, populated: str) -> None:
        before = len(_events(published))
        published.novels.delete(populated)
        events_ = _events(published)
        assert len(events_) == before + 1
        assert events_[-1].payload == {"id": populated, "deleted": True}
        assert events_[-1].entity_id == populated

    @pytest.mark.parametrize("novel_id", ["NV000000000099", "garbage", "NV\x00"])
    def test_delete__unknown_or_malformed_id_raises_not_found(
        self, published: Repo, novel_id: str
    ) -> None:
        with pytest.raises(NotFound):
            published.novels.delete(novel_id)

    def test_delete__tolerates_a_key_already_in_the_trash(
        self, published: Repo, populated: str, clock: FakeClock
    ) -> None:
        with db.write(published.engine) as conn:
            conn.execute(insert(storage_trash).values(key="covers/a.jpg", at=clock.current))
        published.novels.delete(populated)
        with db.read(published.engine) as conn:
            keys = {r.key for r in conn.execute(select(storage_trash)).all()}
        assert keys == {"covers/a.jpg", "exports/one.epub"}

    def test_delete__concurrent_deletes_succeed_once_and_raise_not_found_once(
        self, published: Repo, populated: str
    ) -> None:
        barrier = threading.Barrier(2)
        outcomes: list[str] = []

        def racer() -> None:
            barrier.wait(timeout=30)
            try:
                published.novels.delete(populated)
                outcomes.append("deleted")
            except NotFound:
                outcomes.append("not_found")

        threads = [threading.Thread(target=racer, daemon=True) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
            assert not t.is_alive()
        assert sorted(outcomes) == ["deleted", "not_found"]
        deleted = [e for e in _events(published) if e.payload.get("deleted")]
        assert len(deleted) == 1

    def test_delete__does_not_deadlock_with_a_concurrent_event_writer(
        self, published: Repo, populated: str, clock: FakeClock
    ) -> None:
        """An event writer that locked the novel row first must not deadlock with `delete`."""
        has_row = threading.Event()
        errors: list[BaseException] = []

        def writer() -> None:
            try:
                with db.write(published.engine) as conn:
                    conn.execute(
                        update(novels).where(novels.c.id == populated).values(title="renamed")
                    )
                    has_row.set()
                    threading.Event().wait(1.0)  # let delete run into the held row lock
                    event_log.append(conn, populated, populated, {"id": populated}, now=clock())
            except BaseException as error:  # noqa: BLE001  (checked in the main thread)
                errors.append(error)

        def deleter() -> None:
            try:
                has_row.wait(timeout=30)
                published.novels.delete(populated)
            except BaseException as error:  # noqa: BLE001  (checked in the main thread)
                errors.append(error)

        threads = [threading.Thread(target=f, daemon=True) for f in (writer, deleter)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
            assert not t.is_alive()
        assert errors == []
        with db.read(published.engine) as conn:
            assert conn.execute(select(novels)).all() == []


class TestDelete__AgainstWritersOfTheNovelsRows:
    """Writers lock the novel row first (spec section 4, "Lock order"), so delete serialises."""

    @pytest.fixture
    def novel_id(self, published: Repo) -> str:
        novel = published.novels.create(URL, followed=True)
        _add_chapters(published, novel.id, [{}, {}])
        return novel.id

    def _race(self, repo: Repo, novel_id: str, write: Callable[[Connection], None]) -> list[str]:
        """Run `write` in a transaction that holds the novel row while `delete` runs into it."""
        has_row = threading.Event()
        outcomes: list[str] = []

        def writer() -> None:
            try:
                with db.write(repo.engine) as conn:
                    if not lock_novel(conn, novel_id):
                        outcomes.append("writer:gone")
                        return
                    has_row.set()
                    threading.Event().wait(1.0)  # let delete run into the held row lock
                    write(conn)
                    event_log.append(conn, novel_id, novel_id, {"id": novel_id}, now=repo.clock())
                outcomes.append("writer:ok")
            except BaseException as error:  # noqa: BLE001  (reported through the outcomes)
                outcomes.append(f"writer:{error!r}")
            finally:
                has_row.set()

        def deleter() -> None:
            try:
                has_row.wait(timeout=30)
                repo.novels.delete(novel_id)
                outcomes.append("delete:ok")
            except NotFound:
                outcomes.append("delete:not_found")
            except BaseException as error:  # noqa: BLE001  (reported through the outcomes)
                outcomes.append(f"delete:{error!r}")

        threads = [threading.Thread(target=f, daemon=True) for f in (writer, deleter)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
            assert not t.is_alive()
        return outcomes

    def _assert_one_winner(self, repo: Repo, outcomes: list[str]) -> None:
        assert sorted(outcomes) in (["delete:ok", "writer:ok"], ["delete:ok", "writer:gone"])
        with db.read(repo.engine) as conn:
            for table in (novels, chapters, chapter_contents):
                assert conn.execute(select(table)).all() == []

    def test_delete__sync_shaped_writer_does_not_deadlock(
        self, published: Repo, novel_id: str, clock: FakeClock
    ) -> None:
        def write(conn: Connection) -> None:
            conn.execute(
                insert(chapters).values(
                    **chapter_row(
                        id="CH000000000003", novel_id=novel_id, url=URL + "/3", position=3
                    )
                )
            )
            conn.execute(
                update(chapters).where(chapters.c.id == "CH000000000002").values(title="t")
            )

        self._assert_one_winner(published, self._race(published, novel_id, write))

    def test_delete__download_shaped_writer_does_not_hit_a_foreign_key_violation(
        self, published: Repo, novel_id: str, clock: FakeClock
    ) -> None:
        def write(conn: Connection) -> None:
            conn.execute(
                update(chapters).where(chapters.c.id == "CH000000000001").values(title="t")
            )
            conn.execute(
                insert(chapter_contents).values(
                    chapter_id="CH000000000001", markdown="text", fetched_at=clock.current
                )
            )

        self._assert_one_winner(published, self._race(published, novel_id, write))

    def test_lock_novel__reports_a_deleted_novel(self, published: Repo, novel_id: str) -> None:
        with db.write(published.engine) as conn:
            assert lock_novel(conn, novel_id) is True
        published.novels.delete(novel_id)
        with db.write(published.engine) as conn:
            assert lock_novel(conn, novel_id) is False
