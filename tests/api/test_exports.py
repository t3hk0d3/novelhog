"""Tests for the exports routes."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update

from novelhog.repo import Repo, db
from novelhog.repo.chapters import apply_toc
from novelhog.repo.novels import lock_novel
from novelhog.repo.schema import exports
from tests.conftest import FakeClock

BASE = "https://example.org/novel"
UNKNOWN = "NV000000000000"


def _seed(repo: Repo, count: int = 5) -> str:
    repo.hosts.publish(["example.org"])
    novel_id = repo.novels.create(BASE, followed=False).id
    now = repo.clock()
    with db.write(repo.engine) as conn:
        assert lock_novel(conn, novel_id)
        apply_toc(
            conn,
            novel_id,
            [f"{BASE}/{n}" for n in range(1, count + 1)],
            initial=True,
            followed=False,
            now=now,
        )
    return novel_id


def _url(novel_id: str) -> str:
    return f"/api/v1/novels/{novel_id}/exports"


def _assert_error(response: object, status: int, code: str) -> None:
    assert getattr(response, "status_code") == status  # noqa: B009
    assert getattr(response, "json")()["error"]["code"] == code  # noqa: B009


def _range(start: int = 1, end: int = 3) -> dict[str, object]:
    return {"chapters": {"start": start, "end": end}}


class TestCreateExport__WithANewRange:
    def test_create_export__returns_201_export_in_waiting(
        self, client: TestClient, repo: Repo
    ) -> None:
        novel_id = _seed(repo)
        response = client.post(_url(novel_id), json=_range())
        assert response.status_code == 201
        body = response.json()
        assert body["state"] == "waiting"
        assert body["format"] == "epub"
        assert (body["chapter_from"], body["chapter_to"]) == (1, 3)
        assert body["progress"] == {"downloaded": 0, "total": 3}
        assert body["url"] is None


class TestCreateExport__WithAnIdenticalWaitingExport:
    def test_create_export__returns_200_with_the_existing_export(
        self, client: TestClient, repo: Repo
    ) -> None:
        novel_id = _seed(repo)
        first = client.post(_url(novel_id), json=_range())
        second = client.post(_url(novel_id), json=_range())
        assert second.status_code == 200
        assert second.json()["id"] == first.json()["id"]


class TestCreateExport__WithSkipMissingAndNothingDownloaded:
    def test_create_export__returns_422_nothing_to_export(
        self, client: TestClient, repo: Repo
    ) -> None:
        novel_id = _seed(repo)
        response = client.post(_url(novel_id), json={**_range(), "skip_missing": True})
        _assert_error(response, 422, "nothing_to_export")


class TestCreateExport__WithAListSelector:
    def test_create_export__returns_422_invalid_selector(
        self, client: TestClient, repo: Repo
    ) -> None:
        novel_id = _seed(repo)
        response = client.post(_url(novel_id), json={"chapters": [1, 2]})
        _assert_error(response, 422, "invalid_selector")

    def test_create_export__out_of_bounds_range_returns_422_invalid_selector(
        self, client: TestClient, repo: Repo
    ) -> None:
        novel_id = _seed(repo)
        response = client.post(_url(novel_id), json=_range(1, 99))
        _assert_error(response, 422, "invalid_selector")

    def test_create_export__list_selector_on_an_unknown_novel_returns_422_first(
        self, client: TestClient
    ) -> None:
        response = client.post(_url(UNKNOWN), json={"chapters": [1]})
        _assert_error(response, 422, "invalid_selector")


class TestCreateExport__WithAnUnknownFormat:
    def test_create_export__returns_422_unsupported_format(
        self, client: TestClient, repo: Repo
    ) -> None:
        novel_id = _seed(repo)
        response = client.post(_url(novel_id), json={**_range(), "format": "pdf"})
        _assert_error(response, 422, "unsupported_format")


class TestCreateExport__OnAnUnknownNovel:
    def test_create_export__returns_404(self, client: TestClient) -> None:
        _assert_error(client.post(_url(UNKNOWN), json=_range()), 404, "not_found")


class TestListExports:
    def test_list_exports__returns_newest_first_with_progress_blocked_and_url(
        self, client: TestClient, repo: Repo, clock: FakeClock
    ) -> None:
        novel_id = _seed(repo)
        older = client.post(_url(novel_id), json=_range(1, 2)).json()
        clock.advance(seconds=1)
        newer = client.post(_url(novel_id), json=_range(1, 3)).json()
        body = client.get(_url(novel_id)).json()
        assert [e["id"] for e in body] == [newer["id"], older["id"]]
        assert {"progress", "blocked", "url"} <= set(body[0])

    def test_list_exports__unknown_novel_returns_404(self, client: TestClient) -> None:
        _assert_error(client.get(_url(UNKNOWN)), 404, "not_found")


class TestExportUrl:
    def test_list_exports__ready_export_has_a_url(self, client: TestClient, repo: Repo) -> None:
        novel_id = _seed(repo)
        export_id = client.post(_url(novel_id), json=_range()).json()["id"]
        with db.write(repo.engine) as conn:
            conn.execute(
                update(exports)
                .where(exports.c.id == export_id)
                .values(state="ready", storage_key="k.epub", size_bytes=10)
            )
        item = client.get(_url(novel_id)).json()[0]
        assert item["url"] and item["url"].endswith("k.epub")
        assert item["size_bytes"] == 10


class TestDeleteExport:
    def test_delete_export__waiting_returns_204(self, client: TestClient, repo: Repo) -> None:
        novel_id = _seed(repo)
        export_id = client.post(_url(novel_id), json=_range()).json()["id"]
        assert client.delete(f"{_url(novel_id)}/{export_id}").status_code == 204
        assert client.get(_url(novel_id)).json() == []

    def test_delete_export__building_returns_409_export_building(
        self, client: TestClient, repo: Repo
    ) -> None:
        novel_id = _seed(repo)
        export_id = client.post(_url(novel_id), json={**_range(), "skip_missing": False}).json()[
            "id"
        ]
        with db.write(repo.engine) as conn:
            conn.execute(update(exports).where(exports.c.id == export_id).values(state="building"))
        _assert_error(client.delete(f"{_url(novel_id)}/{export_id}"), 409, "export_building")

    @pytest.mark.parametrize(
        "values",
        [
            {"state": "failed", "error": "boom"},
            {"state": "ready", "storage_key": "k.epub", "size_bytes": 10},
        ],
    )
    def test_delete_export__failed_or_ready_returns_204(
        self, client: TestClient, repo: Repo, values: dict[str, object]
    ) -> None:
        novel_id = _seed(repo)
        export_id = client.post(_url(novel_id), json=_range()).json()["id"]
        with db.write(repo.engine) as conn:
            conn.execute(update(exports).where(exports.c.id == export_id).values(**values))
        assert client.delete(f"{_url(novel_id)}/{export_id}").status_code == 204

    def test_delete_export__other_novel_or_unknown_id_returns_404(
        self, client: TestClient, repo: Repo
    ) -> None:
        novel_id = _seed(repo)
        export_id = client.post(_url(novel_id), json=_range()).json()["id"]
        other = repo.novels.create("https://example.org/other", followed=False).id
        _assert_error(client.delete(f"{_url(other)}/{export_id}"), 404, "not_found")
        _assert_error(client.delete(f"{_url(novel_id)}/EX000000000000"), 404, "not_found")
        _assert_error(client.delete(f"{_url(novel_id)}/nonsense"), 404, "not_found")
