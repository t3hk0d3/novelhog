"""The SSE event generator with a fake repo and a fake clock (spec section 5.7)."""

import asyncio
import json
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, cast

from sqlalchemy.exc import OperationalError

from novelhog.api.routes_events import _guarded, stream
from novelhog.repo import Repo
from novelhog.repo.events import EventRow

NV1 = "NV000000000001"
NV2 = "NV000000000002"
CH1 = "CH000000000001"
EX1 = "EX000000000001"
AT = datetime(2026, 10, 19, tzinfo=UTC)


@dataclass
class FakeEvents:
    rows: list[EventRow] = field(default_factory=list)
    status: Literal["ok", "resync"] = "ok"
    status_calls: list[int] = field(default_factory=list)

    def add(self, novel_id: str, entity_id: str, payload: dict[str, object]) -> None:
        self.rows.append(EventRow(len(self.rows) + 1, novel_id, entity_id, payload, AT))

    def latest_seq(self) -> int:
        return len(self.rows)

    def replay_status(self, seq: int) -> Literal["ok", "resync"]:
        self.status_calls.append(seq)
        return self.status

    def since(self, seq: int, *, novel_id: str | None = None, limit: int = 500) -> list[EventRow]:
        found = [r for r in self.rows if r.seq > seq and novel_id in (None, r.novel_id)]
        return found[:limit]


class Harness:
    """Runs `stream` with a fake clock; `sleep` advances it and runs a scripted step."""

    def __init__(self, events: FakeEvents) -> None:
        self.events = events
        self.now = 0.0
        self.sleeps: list[float] = []
        self.steps: list[Any] = []
        self.stopped = False

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds
        if self.steps:
            self.steps.pop(0)()
        else:
            self.stopped = True

    def run(
        self,
        *,
        last_event_id: str | None = None,
        novel_id: str | None = None,
        keepalive_s: float = 15.0,
    ) -> list[str]:
        repo = cast("Repo", type("R", (), {"events": self.events})())

        async def run(function: Any, *args: Any, **kwargs: Any) -> Any:
            return function(*args, **kwargs)

        async def collect() -> list[str]:
            return [
                frame
                async for frame in stream(
                    repo,
                    last_event_id=last_event_id,
                    novel_id=novel_id,
                    poll_s=1.0,
                    keepalive_s=keepalive_s,
                    should_stop=lambda: self.stopped,
                    sleep=self.sleep,
                    monotonic=lambda: self.now,
                    run=run,
                )
            ]

        return asyncio.run(collect())


def _parse(frame: str) -> dict[str, str]:
    fields = dict(line.split(": ", 1) for line in frame.strip("\n").split("\n"))
    return fields


class TestStream__WithoutLastEventId:
    def test_stream__first_yields_ready_with_latest_seq(self) -> None:
        events = FakeEvents()
        events.add(NV1, NV1, {"id": NV1})
        events.add(NV1, NV1, {"id": NV1})
        frames = Harness(events).run()
        assert _parse(frames[0]) == {"id": "2", "event": "ready", "data": "{}"}

    def test_stream__then_yields_later_events_with_seq_and_kind(self) -> None:
        events = FakeEvents()
        harness = Harness(events)
        harness.steps = [
            lambda: events.add(NV1, CH1, {"id": CH1, "novel_id": NV1}),
            lambda: events.add(NV1, EX1, {"id": EX1}),
        ]
        harness.steps.append(lambda: None)
        frames = harness.run()
        parsed = [_parse(f) for f in frames]
        assert [(p["id"], p["event"]) for p in parsed] == [
            ("0", "ready"),
            ("1", "chapter"),
            ("2", "export"),
        ]


class TestStream__WithReplayableLastEventId:
    def test_stream__replays_later_events_in_order_then_streams_live(self) -> None:
        events = FakeEvents()
        for _ in range(3):
            events.add(NV1, NV1, {"id": NV1})
        harness = Harness(events)
        harness.steps = [lambda: events.add(NV1, NV1, {"id": NV1, "live": True}), lambda: None]
        frames = harness.run(last_event_id="1")
        assert [_parse(f)["id"] for f in frames] == ["2", "3", "4"]
        assert all(_parse(f)["event"] == "novel" for f in frames)
        assert events.status_calls == [1]


class TestStream__WithResyncLastEventId:
    def test_stream__yields_one_resync_with_latest_seq_then_live_events(self) -> None:
        events = FakeEvents(status="resync")
        for _ in range(3):
            events.add(NV1, NV1, {"id": NV1})
        harness = Harness(events)
        harness.steps = [lambda: events.add(NV1, NV1, {"id": NV1}), lambda: None]
        frames = harness.run(last_event_id="1")
        parsed = [_parse(f) for f in frames]
        assert [(p["id"], p["event"]) for p in parsed] == [("3", "resync"), ("4", "novel")]

    def test_stream__non_numeric_id_yields_resync_without_asking_the_log(self) -> None:
        events = FakeEvents()
        events.add(NV1, NV1, {"id": NV1})
        for bad in ("abc", "", "-1", "1.5", " ", "9" * 5000, "1" * 20):
            frames = Harness(events).run(last_event_id=bad)
            assert _parse(frames[0]) == {"id": "1", "event": "resync", "data": "{}"}, bad
        assert events.status_calls == []


class TestStream__WithNovelId:
    def test_stream__only_yields_events_of_that_novel_and_always_ready(self) -> None:
        events = FakeEvents()
        harness = Harness(events)
        harness.steps = [
            lambda: events.add(NV2, NV2, {"id": NV2}),
            lambda: events.add(NV1, CH1, {"id": CH1}),
            lambda: None,
        ]
        parsed = [_parse(f) for f in harness.run(novel_id=NV1)]
        assert [(p["id"], p["event"]) for p in parsed] == [("0", "ready"), ("2", "chapter")]

    def test_stream__resync_is_sent_despite_the_filter(self) -> None:
        events = FakeEvents(status="resync")
        frames = Harness(events).run(last_event_id="5", novel_id=NV1)
        assert _parse(frames[0])["event"] == "resync"


class TestStream__WhenIdle:
    def test_stream__yields_comment_line_at_keepalive_interval(self) -> None:
        events = FakeEvents()
        harness = Harness(events)
        harness.steps = [lambda: None] * 4
        frames = harness.run(keepalive_s=3.0)
        assert frames[1:] == [": keepalive\n\n"]

    def test_stream__event_output_resets_keepalive_timer(self) -> None:
        events = FakeEvents()
        harness = Harness(events)
        harness.steps = [lambda: None, lambda: events.add(NV1, NV1, {"id": NV1}), lambda: None]
        frames = harness.run(keepalive_s=3.0)
        assert [f for f in frames if f.startswith(":")] == []

    def test_stream__stops_when_should_stop_turns_true(self) -> None:
        harness = Harness(FakeEvents())
        harness.steps = [lambda: None] * 100
        harness.steps[1] = lambda: setattr(harness, "stopped", True)
        harness.run()
        assert len(harness.sleeps) == 2


class TestStream__Payloads:
    def test_stream__novel_event_carries_stored_payload(self) -> None:
        events = FakeEvents()
        harness = Harness(events)
        harness.steps = [lambda: None]
        events.add(NV1, NV1, {"id": NV1, "released": [3, 4]})
        events.add(NV2, NV2, {"id": NV2, "deleted": True})
        frames = harness.run(last_event_id="0")
        assert [json.loads(_parse(f)["data"]) for f in frames] == [
            {"id": NV1, "released": [3, 4]},
            {"id": NV2, "deleted": True},
        ]

    def test_stream__chapter_event_carries_chapter_and_novel_id(self) -> None:
        events = FakeEvents()
        events.add(NV1, CH1, {"id": CH1, "novel_id": NV1, "title": "Ünï\ncode"})
        frames = Harness(events).run(last_event_id="0")
        assert len(frames) == 1
        assert json.loads(_parse(frames[0])["data"]) == {
            "id": CH1,
            "novel_id": NV1,
            "title": "Ünï\ncode",
        }


class TestStream__WithFullBatches:
    def test_stream__full_batch_polls_again_without_sleeping_and_keeps_order(self) -> None:
        events = FakeEvents()
        for _ in range(501):
            events.add(NV1, NV1, {"id": NV1})
        harness = Harness(events)
        frames = harness.run(last_event_id="0")
        assert [int(_parse(f)["id"]) for f in frames] == list(range(1, 502))
        assert len(harness.sleeps) == 1


class TestGuarded:
    def test_guarded__database_error_ends_the_stream_cleanly(self) -> None:
        async def failing() -> AsyncGenerator[str]:
            yield "first"
            raise OperationalError("select", {}, Exception("gone"))

        async def collect() -> list[str]:
            return [frame async for frame in _guarded(failing())]

        assert asyncio.run(collect()) == ["first"]
