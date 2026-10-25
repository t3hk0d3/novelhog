"""Tests for the hosts and novels routes."""

import secrets
from datetime import timedelta

from fastapi.testclient import TestClient

from novelhog.domain.hosts import FailureKind
from novelhog.domain.payloads import format_time
from novelhog.domain.tasks import Failed
from novelhog.repo import Repo
from novelhog.repo.task_kinds.novel_refresh import SyncFailed
from novelhog.repo.tasks import Ready, TaskRow
from tests.conftest import FakeClock

TOC = "https://example.org/novel/1"
UNKNOWN = "NV000000000000"
WORKER = "worker-a"


def _start_refresh(repo: Repo) -> TaskRow:
    """Claim and start the first due refresh task, as the worker would."""
    claimed = repo.tasks.claim(WORKER, 300)
    assert claimed is not None
    started = repo.tasks.start(claimed, WORKER)
    assert isinstance(started, Ready)
    return started.task


def _schedule_next_refresh(repo: Repo, clock: FakeClock, hours: int = 6) -> None:
    """Run the first refresh to a failure that schedules the next one `hours` from now."""
    task = _start_refresh(repo)
    failed = SyncFailed(None, 21600, clock() + timedelta(hours=hours))
    assert repo.tasks.finish(task, WORKER, Failed("SiteError: down", failed)) is not None


def _create(client: TestClient, url: str = TOC, **extra: object) -> dict[str, object]:
    response = client.post("/api/v1/novels", json={"toc_url": url, **extra})
    assert response.status_code == 201
    body: dict[str, object] = response.json()
    return body


class TestGetHosts:
    def test_list_hosts__returns_supported_hosts_only(self, client: TestClient, repo: Repo) -> None:
        repo.hosts.publish(["b.example", "a.example"])
        repo.hosts.publish(["a.example"])
        response = client.get("/api/v1/hosts")
        assert response.status_code == 200
        assert response.json() == [
            {"host": "a.example", "reach_after": None, "failing_since": None}
        ]

    def test_list_hosts__shows_when_a_paused_host_may_be_contacted_again(
        self, client: TestClient, repo: Repo, clock: FakeClock
    ) -> None:
        repo.hosts.publish(["a.example"])
        repo.hosts.failed("a.example", FailureKind.THROTTLED, retry_after=120)

        [host] = client.get("/api/v1/hosts").json()

        assert host == {
            "host": "a.example",
            "reach_after": format_time(clock() + timedelta(seconds=120)),
            "failing_since": format_time(clock()),
        }


class TestListNovels:
    def test_list_novels__orders_by_created_at_desc(
        self, client: TestClient, repo: Repo, clock: FakeClock
    ) -> None:
        repo.hosts.publish(["example.org"])
        first = _create(client, "https://example.org/a")
        clock.advance(seconds=5)
        second = _create(client, "https://example.org/b")
        listed = client.get("/api/v1/novels").json()
        assert [n["id"] for n in listed] == [second["id"], first["id"]]


class TestCreateNovel__WithSupportedUrl:
    def test_create_novel__returns_importing_novel_with_normalised_url(
        self, client: TestClient, repo: Repo
    ) -> None:
        repo.hosts.publish(["example.org"])
        response = client.post(
            "/api/v1/novels", json={"toc_url": "HTTPS://www.Example.org/novel/1/"}
        )
        assert response.status_code == 201
        body = response.json()
        assert body["title"] is None
        assert body["importing"] is True
        assert body["toc_url"] == TOC
        assert body["host"] == "example.org"

    def test_create_novel__followed_defaults_to_true(self, client: TestClient, repo: Repo) -> None:
        repo.hosts.publish(["example.org"])
        assert _create(client)["followed"] is True

    def test_create_novel__honours_followed_false(self, client: TestClient, repo: Repo) -> None:
        repo.hosts.publish(["example.org"])
        assert _create(client, followed=False)["followed"] is False


class TestCreateNovel__OnDuplicate:
    def test_create_novel__returns_409_with_existing_id(
        self, client: TestClient, repo: Repo
    ) -> None:
        repo.hosts.publish(["example.org"])
        existing = _create(client)
        response = client.post(
            "/api/v1/novels", json={"toc_url": "https://www.example.org/novel/1/"}
        )
        assert response.status_code == 409
        error = response.json()["error"]
        assert error["code"] == "novel_exists"
        assert error["details"] == {"novel_id": existing["id"]}


class TestCreateNovel__WithUnsupportedHost:
    def test_create_novel__returns_422_with_hosts(self, client: TestClient, repo: Repo) -> None:
        repo.hosts.publish(["example.org"])
        response = client.post("/api/v1/novels", json={"toc_url": "https://other.test/n"})
        assert response.status_code == 422
        error = response.json()["error"]
        assert error["code"] == "unsupported_host"
        assert error["details"] == {"hosts": ["example.org"]}


class TestCreateNovel__WithInvalidUrl:
    def test_create_novel__rejects_non_http_scheme(self, client: TestClient, repo: Repo) -> None:
        repo.hosts.publish(["example.org"])
        response = client.post("/api/v1/novels", json={"toc_url": "ftp://example.org/n"})
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "invalid_url"

    def test_create_novel__rejects_non_default_port(self, client: TestClient, repo: Repo) -> None:
        repo.hosts.publish(["example.org"])
        response = client.post("/api/v1/novels", json={"toc_url": "https://example.org:8443/n"})
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "invalid_url"


class TestCreateNovel__WithOverLongUrl:
    def test_create_novel__returns_422_invalid_url(self, client: TestClient, repo: Repo) -> None:
        repo.hosts.publish(["example.org"])
        url = "https://example.org/" + secrets.token_hex(1500)
        response = client.post("/api/v1/novels", json={"toc_url": url})
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "invalid_url"


class TestCreateNovel__BeforeHostsPublished:
    def test_create_novel__returns_503(self, client: TestClient) -> None:
        response = client.post("/api/v1/novels", json={"toc_url": TOC})
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "hosts_not_published"


class TestCreateNovel__WithInvalidBody:
    def test_create_novel__missing_toc_url_is_validation_error(self, client: TestClient) -> None:
        response = client.post("/api/v1/novels", json={})
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "validation_error"

    def test_create_novel__extra_keys_are_validation_error(self, client: TestClient) -> None:
        response = client.post("/api/v1/novels", json={"toc_url": TOC, "bogus": 1})
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "validation_error"


class TestGetNovel:
    def test_get_novel__returns_the_novel(self, client: TestClient, repo: Repo) -> None:
        repo.hosts.publish(["example.org"])
        created = _create(client)
        response = client.get(f"/api/v1/novels/{created['id']}")
        assert response.status_code == 200
        assert response.json() == created

    def test_get_novel__unknown_id_returns_404(self, client: TestClient) -> None:
        response = client.get(f"/api/v1/novels/{UNKNOWN}")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"

    def test_get_novel__malformed_id_returns_404(self, client: TestClient) -> None:
        assert client.get("/api/v1/novels/nope").status_code == 404


class TestPatchNovel:
    def test_patch_novel__updates_followed(self, client: TestClient, repo: Repo) -> None:
        repo.hosts.publish(["example.org"])
        novel_id = _create(client)["id"]
        response = client.patch(f"/api/v1/novels/{novel_id}", json={"followed": False})
        assert response.status_code == 200
        assert response.json()["followed"] is False

    def test_patch_novel__renames_trimmed_title(self, client: TestClient, repo: Repo) -> None:
        repo.hosts.publish(["example.org"])
        novel_id = _create(client)["id"]
        response = client.patch(f"/api/v1/novels/{novel_id}", json={"title": "  My Title "})
        assert response.json()["title"] == "My Title"

    def test_patch_novel__seen_stamps_seen_at(
        self, client: TestClient, repo: Repo, clock: FakeClock
    ) -> None:
        repo.hosts.publish(["example.org"])
        novel_id = _create(client)["id"]
        clock.advance(seconds=3)
        response = client.patch(f"/api/v1/novels/{novel_id}", json={"seen": True})
        assert response.status_code == 200
        assert response.json()["updated_at"] == "2026-10-19T12:00:03.000Z"

    def test_patch_novel__empty_title_returns_invalid_title(
        self, client: TestClient, repo: Repo
    ) -> None:
        repo.hosts.publish(["example.org"])
        novel_id = _create(client)["id"]
        response = client.patch(f"/api/v1/novels/{novel_id}", json={"title": "   "})
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "invalid_title"

    def test_patch_novel__seen_false_is_validation_error(
        self, client: TestClient, repo: Repo
    ) -> None:
        repo.hosts.publish(["example.org"])
        novel_id = _create(client)["id"]
        response = client.patch(f"/api/v1/novels/{novel_id}", json={"seen": False})
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "validation_error"

    def test_patch_novel__unknown_id_returns_404(self, client: TestClient) -> None:
        response = client.patch(f"/api/v1/novels/{UNKNOWN}", json={"followed": False})
        assert response.status_code == 404


class TestDeleteNovel:
    def test_delete_novel__returns_204_and_removes_it(self, client: TestClient, repo: Repo) -> None:
        repo.hosts.publish(["example.org"])
        novel_id = _create(client)["id"]
        response = client.delete(f"/api/v1/novels/{novel_id}")
        assert response.status_code == 204
        assert response.content == b""
        assert client.get(f"/api/v1/novels/{novel_id}").status_code == 404

    def test_delete_novel__unknown_id_returns_404(self, client: TestClient) -> None:
        assert client.delete(f"/api/v1/novels/{UNKNOWN}").status_code == 404


class TestSyncNovel:
    def test_sync_novel__returns_202_with_next_refresh_at_now(
        self, client: TestClient, repo: Repo, clock: FakeClock
    ) -> None:
        repo.hosts.publish(["example.org"])
        novel_id = _create(client)["id"]
        _schedule_next_refresh(repo, clock)
        clock.advance(seconds=90)
        response = client.post(f"/api/v1/novels/{novel_id}/sync")
        assert response.status_code == 202
        assert response.json()["next_refresh_at"] == "2026-10-19T12:01:30.000Z"

    def test_sync_novel__unknown_id_returns_404(self, client: TestClient) -> None:
        assert client.post(f"/api/v1/novels/{UNKNOWN}/sync").status_code == 404


class TestNovelDerivedFields:
    def test_get_novels__return_next_refresh_at_and_syncing_of_the_nv_event_during_a_sync(
        self, client: TestClient, repo: Repo, clock: FakeClock
    ) -> None:
        repo.hosts.publish(["example.org"])
        novel_id = str(_create(client)["id"])
        _schedule_next_refresh(repo, clock)
        clock.advance(hours=7)
        _start_refresh(repo)
        client.post(f"/api/v1/novels/{novel_id}/sync")
        event = [e for e in repo.events.since(0) if e.entity_id == novel_id][-1].payload

        one = client.get(f"/api/v1/novels/{novel_id}").json()
        [listed] = client.get("/api/v1/novels").json()

        assert (event["next_refresh_at"], event["syncing"]) == ("2026-10-19T19:00:00.000Z", True)
        for body in (one, listed):
            assert (body["next_refresh_at"], body["syncing"]) == (
                event["next_refresh_at"],
                event["syncing"],
            )
