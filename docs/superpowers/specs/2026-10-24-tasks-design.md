# novelhog — Tasks Design Spec

Status: draft, 2026-10-24. Branch `feat/tasks`, on top of `feat/novelhog-v1`.

Builds on the v1 spec `docs/superpowers/specs/2026-10-18-novelhog-design.md`,
referred to below as **v1**. v1 stays binding for everything this spec does not
change. Where this spec changes a v1 rule, the v1 section is named and this spec
wins.

## 1. Purpose

v1 has no job table. Each kind of background work keeps its own queue and claim
state on its own table:

- `novels`: `next_refresh_at`, `sync_requested` and a claim, for syncs;
- `chapters`: `pending`/`downloading`, `queued_at`, `priority` and a claim, for
  downloads;
- `exports`: `waiting`/`building` and a claim, for builds.

As a result there are three claim paths, three recovery rules, several checks
that tie claims to state (`ck_chapters_claim_state`, `ck_chapters_started`,
`ck_exports_building`), and a worker loop with one stage per kind. Retry state
(`Reachability`, `Backoff`) is held in memory only. No record of past work is
kept apart from the last error.

This spec moves all background work onto one `tasks` table:

- One queue, one claim and fencing path, one recovery rule.
- Each kind of work has a processor class.
- Each task records attempts, timing and errors.
- Adding a new kind of background work means adding a processor, not new columns
  and a new loop stage.

### Scope

- **The refactor keeps v1's behaviour.** Users see the same API, UI and
  notifications as in v1. The only new things on the wire are a read-only tasks
  API and task events (§7). The UI does not change. Every v1 worker rule keeps
  its effect: priorities, site-unreachable counting, infrastructure backoff, the
  export rules. The deliberate differences are listed in §1.1.
- **Some work stays as sweeps, not tasks.** Notification dispatch (v1 §6.6), the
  trash (v1 §6.1 `empty_trash`) and maintenance stay as they are, outside the
  queue. Notification dispatch is a cursor over `events`. The other two are
  periodic sweeps with no record behind them. Keeping them separate means a full
  task queue can never hold up notifications, and none of them can deadlock
  against task work.

### 1.1 Deliberate differences from v1

| v1 | Here | Why |
|---|---|---|
| `POST /sync` on an idle novel syncs twice: the claim does not clear `sync_requested`, so `finish_sync` schedules another sync at once | it syncs once: the request merges into the queued refresh task | the second sync was a side effect, not intended (v1 §5.3) |
| `REQUEST_DELAY` is waited between chapters of one batch, whatever their hosts | it is waited before a download whose host was fetched less than the delay ago (§6) | politeness is per host; a refresh or another host's chapter no longer waits |
| `NOVELHOG_BATCH_SIZE` chapters per claim | one task per claim (§6) | a refresh never waits behind a batch |

### Decisions (user, 2026-10-24)

1. Whether a task's state changes are written to `events` is a per-task
   `emit_event` flag.
2. Notifications, trash and maintenance stay separate from tasks.
3. v1 stays as it is. This work goes in a new branch on top of it.
4. Kinds and processors are named after their entity (`chapter_download`,
   `ChapterDownloadProcessor`).
5. A read-only tasks API with no routes that change tasks; async endpoints keep
   returning records, not task IDs.
6. The task list uses the worker's claim order.
7. `GET /tasks?since=` takes a commit-ordered cursor, not a timestamp.
8. During a sync, `next_refresh_at` comes from the running refresh task.

## 2. Concepts

| Term | Meaning |
|---|---|
| **Task** | One unit of background work for one record (`entity_id`): sync a novel, download a chapter, build an export. |
| **Kind** | What the task does, named `<entity>_<action>`: `novel_refresh`, `chapter_download` or `export_build`. Each kind accepts one ID prefix (§3); its processor is `<Entity><Action>Processor`. |
| **Processor** | The class that runs tasks of one kind. It checks whether the task is still needed, does the work and writes the result (§5). |
| **Live task** | A task in `queued` or `running`. |
| **Finished task** | A task in `done`, `failed`, `skipped` or `cancelled`. |
| **Entity state** | The record's own state that users see (chapter `pending`, export `waiting`, …). It stays on the record and keeps its v1 meaning. The task holds only the execution state. |

How the two kinds of state relate:

| Entity state (v1 name) | Task that must exist |
|---|---|
| chapter `pending` (has a position) | a `queued` `chapter_download` task for the chapter |
| chapter `downloading` (with or without a position) | a `running` `chapter_download` task for the chapter; a sync that removes a chapter mid-download leaves it `downloading` without a position, and the download still completes (v1) |
| chapter `not_requested`, `downloaded` or `error`, or `pending` without a position | no live `chapter_download` task |
| export `waiting` and buildable (v1 §6.5) | a live `export_build` task for the export |
| export `building` | a `running` `export_build` task for the export |
| export `ready` or `failed` | no live `export_build` task |
| novel `syncing` (derived) | a `running` `novel_refresh` task whose claim has not lapsed |
| every novel | at least one live `novel_refresh` task: a queued one, or a running one (plus at most one queued successor) |

The database cannot enforce these pairs across tables. They are kept by two
rules:

- **One write path.** A change to an entity state and the matching task change
  happen in one transaction, inside one repo method. No other code writes
  `pending`, `downloading`, `waiting` or `building`.
- **Repair.** Maintenance looks for violations, fixes them and logs each fix
  (§8).

**Derived fields change with tasks.** Any task write that changes a field the
API derives from tasks (`Novel.syncing`, `Novel.next_refresh_at`,
`Chapter.queued_at`) appends the entity's event in the same transaction, as v1
§4 does for every state change. The cases are: refresh start, finish, retry,
recovery and repair; a manual-sync merge or a successor enqueue that changes the
novel's `next_refresh_at`; a chapter's download task enqueued, cancelled,
retried, recovered or repaired.

## 3. Schema

### New table `tasks`

```sql
CREATE TABLE tasks (
    id            TEXT PRIMARY KEY,                -- 'TK' + 12 chars (v1 §3)
    kind          TEXT NOT NULL,                   -- 'novel_refresh' | 'chapter_download' | 'export_build'
    entity_id     TEXT NOT NULL,                   -- NV… / CH… / EX…
    novel_id      TEXT NOT NULL REFERENCES novels(id),
    state         TEXT NOT NULL DEFAULT 'queued',
    priority      INTEGER NOT NULL,                -- higher first (§4 "Ordering")
    ordinal       INTEGER NOT NULL DEFAULT 0,      -- tie-break inside a priority (chapter position)
    run_after     TIMESTAMP NOT NULL,              -- not claimable before this
    payload       JSON NOT NULL DEFAULT '{}',      -- request parameters only, never a copy of the entity
    emit_event    BOOLEAN NOT NULL DEFAULT 0,
    attempts      INTEGER NOT NULL DEFAULT 0,      -- starts so far
    error         TEXT,                            -- last error ('<Type>: <summary>', ≤ 300 chars)
    change_seq    BIGINT NOT NULL,                 -- commit-ordered change cursor (§4 "Change cursor")
    claimed_by    TEXT,
    claimed_until TIMESTAMP,
    created_at    TIMESTAMP NOT NULL,
    updated_at    TIMESTAMP NOT NULL,              -- last change visible in the Task object (§7)
    started_at    TIMESTAMP,                       -- latest start
    finished_at   TIMESTAMP,
    duration_ms   INTEGER,                         -- finished_at − started_at of the last run
    CHECK (state IN ('queued', 'running', 'done', 'failed', 'skipped', 'cancelled')),
    CHECK ((kind, substr(entity_id, 1, 2)) IN
           (('novel_refresh', 'NV'), ('chapter_download', 'CH'), ('export_build', 'EX'))),
    CHECK (state <> 'running' OR (claimed_by IS NOT NULL AND started_at IS NOT NULL)),
    CHECK (claimed_by IS NULL OR state IN ('queued', 'running')),  -- queued + claim = claimed, not started
    CHECK ((state IN ('done', 'failed', 'skipped', 'cancelled')) = (finished_at IS NOT NULL)),
    CHECK (state <> 'failed' OR error IS NOT NULL),
    <v1 _id_check('TK')>, <v1 _claim_check>
);
CREATE UNIQUE INDEX tasks_one_queued ON tasks (kind, entity_id) WHERE state = 'queued';
CREATE UNIQUE INDEX tasks_one_running ON tasks (kind, entity_id) WHERE state = 'running';
CREATE INDEX tasks_queue ON tasks (priority DESC, run_after, ordinal, id) WHERE state = 'queued';
CREATE INDEX tasks_by_entity ON tasks (entity_id, created_at);
CREATE INDEX tasks_by_novel ON tasks (novel_id);
CREATE INDEX tasks_finished ON tasks (finished_at) WHERE finished_at IS NOT NULL;
CREATE INDEX tasks_changes ON tasks (change_seq);     -- since (§7), incl. the global task page
```

- One queued task per entity and kind: enqueueing again **merges** into the
  queued task instead of adding a second one (§4 `enqueue`). One running task
  per entity and kind: the same record is never worked on twice at once, and a
  queued task is not claimed while its entity has a running one (§4 `claim`). A
  queued successor can wait while the current task runs. Refresh needs this,
  because "sync now" during a running sync means "sync again after this one"
  (v1's `sync_requested`).
- `novel_id` is there for the v1 lock order (§4 "Lock order") and for
  deleting a novel's tasks. Plain foreign key, no cascade (v1 §4).
- `updated_at` is set on insert and by every write that changes a field of the
  Task object (§7). Like every v1 timestamp it is computed in Python by the
  writer (v1 §4). `claim`, `release` and `renew` touch only the claim, which is
  not exposed, and leave `updated_at` and `change_seq` alone.
- `payload` is `{}` for every kind in this spec. It exists for later request
  parameters, for example a forced full re-fetch.

### Changes to v1 tables

| Table | Removed | Now derived from tasks |
|---|---|---|
| `novels` | `next_refresh_at`, `sync_requested`, `claimed_by`, `claimed_until`, `ck_novels_claim`, index `novels_due_refresh` | `next_refresh_at` = `run_after` of the queued refresh task, else of the running one (v1 keeps the past due time during a sync); `syncing` = a running refresh task whose `claimed_until >= now` (v1 derives it from the claim the same way) |
| `chapters` | `queued_at`, `priority`, `claimed_by`, `claimed_until`, `ck_chapters_queued`, `ck_chapters_claim`, `ck_chapters_claim_state`, `ck_chapters_started`, index `chapters_queue` | `Chapter.queued_at` = the live download task's `created_at` |
| `exports` | `claimed_by`, `claimed_until`, `ck_exports_claim`, `ck_exports_building` | — |
| `events` | — | `ck_events_entity` also allows `TK` |

`novels.refresh_interval` stays. It is the novel's backoff state, not queue
state. `consumers` keeps its claim columns: the notifier is not a task (§1).

The API objects (v1 §5.2) do not change. `next_refresh_at`, `syncing` and
`queued_at` are read through a join on the live task, in REST responses and in
event snapshots alike.

### Configuration

- `NOVELHOG_BATCH_SIZE` is removed. The runner claims one task at a time (§6). If
  the variable is set, the worker starts anyway and logs `config_ignored` once.
- New constant `TASK_RETENTION = 7 d` (§8). Not configurable.
- New constant `TASK_REPAIR_INTERVAL = 1 h` (§8). Not configurable.

## 4. Queue (`repo/tasks.py`, `TaskQueue`)

### Ordering

Claim order is `priority DESC, run_after ASC, ordinal ASC, id ASC`, among queued
tasks with `run_after <= now`, no claim or a lapsed one, and no running task of
the same `(kind, entity_id)`. Base priorities keep v1's order (v1 §6.1:
refreshes, then builds, then downloads):

| Kind | Priority | `ordinal` | `run_after` on enqueue |
|---|---|---|---|
| `novel_refresh` | 300 | 0 | the next refresh time (v1 §6.2), or now for a manual sync |
| `export_build` | 200 | 0 | the export's `created_at`, so builds keep v1's oldest-export-first order |
| `chapter_download`, auto-queued release (v1 `priority = 1`) | 110 | chapter position | now |
| `chapter_download`, user-queued | 100 | chapter position | now |

`ordinal` is copied from the chapter position when the task is enqueued. A later
sync that moves positions does not update it; order is a hint, not a contract.
For downloads, `run_after` plays the part of v1's `queued_at`, so a requeued task
keeps its place in line.

### Change cursor

Every write that changes a field of the Task object (§7) sets the row's
`change_seq` from `event_clock`, the v1 counter that already orders `events`
(v1 §4 "Commit order = seq order"):

- A changed task row's `change_seq` is the seq of its own `TK` event if it gets
  one (§7), else the seq of the last event the transaction appends. A
  transaction that appends no events takes one clock value of its own, as its
  last step, without an event row. The values are written as the transaction's
  last statements, on task rows the transaction already wrote and so already
  holds; this takes no new row lock and keeps the v1 lock order.
- An inserted task row gets `change_seq = 0` as a placeholder and its real
  value at the end of the same transaction; the placeholder is never visible
  to other transactions.
- A `TK` event's payload is built inside `append`, after its seq is taken, so
  it carries the `change_seq` that the row ends up with.
- The clock row stays locked until commit, so on Postgres task changes commit
  in `change_seq` order; SQLite serialises writers anyway. A reader that has
  seen clock value `n` will never later find a committed task change with
  `change_seq <= n` that it did not see.
- Task and event seqs share one number space. A client may compare a task's
  `change_seq` with the SSE stream's event ids.
- **Clock values without an event row** (transactions without events, the
  migration, §9) are valid SSE resume points: `events.replay_status` treats a
  `Last-Event-ID` at or below the latest seq that has no row like any other
  seq and judges age and count from the first row after it, instead of
  answering `resync` (v1 §5.7 changes accordingly).

### Operations

All of these are methods on `TaskQueue`. Each method that takes `conn` writes
inside the caller's transaction, which already holds `lock_novel`. The others
are their own transaction. "Fenced" means the write includes the "owned by me"
condition (v1 §4) and returns `False` when it matched no row.

| Method | Effect |
|---|---|
| `enqueue(conn, kind, entity_id, novel_id, *, priority, ordinal=0, run_after, emit_event=False, payload={}) -> TaskRow` | Inserts a queued task. If a queued task for `(kind, entity_id)` exists, merges into it: `run_after = min(old, new)`, `priority = max`, `emit_event = old OR new`; the claim and `created_at` stay. |
| `cancel(conn, kind, entity_id) -> bool` | Queued task → `cancelled` (claim cleared, `finished_at` set). A running task is not touched. |
| `delete_for_novel(conn, novel_id)` | Deletes every task of the novel, in `id` order (`DELETE /novels`, v1 §5.3). Writes no task events. |
| `claim(worker_id, ttl) -> TaskRow \| None` | One compare-and-set statement through `repo/claims.py` (v1 §4) with the eligibility of "Ordering". The outer `WHERE` repeats `state = 'queued' AND run_after <= :now` besides "free or expired", so a task cancelled meanwhile is never claimed; `repo/claims.py` gains this option. The task stays `queued`. Writes no events. Exempt from the lock order, like v1's batch claim. |
| `start(conn, task, worker_id) -> StartResult` | Queued and owned, and no running task of the same `(kind, entity_id)` → `running`, `attempts + 1`, `started_at = now`; `error` is kept. Returns `StartResult.OK`; `Cancelled` (the task was cancelled after the claim); `Deferred` (a sibling is running: the claim is released, the task stays queued); or `Lost` (claim lapsed or row gone). |
| `finish(conn, task, worker_id, outcome) -> bool` | Fenced: running and owned → `done` (`error` cleared), `failed` (with `error`) or `skipped`, with `finished_at` and `duration_ms` set and the claim cleared. |
| `retry(conn, task, worker_id, *, run_after, error) -> bool` | Fenced: running and owned → the same row back to `queued`, claim cleared, `run_after` and `error` set, `duration_ms` set for the run just ended, `finished_at` left NULL. If another task was queued for the same entity meanwhile, this row merges into it (enqueue rules) and becomes `cancelled`. |
| `release(task, worker_id) -> bool` | Fenced: queued, claimed by me → claim cleared. Used on stop and on `Deferred`. |
| `renew(task, worker_id, ttl) -> bool` | Heartbeat (v1 §4, unchanged). |
| `lapsed(now) -> list[TaskRow]` | Tasks whose claim lapsed (recovery, §8). |
| `prune(before)` | Deletes finished tasks with `finished_at < before`, in `id` order, in batches of 500. |
| `get(task_id) -> TaskRow \| None` | One task by ID (API, §7). |
| `list(*, novel_id=None, entity_id=None, kinds=(), states=(), changed_after=None, offset=None, limit=None) -> TaskPage` | Matching tasks in claim order, the total count and `next_since`; with `changed_after`, every live task plus tasks with `change_seq > changed_after`, unpaged (API, §7). |

The caller of a fenced method that returned `False` throws its result away and
logs it, as in v1.

### Lock order

A task belongs to its novel the same way chapters and exports do. Any
transaction that writes a task row (except `claim`, `release`, `renew` and
`prune`, which are exempt like v1's multi-novel statements) does three things in
order:

1. takes `lock_novel(task.novel_id)` first;
2. writes the entity rows and **all** task rows, including enqueues that follow
   from the change (the refresh successor, §5.1 builds, cancels), before the
   first `events.append`; derived fields in the snapshots (§2) depend on them;
3. appends the entity events (`CH`, then `NV`, then `EX`), then the `TK` events,
   then writes the change cursor (above), last.

This is v1 §4 "Lock order", with tasks added as child rows. `delete_for_novel`
and `prune` may still meet on the same finished rows; deleting in `id` order
keeps that to a rare `Busy` (`503 database_busy`), the class of conflict v1
already accepts.

## 5. Processors

Each kind has two halves, because the worker never holds a transaction (v1
styleguide: no public `repo` method takes a `Connection`) and `repo` must not
import `worker` (import contract `repo-is-standalone`):

```
class TaskHandler(Protocol):                        # repo/task_kinds/, transactional half
    kind: ClassVar[TaskKind]
    def begin(self, conn, task) -> Begin            # inside start's transaction
    def complete(self, conn, task, outcome) -> None # inside finish's or retry's transaction
    def recover(self, conn, task) -> None           # inside recovery's transaction (§8)

class TaskProcessor(Protocol):                      # worker/tasks/, network half
    kind: ClassVar[TaskKind]
    def run(self, task, work, ctx) -> Outcome       # no transaction held
    def stored_keys(self, outcome) -> list[str]     # files run stored, trashed if the fence fails
```

The public `repo.tasks.start(task, worker_id)` runs `TaskQueue.start` and the
handler's `begin` in one transaction and returns the start result together with
the kind's work item (v1's `NovelClaim`, `ChapterClaim` or `ExportClaim`, read
in that transaction). `repo.tasks.finish(task, worker_id, outcome)` runs
`finish` or `retry` and the handler's `complete` in one transaction. The
processor's `run` gets the work item. Below, "processor" means both halves.

- `Begin` is either `Started`, or `Skip(reason)` when the task is no longer
  needed. The check runs inside `start`'s transaction, under `lock_novel`, so it
  is atomic with the state change. On `Skip`, the task goes straight to
  `skipped` and `run` is not called.
- `Outcome` is one of `Done(result)`, `Failed(error)`, or
  `Retry(run_after, error)` for an infrastructure error.
- The check at `begin` does **not** replace fencing. `complete` runs only when
  `finish` or `retry` returned `True`, and every entity write in it is still
  conditional on the entity state, because the record can change while `run` is
  working. For example, the user can delete a novel mid-download.
- `ctx` holds the adapter, fetcher, storage, settings, stop event, heartbeat and
  `Reachability`, the same dependencies as v1's stages.

| Processor | `begin`: still needed when … / entity change on start | `complete` |
|---|---|---|
| `NovelRefreshProcessor` | the novel exists / none, but an `NV` event (`syncing` turns on) | v1 §6.2 `finish_sync` without its scheduling and claim parts: the TOC diff, title, language, cover, `import_error`, interval and `last_refreshed_at`. Its fence is the task fence. Next, still before any event (§4 "Lock order"), the successor: if a queued refresh task exists (a manual sync came in), it stays as it is, due now; otherwise one is enqueued at v1's next time (`now + interval ± 10 %`, or as v1 §6.2 gives). Last, the `CH`/`NV`/`EX` events, so the `NV` snapshot shows the successor's `next_refresh_at`. `Retry` (infrastructure error): the same row is re-queued at `now + REFRESH_MIN_INTERVAL`, interval unchanged, and an `NV` event is appended. A cover stored by `run` whose `complete` did not run is trashed, as in v1. |
| `ChapterDownloadProcessor` | the chapter is `pending` and has a position / `pending` → `downloading` | v1 §6.4 `complete` and `fail`. `Retry` (infrastructure error, or site unreachable below the threshold): `downloading` → `pending` with the task re-queued, `run_after` kept; but a chapter that lost its position meanwhile goes to `not_requested` and the task is cancelled instead (v1 `_back_in_line`). |
| `ExportBuildProcessor` | the export is `waiting` and buildable (v1 §6.5) / `waiting` → `building` | v1 §6.5 `finish` and `fail`. No `Retry`: as in v1, any error fails the export. A stored key whose `finish` did not run is trashed, as in v1. |

### 5.1 When a build task is enqueued

v1 polls for buildable exports (`claim_ready_export`). Here every transaction
that changes chapters of a novel (every caller of `events.emit_novel_change`)
also re-evaluates the waiting exports it affects (`exports.affected_waiting`,
which already covers end chapters) and enqueues a build for each one that is
now buildable in v1's sense: `skip_missing`; or an end chapter without a
position (the build then fails with "Range changed upstream"); or every
positioned chapter in the range downloaded. This covers downloads, failures,
dequeues, and syncs that move, add or remove chapters. Creating an export that
is already buildable enqueues its build in the same transaction. v1's "a
cancelled chapter that a waiting export needs fails the export" stays as it is.
The enqueues happen before the transaction's first `events.append` (§4 "Lock
order"); the new builds' `TK` events follow the entity events. The merge rule
makes a repeated enqueue harmless.

### 5.2 Callers that set `emit_event`

| Enqueue site | `emit_event` |
|---|---|
| `POST /novels` (first refresh) | true |
| `POST /novels/{id}/sync` (manual sync) | true |
| scheduled successor refresh | false |
| export build (§5.1) | true |
| chapter download (user-queued and auto-queued) | false. The chapter's `CH` events already show every step. |
| repair (§8) | the flag a fresh enqueue at that site would set |

## 6. Runner (`worker/runner.py`)

This replaces v1 §6.1's refresh, build and download stages. Recovery,
notifications, trash and maintenance stay where they are in the loop.

```
loop until stop requested:
    recover()                                    # v1 claims (consumers) + tasks (§8)
    behind = dispatch_notifications()            # unchanged (v1 §6.6)
    empty_trash()                                # unchanged
    repair_if_due()                              # at most every TASK_REPAIR_INTERVAL, idle or not (§8)
    if task = queue.claim(worker_id, CLAIM_TTL):
        run_task(task)
        continue
    if behind: continue
    maintenance()                                # + task retention (§8)
    stop.wait(IDLE_SLEEP)

run_task(task):
    processor = processors[task.kind]
    if task.kind == chapter_download:
        host = the task's novel host                              # read with the claim
        stop.wait(until last_fetch[host] + request_delay(host))   # politeness per host (§1.1)
        if stop requested: queue.release(task); return
    in one transaction (lock_novel → rows → events → cursor):
        result = queue.start(task)
        if result is Cancelled: log task_cancelled; return       # info: an ordinary dequeue
        if result is Deferred: log task_deferred; return         # sibling running; claim released
        if result is Lost: log task_lost; return
        begin = processor.begin(task)
        if begin is Skip: queue.finish(task, skipped); log task_skipped; return
    with heartbeat(task):
        outcome = processor.run(task, ctx)       # exceptions → Failed('<Type>: <summary>')
    if task.kind == chapter_download: last_fetch[host] = now
    in one transaction:
        fenced = queue.retry(...) if outcome is Retry else queue.finish(...)
        if fenced: processor.complete(task, outcome); log task_finished
        else: log task_lost (and trash what run stored)
    if outcome is Retry: raise to the loop's backoff (v1 §6.1, unchanged)
```

- The runner claims one task at a time, so an urgent refresh never waits behind
  a batch of downloads. Downloads therefore cost one claim statement each. That
  is accepted; batching is future work.
- `last_fetch` is per worker and in memory, like `Reachability`.
  `request_delay(host)` is v1's `max(REQUEST_DELAY, adapter.request_delay(host))`,
  seconds against a `CLAIM_TTL` of minutes, so the wait needs no heartbeat.
- On stop: a task that was claimed but not started is released. A running task
  finishes first (v1 §6.1, unchanged).
- Site-unreachable counting (v1 §6.1) is unchanged. `Reachability` stays per
  worker and in memory, and the processors decide between `Retry` and `Failed`
  exactly as v1's stages do.
- **A start or finish transaction that itself fails** (`repo.Busy`, an
  unexpected error) is logged as `task_error` and raised to the loop's backoff. The task
  keeps its claim (claimed or running) until `CLAIM_TTL` lapses; recovery then
  puts it back (§8), and the result of `run` is lost, as in v1.

### Logging

Every transition logs one structured line with `task_id`, `kind`, `entity_id`
and `attempts`:

- `task_started`
- `task_skipped` (`reason`)
- `task_cancelled`, `task_deferred` (info)
- `task_finished` (`outcome`, `duration_ms`, `error`)
- `task_retry` (`run_after`)
- `task_lost`, `task_error`
- `task_recovered`
- `task_repaired` (`problem`)

## 7. API and task events

### Task object

```
id           string          TK…
kind         string          novel_refresh | chapter_download | export_build
entity_id    string          NV… / CH… / EX…
novel_id     string
state        string          queued | running | done | failed | skipped | cancelled
attempts     int
error        string | null   last error; kept while the task is retried, cleared when it is done
run_after    timestamp
change_seq   int             commit-ordered change cursor (§4)
created_at   timestamp
updated_at   timestamp       last change to any field above
started_at   timestamp | null
finished_at  timestamp | null
duration_ms  int | null      of the last run
```

`payload`, `priority`, `ordinal`, `emit_event` and the claim are not exposed.
The model lives in `domain.payloads` and is the single source for REST and SSE
(v1 §4). A task cancelled by a merge keeps `started_at` and `duration_ms` of its
last run.

### Routes (read-only)

```
GET /api/v1/tasks
GET /api/v1/tasks/{task_id}
```

- `GET /tasks?novel_id=&entity_id=&kind=&state=&since=&offset=&limit=` →
  `{items: Task[], total: int, next_since: int}`, in the worker's claim order
  (§4 "Ordering"): `priority` (highest first), then `run_after` (earliest
  first), then `ordinal`, then `id`. Queued tasks therefore appear in the order
  the worker will take them once due. The same order applies with `since`.
  Every filter is optional and combines with AND. `kind` and `state` may repeat
  (`state=queued&state=running` = live tasks). Without `since`, `offset`
  defaults to 0 and `limit` to 50, at most 500.
- `next_since` is the `event_clock` value, read in the same transaction
  **before** the list query. Every response carries it, so a client starts with
  a plain list and then polls.
- `since=<int>` (the `next_since` of an earlier response) returns every live
  task plus every task with `change_seq > since`, whatever its state: one that
  started, finished, failed, was skipped, cancelled or retried since then. It
  drops only finished tasks unchanged since the cursor.
    - **Exact.** Because the cursor is read before the query and task changes
      commit in `change_seq` order (§4), a change is never missed and needs no
      overlap. A change that commits between the clock read and the query may
      come twice; the client merges by `id`.
    - **Unpaged.** A `since` response holds every match: `offset` or `limit`
      with `since` → `422 validation_error`; `total = len(items)`. Pagination
      can be added later if responses grow too big.
    - **Vanished tasks.** Because the response is complete, a live task the
      client holds that is not in it no longer matches the filter; with no
      `state` filter that means it no longer exists (its novel was deleted),
      and the client drops it. Finished tasks removed by retention simply stop
      being returned.
    - **Filters.** `novel_id`, `entity_id`, `kind` and `state` filter the
      combined set: `state=failed&since=…` = tasks that failed since then, with
      no live tasks. A `since` at or above the latest clock value returns only
      live tasks.
    - **Several tasks per entity.** A merge or a retry can leave a `cancelled`
      row next to the live task of the same entity. A client showing one row
      per entity shows its newest task (`created_at`).
- A malformed filter value (an ID with the wrong prefix or format, an unknown
  `kind` or `state`, a `since` that is not a non-negative integer, `limit` out
  of range, `offset` or `limit` together with `since`) → `422
  validation_error`. A well-formed ID that matches nothing → an empty list, not
  `404`.
- `GET /tasks/{task_id}` → the Task, or `404 not_found` for an unknown,
  malformed or pruned ID.
- There are no routes that change tasks. Retrying, cancelling and "sync now"
  stay on the records (v1 §5.3–§5.5), so there is one way to make each change
  and the task schema stays free to change.
- Both routes follow v1's host and origin checks (v1 §5.9) and read-only
  database access like the other `GET` routes.
- **Async endpoints do not return task IDs.** A task ID is not a stable handle
  for the work a request started: "sync now" usually merges into the queued
  refresh, a retry can move the work to another row, an export has no build
  task while it waits, and queueing chapters creates one task per chapter. A
  client follows the record instead: `task` events with its `entity_id` (manual
  syncs, first imports and builds set `emit_event`, §5.2), or
  `GET /tasks?entity_id=<id>&state=queued&state=running`.

### Task events

- When a task with `emit_event = true` is inserted or changes a field of the
  Task object (enqueue, merge, start, finish, retry, skip, cancel, recovery,
  repair), the same transaction appends one `TK` event for it after the
  entity's own events. When one write changes two rows (a retry or recovery
  that merges, a cancel next to a new enqueue), each changed row with
  `emit_event` gets its own `TK` event. Claim, release and renew write none.
- Payload: the Task object, as `GET /tasks/{task_id}` would return it right
  after the change (v1 §4 "Event rows are state snapshots").
- **SSE** event name: `task` (v1 §5.7 gains the row `TK → task`). The web
  client registers named listeners only, so it ignores `task` events until a
  later UI uses them.
- **The notifier** skips `TK` events and advances past them. Notification rules
  (v1 §6.6, §8.6) do not change.
- **Pruning** (v1 §4) keeps its "newest row of each `entity_id`" exception for
  `NV`, `CH` and `EX` only. `TK` events are pruned purely by age, so pruned tasks
  leave no rows behind.

## 8. Recovery, retention, repair

- **Recovery** (in `recover()`, each lapsed task in its own transaction under
  `lock_novel`):
  - a queued task with a lapsed claim: the claim is cleared;
  - a running task with a lapsed claim: back to `queued` with `run_after`
    kept, then `processor.recover` applies v1's reset (v1 `work.recover`):
    chapter `downloading` → `pending`, or `not_requested` with the task
    cancelled if it lost its position; export `building` → `waiting` with an
    `EX` event; refresh → an `NV` event (`syncing` turns off);
  - if a queued task for the same entity already exists, the recovered one
    merges into it and becomes `cancelled`.
- **Retention:** maintenance deletes finished tasks whose `finished_at` is older
  than `TASK_RETENTION` (7 d).
- **Repair** runs at most every `TASK_REPAIR_INTERVAL` (1 h) from the loop,
  whether or not the worker is idle, so a backlog cannot postpone it. It scans
  for the §2 pairs that do not hold. Each fix runs in its own transaction, takes
  `lock_novel`, **re-checks its condition** there and does nothing if it no
  longer holds; a fix is logged as `task_repaired`.

  | Problem | Fix |
  |---|---|
  | chapter `pending` with a position and no live download task | enqueue (user-queued priority: an auto-queued release loses its head start, accepted) |
  | chapter `pending` without a position | → `not_requested`, cancel its queued task |
  | chapter `downloading` with no running task | → `pending` and enqueue, or `not_requested` without a position |
  | export `waiting` and buildable, no live build task | enqueue |
  | export `building` with no running build task | → `waiting`; enqueue if buildable |
  | novel with no live refresh task | enqueue at `last_refreshed_at + refresh_interval`, or now if never refreshed |
  | queued task whose entity is gone or finished (chapter `not_requested`, `downloaded` or `error`; export `ready` or `failed`) | → `cancelled` |

  Repair never touches a running task, nor the entity of one, while its claim
  is valid: a running task counts as present until its claim lapses and
  recovery handles it. A chapter that a sync removed mid-download is therefore
  not a problem; its download completes as in v1.

## 9. Migration

One Alembic revision, following v1 §4's rules (both databases; SQLite batch
mode with foreign keys off). Stop the workers before upgrading.

1. Create `tasks`. Add `TK` to `ck_events_entity`.
2. Fill it from the v1 columns, dropping all claims. Every migrated task gets
   `emit_event = false`, `updated_at` = the migration time, and `change_seq` =
   one new `event_clock` value taken for the whole migration.
   - each novel → one queued refresh task: `run_after = next_refresh_at`
     (v1 already set it to the request time when `sync_requested`);
     `created_at` = the migration time;
   - each chapter `pending` or `downloading` **with a position** → one queued
     download task: `created_at = run_after = queued_at`, priority 110 or 100
     from `priority`, `ordinal = position`; a `downloading` chapter becomes
     `pending`;
   - each chapter `pending` or `downloading` without a position →
     `not_requested`, no task (v1 `_back_in_line`);
   - each export `building` → `waiting`; each `waiting` export that is
     buildable → one queued build task with `created_at = run_after =` the
     export's `created_at`.
3. Drop the columns, checks and indexes listed in §3.

**Downgrade:**

1. Reset running work: chapters `downloading` → `pending` (or `not_requested`
   without a position), exports `building` → `waiting`.
2. Restore the columns: `next_refresh_at` from the novel's queued refresh task,
   else its running one, else now; `sync_requested = false`; `queued_at` and
   `priority` (110 → 1, 100 → 0) from each pending chapter's live download task,
   else `queued_at = updated_at` and `priority = 0`; all claims empty.
3. Delete `TK` events, restore v1's checks and indexes, drop `tasks`.

## 10. Code layout

| Path | Change |
|---|---|
| `repo/schema.py`, `repo/migrations/` | `tasks` table, column drops (§3, §9) |
| `repo/tasks.py` | new: `TaskQueue` (§4), internal; public `Tasks` (`repo.tasks`) composing start, finish, recovery, repair and the read API |
| `repo/task_kinds/{base,novel_refresh,chapter_download,export_build}.py` | new: `TaskHandler` protocol and the three handlers (§5); they take over the transactional parts of v1's `finish_sync`, `work.start/complete/fail/requeue` and `exports._started/finish/fail` |
| `domain/tasks.py` | new: `TaskKind`, `TaskState`, priorities, `Begin`, `Outcome` |
| `repo/claims.py` | claim's outer `WHERE` can repeat an eligibility condition (§4 `claim`) |
| `repo/work.py` | loses `claim_batch` and chapter/novel/export recovery; keeps consumer recovery |
| `repo/chapters.py`, `novels.py`, `exports.py` | entity transitions call `TaskQueue` in the same transaction; derived fields via join; build enqueueing (§5.1) |
| `repo/events.py` | `TK → task` event kind; change cursor helper; `replay_status` accepts a seq without a row (§4) |
| `domain/payloads.py` | `TaskPayload`; `NovelPayload`/`ChapterPayload` read derived fields |
| `api/routes_tasks.py`, `api/schemas.py` | new: the two read-only routes and the `Task` schema (§7) |
| `api/routes_events.py` | streams `task` events |
| `notifications/rules.py`, `notifications/dispatcher.py` | skip `TK` events |
| `web/src/api/events.ts` | `task` stays out of `EVENT_NAMES`; the event type documents it |
| `worker/tasks/{base,novel_refresh,chapter_download,export_build}.py` | new: `TaskProcessor` protocol and the three processors' `run`; they take over the network parts of `worker/refresh.py`, `downloads.py`, `builds.py` |
| `worker/runner.py` | new: `run_task`, processor registry, per-host politeness |
| `worker/loop.py` | stages 4–6 replaced by the runner; repair on its own interval (§6) |
| `worker/maintenance.py` | retention and repair (§8) |
| `config.py` | drops `batch_size`, logs `config_ignored` |

## 11. Testing

Same layout and gates as v1 §13. The v1 worker integration tests
(`tests/integration/worker/`) are the behaviour contract. They are changed only
where they read removed columns directly or where §1.1 changes the behaviour,
and they must keep asserting the same outcomes. Concurrency scenarios run on
SQLite and Postgres.

### BDD skeleton

```
TaskQueue
    enqueue
        When no queued task exists for (kind, entity)
            - inserts a queued task with the given priority, ordinal, run_after, emit_event
        When a queued task exists
            - merges: run_after = min, priority = max, emit_event = OR; keeps created_at and claim
            - leaves exactly one queued row
        When only a running task exists
            - inserts a queued successor beside it
        With an entity prefix that does not match the kind
            - is rejected by the database check
    claim
        - returns the queued task with highest priority, then earliest run_after, then lowest ordinal, then id
        - skips tasks with run_after in the future
        - skips a queued task whose (kind, entity) has a running task
        - takes a task whose claim lapsed
        - two workers racing claim different tasks or one gets None
        - a task cancelled while the claim statement runs is not claimed (Postgres race)
        - writes no events and leaves change_seq and updated_at alone
    start
        When queued and owned
            - sets running, attempts + 1, started_at; keeps error; returns OK
        When the task was cancelled after the claim
            - changes nothing and returns Cancelled
        When a sibling of the same (kind, entity) is running
            - releases the claim, keeps the task queued and returns Deferred
        When the claim was lost
            - changes nothing and returns Lost
    finish
        - sets done (error cleared) / failed (error) / skipped with finished_at, duration_ms, claim cleared; returns True
        When the claim was lost
            - changes nothing and returns False
    retry
        - returns the same row to queued with run_after, error and duration_ms set, finished_at NULL, claim
          cleared; returns True
        When another queued task for the entity appeared meanwhile
            - merges into it and cancels the retried row
        When the claim was lost
            - returns False
    cancel
        - turns a queued task into cancelled; leaves a running task alone
        - is called by every dequeue: chapters PATCH to not_requested, a sync removing a pending chapter
        - is called for builds by export delete and by failing a waiting export
    delete_for_novel
        - removes every task of the novel, no TK events
    change cursor
        - every insert, merge, start, finish, retry, cancel, recovery and repair sets change_seq
        - change_seq is the row's own TK event seq, else the transaction's last event seq
        - a TK event payload's change_seq equals the row's change_seq after commit
        - change_seq order equals commit order on Postgres (two concurrent writers)
        - a transaction without events takes one clock value of its own
        - an inserted row never shows change_seq 0 to another transaction
        - claim, release and renew do not change it
    updated_at
        - is set by the same writes as change_seq, from the writer's clock
    prune
        - deletes finished tasks older than TASK_RETENTION in batches, keeps live and recent ones
    lock order (Postgres)
        - start against DELETE /novels: one wins, no deadlock; the loser is Lost or not found
        - retry against a concurrent manual-sync merge: one queued row remains
GET /api/v1/tasks
    Without filters
        - returns {items, total, next_since} in claim order: priority DESC, run_after ASC, ordinal ASC, id ASC
        - pages with offset 0 and limit 50 by default; total counts all matches
        - next_since is the event_clock value read before the query
    With novel_id, entity_id, kind and state filters
        - returns only matching tasks; repeated kind or state means any of them
    With since
        - returns every queued and running task
        - returns tasks with change_seq > since in any state (done, failed, skipped, cancelled)
        - leaves out finished tasks whose change_seq <= since
        - combined with state, returns only matches of both (state=failed&since=… = failed since then)
        - a since at or above the latest clock value returns only live tasks
        - a change committed after a response is returned by the next poll with that response's next_since
        - a long transaction that commits late is still returned by the next poll (no gap)
        - returns every match unpaged in claim order, total = len(items)
        - a live task of a deleted novel is absent from the next response
    With a malformed filter value, a since that is not a non-negative integer, offset or limit with since,
    or limit above 500
        - 422 validation_error
    With a well-formed ID that matches nothing
        - empty list
    - response fields are exactly the Task object; payload, priority, ordinal, emit_event and claim are absent
GET /api/v1/tasks/{task_id}
    - returns the Task
    For an unknown, malformed or pruned ID
        - 404 not_found
Task events
    When emit_event is true
        - enqueue, merge, start, finish, retry, skip, cancel, recovery and repair each append a TK event
          after the entity's events
        - a retry or recovery that merges appends one TK event per changed row
        - payload equals what GET /tasks/{task_id} returns right after the change
    When emit_event is false
        - no TK events
    SSE
        - streams a TK event as event: task with its seq as id
        - a reconnect with a Last-Event-ID that has no event row (task-only transaction, migration) replays,
          it does not resync
    Notifier
        - dispatch advances past TK events and sends nothing for them
    Pruning
        - TK events are pruned by age even when they are an entity's newest row
Derived fields and entity events
    - refresh start appends an NV event with syncing true
    - refresh finish appends an NV event with syncing false and next_refresh_at = the successor's run_after
    - refresh retry appends an NV event with syncing false and next_refresh_at = now + REFRESH_MIN_INTERVAL
    - during a sync, NV snapshots have next_refresh_at = the running task's run_after (never null)
    - a manual sync during a sync appends an NV event with next_refresh_at = now
    - syncing is false once the running task's claim lapsed, before recovery
    - chapter queued_at is the live download task's created_at; enqueue and cancel append CH events
Runner.run_task
    When start returns Cancelled, Deferred or Lost
        - logs task_cancelled / task_deferred / task_lost; run is not called
    When begin says Skip
        - task becomes skipped, run is not called, entity unchanged
    When run returns Done
        - task done, processor.complete writes the result in the same transaction
    When run raises
        - task failed with '<Type>: <summary>', traceback logged
    When run returns Retry
        - task queued with run_after, entity reset, loop backs off
    When the claim is lost during run
        - result discarded, only task_lost logged, entity not overwritten, stored files trashed
    When the start or finish transaction fails
        - task_error logged, loop backs off, task recovered after CLAIM_TTL
    On stop
        - a claimed, unstarted task is released; a running task finishes
    Politeness
        - a download waits until the host's previous fetch + request delay
        - a refresh or another host's download does not wait
NovelRefreshProcessor
    - "still needed" = the novel exists
    On Done or Failed
        - applies the TOC diff, title, cover, import_error and interval of v1 finish_sync
        - enqueues the successor at v1's next refresh time, before the NV event
        - the NV event's next_refresh_at is the successor's run_after
    When a manual sync was queued during the run
        - the queued task stays due now; no second successor
    On Retry
        - the same task is re-queued at now + REFRESH_MIN_INTERVAL, interval unchanged
    When the fence fails after a cover was stored
        - the cover key is trashed
    POST /sync on an idle novel
        - syncs once (§1.1)
ChapterDownloadProcessor
    - "still needed" = the chapter is pending and positioned; start turns it downloading
    When the chapter was dequeued after claim
        - start returns Cancelled
    When a sync removed the chapter after claim
        - start returns Cancelled (the sync's dequeue cancelled the task)
    When a sync removed the chapter mid-download
        - the download completes and stores the content (v1); repair does not touch it
    On Done / Failed
        - v1 complete / fail
    On Retry
        - chapter back to pending, run_after kept (keeps its place in line)
        When the chapter lost its position meanwhile
            - chapter not_requested, task cancelled
    Site unreachable
        - below SITE_UNREACHABLE_RETRIES → Retry; at the threshold → Failed "Site unreachable: <message>"
ExportBuildProcessor
    - "still needed" = the export is waiting and buildable; start turns it building
    On Done / Failed
        - v1 finish / fail
    When run raises
        - the export fails (no retry)
    When the export was deleted meanwhile
        - result discarded, stored key trashed
Build enqueueing
    - export created already buildable (incl. skip_missing) → build task queued in the same transaction
    - last missing chapter downloaded → build task queued
    - a sync moves an undownloaded chapter out of the range → build task queued
    - a sync removes an end chapter → build task queued; the build fails "Range changed upstream"
    - a sync returns a removed downloaded chapter into the range, completing it → build task queued
    - build enqueues come before the transaction's first event; their TK events follow the entity events
    - builds of two exports run oldest created_at first
    - needed chapter cancelled → export failed (v1), no build task
Recovery
    - lapsed running download → queued, chapter pending, run_after kept
    - lapsed running download of a chapter without a position → chapter not_requested, task cancelled
    - lapsed running build → queued, export waiting with an EX event
    - lapsed running refresh → queued, NV event (syncing off)
    - lapsed claimed queued task → claim cleared
    - a recovered task with a queued sibling merges into it and becomes cancelled
Repair (maintenance)
    - runs at most every TASK_REPAIR_INTERVAL, also while the worker is busy
    - each problem in §8's table is fixed and logged as task_repaired
    - a fix whose condition no longer holds in its own transaction does nothing
    - a novel that is syncing is not touched
    - a building export with a valid running claim is not reset
    - a running task is never cancelled by repair, even when its entity is gone or finished
    - a consistent database produces no repairs
Configuration
    - NOVELHOG_BATCH_SIZE set → worker starts and logs config_ignored once
Migration (SQLite and Postgres)
    - novels get a queued refresh at next_refresh_at
    - positioned pending and downloading chapters get download tasks with created_at = run_after = queued_at,
      priority from v1 priority; downloading ones come back pending
    - unpositioned pending or downloading chapters become not_requested without a task
    - building exports come back waiting; buildable waiting exports get build tasks at their created_at
    - Chapter.queued_at and Novel.next_refresh_at have the same values before and after
    - downgrade with queued and running tasks restores next_refresh_at, sync_requested, queued_at and priority
      and passes v1's checks, also for a pending chapter without a live task
    - autogenerate against schema.metadata reports no differences
```

## 12. Out of scope (later)

- **Pausing an unreachable host** (the open v1 question "M2"). With tasks this
  becomes "push `run_after` forward for that host's queued download tasks", so
  the chapters stay queued.
- **Tasks in the UI** (its own spec, after this ships): an activity section on
  the novel page, a worker status indicator, a retry hint on chapter rows. It
  will revisit retention (last N per entity instead of 7 days) and whether
  scheduled refreshes emit events.
- Routes that change tasks, and task kinds without a novel.
- Returning the task from `POST /novels/{id}/sync`, if following it through
  events proves awkward in the UI.
- **Novel previews** (its own spec, on top of this one): a draft novel
  (`confirmed = false`) whose first-import task fills the preview. Confirming
  keeps it without a second scrape; cancelling deletes it; maintenance deletes
  abandoned drafts. Drafts stay out of the library, get no successor refresh,
  never auto-queue or notify. Not an entity-less `novel_preview` task: that
  would need tasks and events without a novel, a result field, separate cover
  handling, and would scrape the site twice.
- Pagination for `since` responses, and a counts endpoint for a header
  indicator, if live tasks grow too many to return on every poll.
- Batch claims for downloads, and per-host queues or concurrency.
- Task dependencies in general. Builds use the enqueue rule in §5.1 instead.

## 13. Resolved questions

1. `TASK_RETENTION = 7 d` is right, for failed and successful tasks alike
   (user, 2026-10-24).
2. The `emit_event` defaults in §5.2 stand (user, 2026-10-24).
