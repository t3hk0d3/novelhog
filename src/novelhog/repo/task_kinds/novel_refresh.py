"""The repo half of `novel_refresh`: v1's `finish_sync` as a task handler (tasks spec 5, 5.2).

The handler reads the work item (`NovelClaim`) when the refresh starts, turns `syncing` on with an
`NV` event, and on finish applies the sync result: the TOC diff, the novel fields and the
interval of v1 section 6.2. Before any event it enqueues the successor refresh, so the `NV`
snapshot shows the successor's `next_refresh_at` (tasks spec section 4, "Lock order").
"""

import random
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, ClassVar

from sqlalchemy import Connection, func, select, update

from novelhog.domain.refresh import jittered, next_interval
from novelhog.domain.tasks import (
    PRIORITY_REFRESH,
    Begin,
    Done,
    Failed,
    Outcome,
    Retry,
    Skip,
    Started,
    TaskKind,
)
from novelhog.repo import chapters as chapter_repo
from novelhog.repo import events, hosts, novels
from novelhog.repo.chapters import TocDiff
from novelhog.repo.context import RepoContext
from novelhog.repo.schema import chapters
from novelhog.repo.schema import novels as novels_table
from novelhog.repo.tasks import TaskQueue, TaskRow


@dataclass(frozen=True)
class NovelClaim:
    """A novel whose refresh started, with what the sync needs to know about it.

    `host_supported` and `known_pages` (chapters with a position) are read when the refresh
    starts, for the checks of v1 section 6.2 that run before the TOC is applied.
    """

    novel_id: str
    host: str
    toc_url: str
    title: str | None
    cover_key: str | None
    followed: bool
    initial: bool
    refresh_interval: int
    host_supported: bool
    known_pages: int


@dataclass(frozen=True)
class SyncSucceeded:
    """A fetched TOC. `title`, `language` and `cover_key` are None when nothing was found."""

    urls: list[str]
    title: str | None
    language: str | None
    cover_key: str | None
    min_s: int
    max_s: int
    rng: random.Random


@dataclass(frozen=True)
class SyncFailed:
    """A failed sync: `import_error` is set on the initial import only; the schedule is given."""

    import_error: str | None
    interval: int
    next_at: datetime


type SyncOutcome = SyncSucceeded | SyncFailed


_EMPTY = TocDiff(new=[], moved=[], removed=[], returned=[])


class NovelRefreshHandler:
    """The transactional half of `novel_refresh`; `complete` reports the `TocDiff` it applied."""

    kind: ClassVar[TaskKind] = TaskKind.NOVEL_REFRESH

    def __init__(self, context: RepoContext) -> None:
        self._static_base_url = context.static_base_url
        self._queue = TaskQueue(context)

    def work(self, conn: Connection, task: TaskRow) -> NovelClaim:
        """Read the novel as the sync needs it."""
        row = conn.execute(select(novels_table).where(novels_table.c.id == task.entity_id)).one()
        known = conn.execute(
            select(func.count())
            .select_from(chapters)
            .where(chapters.c.novel_id == row.id, chapters.c.position.is_not(None))
        ).scalar_one()
        return NovelClaim(
            novel_id=row.id,
            host=row.host,
            toc_url=row.toc_url,
            title=row.title,
            cover_key=row.cover_key,
            followed=row.followed,
            initial=row.last_refreshed_at is None,
            refresh_interval=row.refresh_interval,
            host_supported=row.host in hosts.supported_hosts(conn),
            known_pages=int(known),
        )

    def begin(self, conn: Connection, task: TaskRow, now: datetime) -> Begin:
        """Still needed while the novel exists; the `NV` event shows `syncing` on."""
        found = conn.execute(
            select(novels_table.c.id).where(novels_table.c.id == task.entity_id)
        ).first()
        if found is None:
            return Skip("novel gone")
        self._emit(conn, task.entity_id, now)
        return Started()

    def complete(
        self, conn: Connection, task: TaskRow, outcome: Outcome[Any], now: datetime
    ) -> TocDiff | None:
        """Apply the sync result, enqueue the successor, then append the events.

        `Retry` (an infrastructure error) leaves the novel's schedule to the re-queued task and
        only appends the `NV` event. Returns the applied diff (empty after a failure), or None
        after a `Retry`.
        """
        novel_id = task.entity_id
        row = conn.execute(select(novels_table).where(novels_table.c.id == novel_id)).first()
        if row is None:
            return None
        if isinstance(outcome, Retry):
            self._touch(conn, novel_id, {}, now)
            self._events(conn, novel_id, _EMPTY, now)
            return None
        diff, values, due = self._result(conn, row, outcome, now)
        self._touch(conn, novel_id, values, now)
        if self._queue.queued(conn, self.kind, novel_id) is None:
            self._queue.enqueue(
                conn, self.kind, novel_id, novel_id, priority=PRIORITY_REFRESH, run_after=due
            )
        self._events(conn, novel_id, diff, now)
        return diff

    def recover(self, conn: Connection, task: TaskRow, now: datetime) -> None:
        """The lapsed refresh went back to the queue: the `NV` event shows `syncing` off."""
        self._emit(conn, task.entity_id, now)

    @staticmethod
    def _result(
        conn: Connection, row: Any, outcome: Done[Any] | Failed[Any], now: datetime
    ) -> tuple[TocDiff, dict[str, object], datetime]:
        """The diff, the novel's new values and the successor's due time (v1 section 6.2)."""
        result = outcome.result
        if isinstance(outcome, Done) and isinstance(result, SyncSucceeded):
            diff = chapter_repo.apply_toc(
                conn,
                row.id,
                result.urls,
                initial=row.last_refreshed_at is None,
                followed=row.followed,
                now=now,
            )
            interval = next_interval(
                row.refresh_interval,
                found_new=bool(diff.new),
                failed=False,
                min_s=result.min_s,
                max_s=result.max_s,
            )
            values: dict[str, object] = {
                "title": row.title if row.title is not None else result.title,
                "language": result.language if result.language else row.language,
                "cover_key": result.cover_key if result.cover_key else row.cover_key,
                "import_error": None,
                "last_refreshed_at": now,
                "refresh_interval": interval,
            }
            return diff, values, now + timedelta(seconds=jittered(interval, result.rng))
        if isinstance(result, SyncFailed):
            values = {"refresh_interval": result.interval}
            if result.import_error is not None:
                values["import_error"] = result.import_error
            return _EMPTY, values, result.next_at
        # A failure without a schedule (the processor always gives one): keep the interval.
        return _EMPTY, {}, now + timedelta(seconds=row.refresh_interval)

    @staticmethod
    def _touch(conn: Connection, novel_id: str, values: dict[str, object], now: datetime) -> None:
        conn.execute(
            update(novels_table)
            .where(novels_table.c.id == novel_id)
            .values(**values, updated_at=now)
        )

    def _events(self, conn: Connection, novel_id: str, diff: TocDiff, now: datetime) -> None:
        events.emit_novel_change(
            conn,
            novel_id,
            diff.changed_ids,
            now=now,
            static_base_url=self._static_base_url,
            released=diff.new,
        )

    def _emit(self, conn: Connection, novel_id: str, now: datetime) -> None:
        """Append the novel's `NV` event; the snapshot is built after the clock is taken."""
        events.append(
            conn,
            novel_id,
            novel_id,
            lambda: novels.snapshot(
                conn, novel_id, now=now, static_base_url=self._static_base_url
            ).to_json(),
            now=now,
        )
