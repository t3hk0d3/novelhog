"""Tests for the tasks routes (tasks spec section 7)."""

from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from novelhog.domain.tasks import TaskKind, TaskState
from novelhog.repo import Repo, db
from novelhog.repo.novels import lock_novel
from novelhog.repo.tasks import TaskRow

BASE = "https://example.org/novel"
UNKNOWN = "TK000000000000"
FIELDS = {
    "id",
    "kind",
    "entity_id",
    "novel_id",
    "state",
    "attempts",
    "error",
    "run_after",
    "change_seq",
    "created_at",
    "updated_at",
    "started_at",
    "finished_at",
    "duration_ms",
}
WORKER = "worker-a"


def _novel(repo: Repo, n: int = 1) -> str:
    repo.hosts.publish(["example.org"])
    return repo.novels.create(f"{BASE}/{n}", followed=False).id


def _enqueue(
    repo: Repo,
    novel_id: str,
    *,
    kind: TaskKind = TaskKind.CHAPTER_DOWNLOAD,
    entity_id: str | None = None,
    priority: int = 100,
    ordinal: int = 0,
    run_after_s: int = 0,
    **options: Any,
) -> TaskRow:
    queue = repo.tasks._queue  # noqa: SLF001  (tests build tasks with the queue primitives)
    entity = entity_id or f"CH{ordinal + 1:012d}"
    with db.write(repo.engine) as conn:
        assert lock_novel(conn, novel_id)
        task = queue.enqueue(
            conn,
            kind,
            entity,
            novel_id,
            priority=priority,
            ordinal=ordinal,
            run_after=repo.clock() + timedelta(seconds=run_after_s),
            **options,
        )
        queue.stamp(conn)
    return task


def _finish(repo: Repo, task: TaskRow, state: TaskState, error: str | None = None) -> None:
    """Claim, start and finish `task` (it must be the first eligible one)."""
    queue = repo.tasks._queue  # noqa: SLF001
    claimed = queue.claim(WORKER, 300)
    assert claimed is not None and claimed.id == task.id
    with db.write(repo.engine) as conn:
        assert lock_novel(conn, task.novel_id)
        queue.start(conn, claimed, WORKER)
        assert queue.finish(conn, claimed, WORKER, state, error)
        queue.stamp(conn)


def _ids(body: dict[str, Any]) -> list[str]:
    return [item["id"] for item in body["items"]]


def _assert_error(response: object, status: int, code: str) -> None:
    assert getattr(response, "status_code") == status  # noqa: B009
    assert getattr(response, "json")()["error"]["code"] == code  # noqa: B009


class TestListTasks__WithoutFilters:
    def test_list_tasks__returns_claim_order(self, client: TestClient, repo: Repo) -> None:
        novel = _novel(repo)
        low_late = _enqueue(repo, novel, ordinal=0, run_after_s=-10, priority=100)
        low_early_b = _enqueue(repo, novel, ordinal=2, run_after_s=-20, priority=100)
        low_early_a = _enqueue(repo, novel, ordinal=1, run_after_s=-20, priority=100)
        high = _enqueue(repo, novel, ordinal=3, run_after_s=60, priority=300)
        body = client.get("/api/v1/tasks").json()
        assert _ids(body) == [high.id, low_early_a.id, low_early_b.id, low_late.id]

    def test_list_tasks__equal_ordinals_are_ordered_by_id(
        self, client: TestClient, repo: Repo
    ) -> None:
        novel = _novel(repo)
        tasks = [_enqueue(repo, novel, entity_id=f"CH{n:012d}") for n in (3, 1, 2)]
        body = client.get("/api/v1/tasks").json()
        assert _ids(body) == sorted(task.id for task in tasks)

    def test_list_tasks__finished_tasks_keep_their_place_in_claim_order(
        self, client: TestClient, repo: Repo
    ) -> None:
        novel = _novel(repo)
        first = _enqueue(repo, novel, ordinal=0, priority=300)
        second = _enqueue(repo, novel, ordinal=1, priority=100)
        _finish(repo, first, TaskState.DONE)
        assert _ids(client.get("/api/v1/tasks").json()) == [first.id, second.id]

    def test_list_tasks__pages_with_defaults_and_total_counts_all(
        self, client: TestClient, repo: Repo
    ) -> None:
        novel = _novel(repo)
        for n in range(55):
            _enqueue(repo, novel, ordinal=n)
        body = client.get("/api/v1/tasks").json()
        assert (len(body["items"]), body["total"]) == (50, 55)
        page = client.get("/api/v1/tasks", params={"offset": 50, "limit": 10}).json()
        assert (len(page["items"]), page["total"]) == (5, 55)

    def test_list_tasks__next_since_is_the_event_clock_read_before_the_query(
        self, client: TestClient, repo: Repo
    ) -> None:
        novel = _novel(repo)
        _enqueue(repo, novel, emit_event=True)
        body = client.get("/api/v1/tasks").json()
        assert body["next_since"] == repo.events.latest_seq()
        assert body["next_since"] > 0

    def test_list_tasks__response_fields_are_exactly_the_task_object(
        self, client: TestClient, repo: Repo
    ) -> None:
        _enqueue(repo, _novel(repo))
        body = client.get("/api/v1/tasks").json()
        assert set(body) == {"items", "total", "next_since"}
        assert set(body["items"][0]) == FIELDS


class TestListTasks__WithFilters:
    @pytest.fixture
    def seeded(self, repo: Repo) -> dict[str, TaskRow]:
        first, second = _novel(repo, 1), _novel(repo, 2)
        return {
            "download": _enqueue(repo, first, ordinal=0, priority=100),
            "other_novel": _enqueue(repo, second, ordinal=1, priority=100),
            "refresh": _enqueue(
                repo, first, kind=TaskKind.NOVEL_REFRESH, entity_id=first, priority=300
            ),
        }

    def test_list_tasks__novel_id_returns_only_that_novels_tasks(
        self, client: TestClient, seeded: dict[str, TaskRow]
    ) -> None:
        novel_id = seeded["download"].novel_id
        body = client.get("/api/v1/tasks", params={"novel_id": novel_id}).json()
        assert sorted(_ids(body)) == sorted([seeded["download"].id, seeded["refresh"].id])
        assert body["total"] == 2

    def test_list_tasks__entity_id_returns_only_that_entitys_tasks(
        self, client: TestClient, seeded: dict[str, TaskRow]
    ) -> None:
        entity = seeded["other_novel"].entity_id
        body = client.get("/api/v1/tasks", params={"entity_id": entity}).json()
        assert _ids(body) == [seeded["other_novel"].id]

    def test_list_tasks__repeated_kind_means_any_of_them(
        self, client: TestClient, seeded: dict[str, TaskRow]
    ) -> None:
        one = client.get("/api/v1/tasks", params={"kind": "novel_refresh"}).json()
        assert _ids(one) == [seeded["refresh"].id]
        both = client.get(
            "/api/v1/tasks", params=[("kind", "novel_refresh"), ("kind", "chapter_download")]
        ).json()
        assert both["total"] == 3

    def test_list_tasks__repeated_state_means_any_of_them(
        self, client: TestClient, repo: Repo, seeded: dict[str, TaskRow]
    ) -> None:
        _finish(repo, seeded["refresh"], TaskState.DONE)
        done = client.get("/api/v1/tasks", params={"state": "done"}).json()
        assert _ids(done) == [seeded["refresh"].id]
        both = client.get("/api/v1/tasks", params=[("state", "done"), ("state", "queued")]).json()
        assert both["total"] == 3
        assert client.get("/api/v1/tasks", params={"state": "queued"}).json()["total"] == 2

    def test_list_tasks__filters_combine_with_and(
        self, client: TestClient, seeded: dict[str, TaskRow]
    ) -> None:
        body = client.get(
            "/api/v1/tasks",
            params={"novel_id": seeded["download"].novel_id, "kind": "chapter_download"},
        ).json()
        assert _ids(body) == [seeded["download"].id]


class TestListTasks__WithSince:
    def test_list_tasks__returns_every_queued_and_running_task(
        self, client: TestClient, repo: Repo
    ) -> None:
        novel = _novel(repo)
        running = _enqueue(repo, novel, ordinal=0, priority=300)
        queued = _enqueue(repo, novel, ordinal=1)
        queue = repo.tasks._queue  # noqa: SLF001
        claimed = queue.claim(WORKER, 300)
        assert claimed is not None and claimed.id == running.id
        with db.write(repo.engine) as conn:
            assert lock_novel(conn, novel)
            queue.start(conn, claimed, WORKER)
            queue.stamp(conn)
        since = client.get("/api/v1/tasks").json()["next_since"] + 100
        body = client.get("/api/v1/tasks", params={"since": since}).json()
        assert _ids(body) == [running.id, queued.id]

    @pytest.mark.parametrize(
        "state", [TaskState.DONE, TaskState.FAILED, TaskState.SKIPPED, TaskState.CANCELLED]
    )
    def test_list_tasks__returns_tasks_changed_after_since_in_any_state(
        self, client: TestClient, repo: Repo, state: TaskState
    ) -> None:
        novel = _novel(repo)
        task = _enqueue(repo, novel)
        since = client.get("/api/v1/tasks").json()["next_since"]
        if state is TaskState.CANCELLED:
            queue = repo.tasks._queue  # noqa: SLF001
            with db.write(repo.engine) as conn:
                assert lock_novel(conn, novel)
                assert queue.cancel(conn, task.kind, task.entity_id)
                queue.stamp(conn)
        else:
            _finish(repo, task, state, "Timeout: slow" if state is TaskState.FAILED else None)
        body = client.get("/api/v1/tasks", params={"since": since}).json()
        assert [(item["id"], item["state"]) for item in body["items"]] == [(task.id, state.value)]
        assert body["items"][0]["change_seq"] > since

    def test_list_tasks__leaves_out_finished_tasks_unchanged_since(
        self, client: TestClient, repo: Repo
    ) -> None:
        novel = _novel(repo)
        finished = _enqueue(repo, novel, ordinal=0, priority=300)
        live = _enqueue(repo, novel, ordinal=1)
        _finish(repo, finished, TaskState.DONE)
        since = client.get("/api/v1/tasks").json()["next_since"]
        body = client.get("/api/v1/tasks", params={"since": since}).json()
        assert _ids(body) == [live.id]

    def test_list_tasks__combined_with_state_returns_matches_of_both(
        self, client: TestClient, repo: Repo
    ) -> None:
        novel = _novel(repo)
        old = _enqueue(repo, novel, ordinal=0, priority=300)
        _finish(repo, old, TaskState.FAILED, "Timeout: old")
        since = client.get("/api/v1/tasks").json()["next_since"]
        new = _enqueue(repo, novel, ordinal=1, priority=300)
        _enqueue(repo, novel, ordinal=2)
        _finish(repo, new, TaskState.FAILED, "Timeout: new")
        body = client.get("/api/v1/tasks", params={"since": since, "state": "failed"}).json()
        assert _ids(body) == [new.id]

    def test_list_tasks__since_at_or_above_the_clock_returns_only_live_tasks(
        self, client: TestClient, repo: Repo
    ) -> None:
        novel = _novel(repo)
        done = _enqueue(repo, novel, ordinal=0, priority=300)
        live = _enqueue(repo, novel, ordinal=1)
        _finish(repo, done, TaskState.DONE)
        clock = client.get("/api/v1/tasks").json()["next_since"]
        for since in (clock, clock + 1000):
            body = client.get("/api/v1/tasks", params={"since": since}).json()
            assert _ids(body) == [live.id]

    def test_list_tasks__a_change_after_a_response_is_returned_by_the_next_poll(
        self, client: TestClient, repo: Repo
    ) -> None:
        novel = _novel(repo)
        task = _enqueue(repo, novel, priority=300)
        cursor = client.get("/api/v1/tasks").json()["next_since"]
        _finish(repo, task, TaskState.DONE)
        body = client.get("/api/v1/tasks", params={"since": cursor}).json()
        assert _ids(body) == [task.id]
        assert (
            client.get("/api/v1/tasks", params={"since": body["next_since"]}).json()["items"] == []
        )

    def test_list_tasks__returns_every_match_unpaged_in_claim_order(
        self, client: TestClient, repo: Repo
    ) -> None:
        novel = _novel(repo)
        created = [_enqueue(repo, novel, ordinal=n) for n in range(60)]
        body = client.get("/api/v1/tasks", params={"since": 0}).json()
        assert _ids(body) == [task.id for task in created]
        assert body["total"] == len(body["items"]) == 60

    def test_list_tasks__a_live_task_of_a_deleted_novel_is_absent(
        self, client: TestClient, repo: Repo
    ) -> None:
        novel = _novel(repo)
        _enqueue(repo, novel)
        since = client.get("/api/v1/tasks").json()["next_since"]
        with db.write(repo.engine) as conn:
            assert lock_novel(conn, novel)
            repo.tasks._queue.delete_for_novel(conn, novel)  # noqa: SLF001
        body = client.get("/api/v1/tasks", params={"since": since}).json()
        assert body["items"] == []


class TestListTasks__WithInvalidQuery:
    @pytest.mark.parametrize(
        "params",
        [
            {"novel_id": "CH000000000001"},
            {"novel_id": "nonsense"},
            {"entity_id": "TK000000000001"},
            {"entity_id": "CH00"},
            {"kind": "bogus"},
            {"state": "bogus"},
            {"since": "x"},
            {"since": -1},
            {"since": 2**63},
            {"offset": 2**63},
            {"offset": 2**70},
            {"since": 1, "offset": 0},
            {"since": 1, "limit": 10},
            {"offset": -1},
            {"limit": 0},
            {"limit": 501},
            {"limit": "x"},
        ],
    )
    def test_list_tasks__returns_422_validation_error(
        self, client: TestClient, params: dict[str, object]
    ) -> None:
        _assert_error(client.get("/api/v1/tasks", params=params), 422, "validation_error")

    def test_list_tasks__limit_500_is_accepted(self, client: TestClient) -> None:
        assert client.get("/api/v1/tasks", params={"limit": 500}).status_code == 200


class TestListTasks__WithWellFormedIdMatchingNothing:
    def test_list_tasks__returns_an_empty_list(self, client: TestClient) -> None:
        response = client.get(
            "/api/v1/tasks",
            params={"novel_id": "NV000000000009", "entity_id": "CH000000000009"},
        )
        body = response.json()
        assert response.status_code == 200
        assert (body["items"], body["total"]) == ([], 0)


class TestTasksHostCheck:
    @pytest.mark.parametrize("path", ["/api/v1/tasks", f"/api/v1/tasks/{UNKNOWN}"])
    def test_tasks__a_foreign_host_returns_403_host_not_allowed(
        self, client: TestClient, path: str
    ) -> None:
        response = client.get(path, headers={"Host": "evil.example.com"})
        _assert_error(response, 403, "host_not_allowed")


class TestGetTask:
    def test_get_task__returns_the_task(self, client: TestClient, repo: Repo) -> None:
        task = _enqueue(repo, _novel(repo), emit_event=True)
        response = client.get(f"/api/v1/tasks/{task.id}")
        body = response.json()
        assert response.status_code == 200
        assert set(body) == FIELDS
        assert (body["id"], body["state"], body["kind"]) == (task.id, "queued", "chapter_download")
        assert body["change_seq"] > 0

    @pytest.mark.parametrize("task_id", [UNKNOWN, "nonsense", "NV000000000001"])
    def test_get_task__unknown_or_malformed_id_returns_404(
        self, client: TestClient, task_id: str
    ) -> None:
        _assert_error(client.get(f"/api/v1/tasks/{task_id}"), 404, "not_found")

    def test_get_task__a_pruned_task_returns_404(self, client: TestClient, repo: Repo) -> None:
        novel = _novel(repo)
        task = _enqueue(repo, novel)
        _finish(repo, task, TaskState.DONE)
        assert repo.tasks._queue.prune(repo.clock() + timedelta(days=1)) == 1  # noqa: SLF001
        _assert_error(client.get(f"/api/v1/tasks/{task.id}"), 404, "not_found")


class TestListTasks__WithLargestIntegers:
    @pytest.mark.parametrize("name", ["since", "offset"])
    def test_list_tasks__the_signed_64_bit_maximum_is_accepted(
        self, client: TestClient, name: str
    ) -> None:
        assert client.get("/api/v1/tasks", params={name: 2**63 - 1}).status_code == 200
