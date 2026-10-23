"""Tests for the chapters routes."""

import pytest
from fastapi.testclient import TestClient

from novelhog.domain.exports import Format
from novelhog.repo import Repo, db
from novelhog.repo.chapters import apply_toc
from novelhog.repo.novels import lock_novel

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
    return f"/api/v1/novels/{novel_id}/chapters"


def _assert_error(response: object, status: int, code: str) -> dict[str, object]:
    assert getattr(response, "status_code") == status  # noqa: B009
    error: dict[str, object] = getattr(response, "json")()["error"]  # noqa: B009
    assert error["code"] == code
    return error


class TestListChapters:
    def test_list_chapters__returns_items_by_position_and_total(
        self, client: TestClient, repo: Repo
    ) -> None:
        novel_id = _seed(repo)
        body = client.get(_url(novel_id)).json()
        assert body["total"] == 5
        assert [c["position"] for c in body["items"]] == [1, 2, 3, 4, 5]
        assert body["items"][0]["state"] == "not_requested"

    def test_list_chapters__offset_and_limit_page_the_items(
        self, client: TestClient, repo: Repo
    ) -> None:
        novel_id = _seed(repo)
        body = client.get(_url(novel_id), params={"offset": 1, "limit": 2}).json()
        assert [c["position"] for c in body["items"]] == [2, 3]
        assert body["total"] == 5

    def test_list_chapters__state_filter_limits_items_and_total(
        self, client: TestClient, repo: Repo
    ) -> None:
        novel_id = _seed(repo)
        client.patch(_url(novel_id), json={"state": "pending", "chapters": [2, 4]})
        body = client.get(_url(novel_id), params={"state": "pending"}).json()
        assert [c["position"] for c in body["items"]] == [2, 4]
        assert body["total"] == 2

    def test_list_chapters__limit_5000_is_accepted(self, client: TestClient, repo: Repo) -> None:
        novel_id = _seed(repo)
        assert client.get(_url(novel_id), params={"limit": 5000}).status_code == 200

    @pytest.mark.parametrize(
        "params", [{"limit": 5001}, {"offset": -1}, {"state": "bogus"}, {"limit": "x"}]
    )
    def test_list_chapters__invalid_query_returns_422(
        self, client: TestClient, repo: Repo, params: dict[str, object]
    ) -> None:
        novel_id = _seed(repo)
        _assert_error(client.get(_url(novel_id), params=params), 422, "validation_error")

    def test_list_chapters__unknown_novel_returns_404(self, client: TestClient) -> None:
        _assert_error(client.get(_url(UNKNOWN)), 404, "not_found")


class TestPatchChapters__WithListSelector:
    def test_patch_chapters__returns_updated_and_skipped(
        self, client: TestClient, repo: Repo
    ) -> None:
        novel_id = _seed(repo)
        client.patch(_url(novel_id), json={"state": "pending", "chapters": [1]})
        response = client.patch(_url(novel_id), json={"state": "pending", "chapters": [1, 2]})
        assert response.status_code == 200
        assert response.json() == {
            "updated": [2],
            "skipped": [{"position": 1, "state": "pending"}],
        }

    def test_patch_chapters__dequeues_pending_chapters(
        self, client: TestClient, repo: Repo
    ) -> None:
        novel_id = _seed(repo)
        client.patch(_url(novel_id), json={"state": "pending", "chapters": [3]})
        response = client.patch(_url(novel_id), json={"state": "not_requested", "chapters": [3]})
        assert response.json() == {"updated": [3], "skipped": []}


class TestPatchChapters__WithRangeSelector:
    def test_patch_chapters__accepts_start_and_end(self, client: TestClient, repo: Repo) -> None:
        novel_id = _seed(repo)
        response = client.patch(
            _url(novel_id), json={"state": "pending", "chapters": {"start": 2, "end": 4}}
        )
        assert response.json() == {"updated": [2, 3, 4], "skipped": []}

    def test_patch_chapters__extra_range_key_returns_422(
        self, client: TestClient, repo: Repo
    ) -> None:
        novel_id = _seed(repo)
        response = client.patch(
            _url(novel_id), json={"state": "pending", "chapters": {"start": 1, "end": 2, "x": 3}}
        )
        _assert_error(response, 422, "validation_error")

    def test_patch_chapters__extra_body_key_returns_422(
        self, client: TestClient, repo: Repo
    ) -> None:
        novel_id = _seed(repo)
        response = client.patch(
            _url(novel_id), json={"state": "pending", "chapters": [1], "extra": 1}
        )
        _assert_error(response, 422, "validation_error")


class TestPatchChapters__CancellingANeededChapter:
    def test_patch_chapters__fails_the_waiting_export(self, client: TestClient, repo: Repo) -> None:
        novel_id = _seed(repo)
        client.patch(_url(novel_id), json={"state": "pending", "chapters": [2]})
        export_id = repo.exports.create(novel_id, 1, 3, Format.EPUB)[0].id
        response = client.patch(_url(novel_id), json={"state": "not_requested", "chapters": [2]})
        assert response.status_code == 200
        exports = client.get(f"/api/v1/novels/{novel_id}/exports").json()
        failed = next(e for e in exports if e["id"] == export_id)
        assert (failed["state"], failed["error"]) == ("failed", "Chapter 2 was cancelled")


class TestPatchChapters__WithTargetState:
    @pytest.mark.parametrize("state", ["downloading", "downloaded", "error"])
    def test_patch_chapters__worker_state_returns_invalid_target_state(
        self, client: TestClient, repo: Repo, state: str
    ) -> None:
        novel_id = _seed(repo)
        response = client.patch(_url(novel_id), json={"state": state, "chapters": [1]})
        _assert_error(response, 422, "invalid_target_state")

    def test_patch_chapters__unknown_state_returns_validation_error(
        self, client: TestClient, repo: Repo
    ) -> None:
        novel_id = _seed(repo)
        response = client.patch(_url(novel_id), json={"state": "bogus", "chapters": [1]})
        _assert_error(response, 422, "validation_error")


class TestPatchChapters__WithMalformedSelector:
    @pytest.mark.parametrize(
        "selector",
        [{}, {"start": 1}, {"start": 1, "end": 2, "chapters": [1]}, "1-2", None, [1, "a"]],
    )
    def test_patch_chapters__returns_validation_error(
        self, client: TestClient, repo: Repo, selector: object
    ) -> None:
        novel_id = _seed(repo)
        response = client.patch(_url(novel_id), json={"state": "pending", "chapters": selector})
        _assert_error(response, 422, "validation_error")

    def test_patch_chapters__missing_selector_returns_validation_error(
        self, client: TestClient, repo: Repo
    ) -> None:
        novel_id = _seed(repo)
        _assert_error(
            client.patch(_url(novel_id), json={"state": "pending"}), 422, "validation_error"
        )


class TestPatchChapters__OutOfRange:
    def test_patch_chapters__list_returns_invalid_selector_with_positions(
        self, client: TestClient, repo: Repo
    ) -> None:
        novel_id = _seed(repo)
        response = client.patch(_url(novel_id), json={"state": "pending", "chapters": [0, 3, 9]})
        error = _assert_error(response, 422, "invalid_selector")
        assert error["details"] == {"out_of_range": [0, 9]}

    def test_patch_chapters__range_returns_invalid_selector(
        self, client: TestClient, repo: Repo
    ) -> None:
        novel_id = _seed(repo)
        response = client.patch(
            _url(novel_id), json={"state": "pending", "chapters": {"start": 4, "end": 6}}
        )
        error = _assert_error(response, 422, "invalid_selector")
        assert error["details"] == {"out_of_range": [6]}


class TestPatchChapters__OnUnknownNovel:
    def test_patch_chapters__returns_404(self, client: TestClient) -> None:
        response = client.patch(_url(UNKNOWN), json={"state": "pending", "chapters": [1]})
        _assert_error(response, 404, "not_found")
