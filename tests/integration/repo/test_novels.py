"""The novels repository (spec sections 4, 5.2 and 5.3)."""

import threading
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import insert, select, update

from novelhog.domain.errors import Conflict, NotFound, ServiceUnavailable, ValidationFailed
from novelhog.repo import Repo, db
from novelhog.repo import events as event_log
from novelhog.repo.schema import (
    chapter_contents,
    chapters,
    events,
    exports,
    novels,
    storage_trash,
)
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
    with db.read(repo.engine) as conn:
        return list(conn.execute(select(events).order_by(events.c.seq)).all())


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
        assert row.next_refresh_at == clock.current
        assert row.created_at == clock.current

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
                {"state": "pending", "queued_at": released},
                {
                    "state": "downloading",
                    "queued_at": released,
                    "claimed_by": "w",
                    "claimed_until": released,
                },
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

    def test_get__syncing_only_while_claimed_by_a_live_claim(
        self, published: Repo, clock: FakeClock
    ) -> None:
        novel = published.novels.create(URL, followed=True)
        until = clock.current + timedelta(minutes=5)
        with db.write(published.engine) as conn:
            conn.execute(update(novels).values(claimed_by="w", claimed_until=until))
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
    def test_request_sync__sets_next_refresh_and_flag_and_appends_an_event(
        self, published: Repo, clock: FakeClock
    ) -> None:
        novel = published.novels.create(URL, followed=True)
        clock.advance(hours=2)
        with db.write(published.engine) as conn:
            conn.execute(update(novels).values(next_refresh_at=clock.current + timedelta(days=1)))
        view = published.novels.request_sync(novel.id)
        row = _row(published, novel.id)
        assert (row.next_refresh_at, row.sync_requested) == (clock.current, True)
        assert view.next_refresh_at == clock.current
        assert len(_events(published)) == 2

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

    def test_delete__removes_novel_chapters_contents_and_exports(
        self, published: Repo, populated: str
    ) -> None:
        published.novels.delete(populated)
        with db.read(published.engine) as conn:
            for table in (novels, chapters, chapter_contents, exports):
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
