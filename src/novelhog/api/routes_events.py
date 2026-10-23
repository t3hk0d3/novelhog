"""Server-Sent Events stream (spec section 5.7).

The API keeps no per-client state: a client resumes with `Last-Event-ID`, and the stream polls the
event log. `stream` is an async generator that runs only the repo calls in the threadpool (a
thread is held for the duration of one poll) and waits between polls on the event loop, so an idle
client costs no thread and a disconnect or shutdown cancels the wait at once.
"""

import json
import time
from collections.abc import AsyncGenerator, Awaitable, Callable
from typing import Annotated, Any

import anyio
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool

from novelhog import log
from novelhog.api.deps import get_repo
from novelhog.api.schemas import ErrorBody
from novelhog.repo import Repo

router = APIRouter()

RepoDep = Annotated[Repo, Depends(get_repo)]

BATCH = 500
KEEPALIVE = ": keepalive\n\n"
# A sequence number never has more digits than a 64-bit integer; longer values are not ours.
MAX_ID_LENGTH = 19


def _frame(seq: int, name: str, data: dict[str, object]) -> str:
    return f"id: {seq}\nevent: {name}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"


def _parse_seq(value: str) -> int | None:
    """Return the sequence number in a `Last-Event-ID`, or None if it is not a plain number."""
    text = value.strip()
    if len(text) > MAX_ID_LENGTH or not (text.isascii() and text.isdigit()):
        return None
    return int(text)


async def stream(
    repo: Repo,
    *,
    last_event_id: str | None,
    novel_id: str | None,
    poll_s: float,
    keepalive_s: float,
    should_stop: Callable[[], bool] = lambda: False,
    sleep: Callable[[float], Awaitable[object]] = anyio.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    run: Callable[..., Awaitable[Any]] = run_in_threadpool,
) -> AsyncGenerator[str]:
    """Yield SSE frames until `should_stop()` is true or the consumer cancels the iteration.

    Starts with `ready` (no `last_event_id`), a replay of the events after it, or `resync`. Then
    polls the log every `poll_s` seconds and sends a comment line after `keepalive_s` seconds
    without output. Repo calls go through `run` (the threadpool); waits use `sleep`.

    With `novel_id` only that novel's events are sent. Replay limits (5000 events, 24 h) are
    judged on the whole log, so a filtered client may get `resync` a little earlier than needed.
    """
    events = repo.events
    if last_event_id is None:
        cursor = await run(events.latest_seq)
        yield _frame(cursor, "ready", {})
    else:
        seq = _parse_seq(last_event_id)
        if seq is not None and await run(events.replay_status, seq) == "ok":
            cursor = seq
        else:
            cursor = await run(events.latest_seq)
            yield _frame(cursor, "resync", {})
    last_output = monotonic()
    while not should_stop():
        rows = await run(events.since, cursor, novel_id=novel_id, limit=BATCH)
        for row in rows:
            cursor = row.seq
            yield _frame(row.seq, row.kind, row.payload)
            last_output = monotonic()
        if len(rows) == BATCH:
            continue
        if monotonic() - last_output >= keepalive_s:
            yield KEEPALIVE
            last_output = monotonic()
        await sleep(poll_s)


async def _guarded(frames: AsyncGenerator[str]) -> AsyncGenerator[str]:
    """Pass frames through; end the stream quietly on a database error.

    A database error after the headers went out is logged once and the stream ends, so the
    browser reconnects with `Last-Event-ID`.  A cancellation (client gone) propagates;
    the wrapped generator is closed either way.
    """
    try:
        async for frame in frames:
            yield frame
    except SQLAlchemyError as error:
        log.get().warning("sse_stream_database_error", error=type(error).__name__)
    finally:
        await frames.aclose()


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
        repo.novels.get(novel_id)  # NotFound (404) for a malformed or unknown id
    state: Any = request.app.state
    frames = stream(
        repo,
        last_event_id=request.headers.get("last-event-id"),
        novel_id=novel_id,
        poll_s=state.sse_poll_s,
        keepalive_s=state.sse_keepalive_s,
        should_stop=state.stopping.is_set,
    )
    return StreamingResponse(
        _guarded(frames),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
