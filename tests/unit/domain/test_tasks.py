"""Tests for the task domain types."""

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta

import pytest

from novelhog.domain.tasks import (
    FINISHED_STATES,
    LIVE_STATES,
    PRIORITY_BUILD,
    PRIORITY_REFRESH,
    PRIORITY_RELEASE,
    PRIORITY_USER,
    TASK_REPAIR_INTERVAL,
    TASK_RETENTION,
    Done,
    Failed,
    Retry,
    RetryCause,
    Skip,
    Started,
    StartResult,
    TaskKind,
    TaskState,
)

T = datetime(2026, 10, 24, tzinfo=UTC)


class TestTaskKind:
    @pytest.mark.parametrize(
        ("kind", "prefix"),
        [
            (TaskKind.NOVEL_REFRESH, "NV"),
            (TaskKind.CHAPTER_DOWNLOAD, "CH"),
            (TaskKind.EXPORT_BUILD, "EX"),
        ],
    )
    def test_prefix__is_the_entity_id_prefix(self, kind: TaskKind, prefix: str) -> None:
        assert kind.prefix == prefix

    def test_values__are_the_wire_names(self) -> None:
        assert [kind.value for kind in TaskKind] == [
            "novel_refresh",
            "chapter_download",
            "export_build",
        ]


class TestTaskState:
    def test_live_states__are_queued_and_running(self) -> None:
        assert LIVE_STATES == {TaskState.QUEUED, TaskState.RUNNING}

    def test_finished_states__are_the_other_four(self) -> None:
        assert FINISHED_STATES == {
            TaskState.DONE,
            TaskState.FAILED,
            TaskState.SKIPPED,
            TaskState.CANCELLED,
        }
        assert isinstance(FINISHED_STATES, frozenset)
        assert isinstance(LIVE_STATES, frozenset)


class TestConstants:
    def test_priorities_and_timings(self) -> None:
        assert (PRIORITY_REFRESH, PRIORITY_BUILD, PRIORITY_RELEASE, PRIORITY_USER) == (
            300,
            200,
            110,
            100,
        )
        assert TASK_RETENTION == timedelta(days=7)
        assert TASK_REPAIR_INTERVAL == timedelta(hours=1)


class TestStartResult:
    def test_values(self) -> None:
        assert {r.name for r in StartResult} == {"OK", "CANCELLED", "DEFERRED", "LOST"}


class TestOutcome:
    def test_outcomes__compare_by_value(self) -> None:
        assert Done(1) == Done(1)
        assert Failed("boom") == Failed("boom", None)
        assert Retry(T, "e") == Retry(T, "e")
        assert Started() == Started()
        assert Skip("gone") == Skip("gone")
        assert Done(1) != Done(2)

    def test_retry__is_an_infrastructure_retry_by_default(self) -> None:
        assert Retry(T, "e").cause is RetryCause.INFRASTRUCTURE

    def test_retry__compares_its_cause(self) -> None:
        assert Retry(T, "e", RetryCause.TRANSIENT) != Retry(T, "e", RetryCause.HOST_PAUSED)

    @pytest.mark.parametrize(
        "outcome", [Done(1), Failed("boom"), Retry(T, "e"), Started(), Skip("gone")]
    )
    def test_outcomes__are_frozen(self, outcome: object) -> None:
        with pytest.raises(FrozenInstanceError):
            outcome.x = 1  # type: ignore[attr-defined]
