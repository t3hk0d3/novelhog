"""Server-Sent Events stream (spec section 5.7).

The API keeps no per-client state: a client resumes with `Last-Event-ID`, and the stream polls the
event log. `stream` is a plain synchronous generator; the route runs it in the threadpool, one
`next()` per call, so a database poll never blocks the event loop and no connection is held while
the generator sleeps.
"""

import json
import threading
import time
from collections.abc import AsyncIterator, Callable, Iterator
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from starlette.concurrency import iterate_in_threadpool

from novelhog import ids
from novelhog.api.deps import get_repo
from novelhog.api.schemas import ErrorBody
from novelhog.domain.errors import NotFound
from novelhog.repo import Repo

router = APIRouter()

RepoDep = Annotated[Repo, Depends(get_repo)]

BATCH = 500
KEEPALIVE = ": keepalive\n\n"


def _frame(seq: int, name: str, data: dict[str, object]) -> str:
    return f"id: {seq}\nevent: {name}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"


def _parse_seq(value: str) -> int | None:
    """Return the sequence number in a `Last-Event-ID`, or None if it is not a plain number."""
    text = value.strip()
    return int(text) if text.isascii() and text.isdigit() else None


def stream(
    repo: Repo,
    *,
    last_event_id: str | None,
    novel_id: str | None,
    poll_s: float,
    keepalive_s: float,
    should_stop: Callable[[], bool],
    sleep: Callable[[float], object] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> Iterator[str]:
    """Yield SSE frames until `should_stop()` is true.

    Starts with `ready` (no `last_event_id`), a replay of the events after it, or `resync`. Then
    polls the log every `poll_s` seconds and sends a comment line after `keepalive_s` seconds
    without output. `sleep` must return early once `should_stop()` turns true.
    """
    events = repo.events
    if last_event_id is None:
        cursor = events.latest_seq()
        yield _frame(cursor, "ready", {})
    else:
        seq = _parse_seq(last_event_id)
        if seq is not None and events.replay_status(seq) == "ok":
            cursor = seq
        else:
            cursor = events.latest_seq()
            yield _frame(cursor, "resync", {})
    last_output = monotonic()
    while not should_stop():
        rows = events.since(cursor, novel_id=novel_id, limit=BATCH)
        for row in rows:
            if should_stop():
                return
            cursor = row.seq
            yield _frame(row.seq, row.kind, row.payload)
            last_output = monotonic()
        if len(rows) == BATCH:
            continue
        if monotonic() - last_output >= keepalive_s:
            yield KEEPALIVE
            last_output = monotonic()
        sleep(poll_s)


@router.get(
    "/events",
    operation_id="stream_events",
    response_class=StreamingResponse,
    responses={
        200: {"content": {"text/event-stream": {"schema": {"type": "string"}}}},
        404: {"model": ErrorBody},
    },
)
def stream_events(
    request: Request,
    repo: RepoDep,
    novel_id: Annotated[str | None, Query()] = None,
) -> StreamingResponse:
    """Stream `novel`, `chapter` and `export` events, preceded by `ready` or `resync`."""
    if novel_id is not None:
        if not ids.is_valid(novel_id, "NV"):
            raise NotFound("novel", novel_id)
        repo.novels.get(novel_id)
    stop = threading.Event()
    state: Any = request.app.state
    frames = stream(
        repo,
        last_event_id=request.headers.get("last-event-id"),
        novel_id=novel_id,
        poll_s=state.sse_poll_s,
        keepalive_s=state.sse_keepalive_s,
        should_stop=stop.is_set,
        sleep=stop.wait,
    )

    async def body() -> AsyncIterator[str]:
        try:
            async for frame in iterate_in_threadpool(frames):
                yield frame
        finally:
            # Cancelled on client disconnect or server shutdown: wake the sleeping poll thread.
            stop.set()

    return StreamingResponse(
        body(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
