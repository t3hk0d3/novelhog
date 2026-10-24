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

- **The refactor does not change behaviour.** Users see the same API, UI and
  notifications as in v1. The only new things on the wire are optional task
  events (§7). Every v1 worker rule keeps its effect: priorities, politeness,
  site-unreachable counting, infrastructure backoff, the export rules.
- **Some work stays as sweeps, not tasks.** Notification dispatch (v1 §6.6), the
  trash (v1 §6.1 `empty_trash`) and maintenance stay as they are, outside the
  queue. Notification dispatch is a cursor over `events`. The other two are
  periodic sweeps with no record behind them. Keeping them separate means a full
  task queue can never hold up notifications, and none of them can deadlock
  against task work.

### Decisions (user, 2026-10-24)

1. Whether a task's state changes are written to `events` is a per-task
   `emit_event` flag.
2. Notifications, trash and maintenance stay separate from tasks.
3. v1 stays as it is. This work goes in a new branch on top of it.

## 2. Concepts

| Term | Meaning |
|---|---|
| **Task** | One unit of background work for one record (`entity_id`): sync a novel, download a chapter, build an export. |
| **Kind** | What the task does: `refresh`, `download` or `build`. Each kind accepts one ID prefix (§3). |
| **Processor** | The class that runs tasks of one kind. It checks whether the task is still needed, does the work and writes the result (§5). |
| **Live task** | A task in `queued` or `running`. |
| **Entity state** | The record's own state that users see (chapter `pending`, export `waiting`, …). It stays on the record and keeps its v1 meaning. The task holds only the execution state. |

How the two kinds of state relate:

| Entity state (v1 name) | Task that must exist |
|---|---|
| chapter `pending` | a `queued` `download` task for the chapter |
| chapter `downloading` | a `running` `download` task for the chapter |
| export `waiting` and buildable (v1 §6.5) | a `queued` `build` task for the export |
| export `building` | a `running` `build` task for the export |
| novel `syncing` (derived) | a `running` `refresh` task for the novel |
| every novel | exactly one live `refresh` task chain: always one `queued`, plus at most one `running` |

The database cannot enforce these pairs across tables. They are kept by two
rules:

- **One write path.** A change to an entity state and the matching task change
  happen in one transaction, inside one repo method. No other code writes
  `pending`, `downloading`, `waiting` or `building`.
- **Repair.** Hourly maintenance looks for violations, fixes them and logs each
  fix (§8).

## 3. Schema

### New table `tasks`

```sql
CREATE TABLE tasks (
    id            TEXT PRIMARY KEY,                -- 'TK' + 12 chars (v1 §3)
    kind          TEXT NOT NULL,                   -- 'refresh' | 'download' | 'build'
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
    claimed_by    TEXT,
    claimed_until TIMESTAMP,
    created_at    TIMESTAMP NOT NULL,
    started_at    TIMESTAMP,                       -- latest start
    finished_at   TIMESTAMP,
    duration_ms   INTEGER,                         -- finished_at − started_at of the last run
    CHECK (state IN ('queued', 'running', 'done', 'failed', 'skipped', 'cancelled')),
    CHECK ((kind, substr(entity_id, 1, 2)) IN
           (('refresh', 'NV'), ('download', 'CH'), ('build', 'EX'))),
    CHECK (state <> 'running' OR (claimed_by IS NOT NULL AND started_at IS NOT NULL)),
    CHECK (claimed_by IS NULL OR state IN ('queued', 'running')),  -- queued + claim = claimed, not started
    CHECK ((state IN ('done', 'failed', 'skipped', 'cancelled')) = (finished_at IS NOT NULL)),
    CHECK (state <> 'failed' OR error IS NOT NULL),
    <v1 _id_check('TK')>, <v1 _claim_check>
);
CREATE UNIQUE INDEX tasks_one_queued ON tasks (kind, entity_id) WHERE state = 'queued';
CREATE UNIQUE INDEX tasks_one_running ON tasks (kind, entity_id) WHERE state = 'running';
CREATE INDEX tasks_queue ON tasks (priority DESC, run_after, ordinal) WHERE state = 'queued';
CREATE INDEX tasks_by_entity ON tasks (entity_id, created_at);
CREATE INDEX tasks_by_novel ON tasks (novel_id);
CREATE INDEX tasks_finished ON tasks (finished_at) WHERE finished_at IS NOT NULL;
```

- One queued task per entity and kind: enqueueing again **merges** into the
  queued task instead of adding a second one (§4 `enqueue`). One running task
  per entity and kind: the same record is never worked on twice at once. A
  queued successor can wait while the current task runs. Refresh needs this,
  because "sync now" during a running sync means "sync again after this one"
  (this is v1's `sync_requested`).
- `novel_id` is there for the v1 lock order (§4 "Lock order") and for
  deleting a novel's tasks. Plain foreign key, no cascade (v1 §4).
- `payload` is `{}` for every kind in this spec. It exists for later request
  parameters, for example a forced full re-fetch.

### Changes to v1 tables

| Table | Removed | Now derived from tasks |
|---|---|---|
| `novels` | `next_refresh_at`, `sync_requested`, `claimed_by`, `claimed_until`, `ck_novels_claim`, index `novels_due_refresh` | `next_refresh_at` = `run_after` of the queued refresh task; `syncing` = a running refresh task exists |
| `chapters` | `queued_at`, `priority`, `claimed_by`, `claimed_until`, `ck_chapters_queued`, `ck_chapters_claim`, `ck_chapters_claim_state`, `ck_chapters_started`, index `chapters_queue` | `Chapter.queued_at` = the live download task's `created_at` |
| `exports` | `claimed_by`, `claimed_until`, `ck_exports_claim`, `ck_exports_building` | — |
| `events` | — | `ck_events_entity` also allows `TK` |

`novels.refresh_interval` stays. It is the novel's backoff state, not queue
state. `consumers` keeps its claim columns: the notifier is not a task (§1).

The API objects (v1 §5.2) do not change. `next_refresh_at`, `syncing` and
`queued_at` are read through a join on the live task.

### Configuration

- `NOVELHOG_BATCH_SIZE` is removed. The runner claims one task at a time (§6). If
  the variable is set, the worker starts anyway and logs `config_ignored` once.
- New constant `TASK_RETENTION = 7 d` (§8). Not configurable.

## 4. Queue (`repo/tasks.py`, `TaskQueue`)

### Ordering

Claim order is `priority DESC, run_after ASC, ordinal ASC`, among queued tasks
with `run_after <= now` and no claim, or a lapsed one. Base priorities keep
v1's order (v1 §6.1: refreshes, then builds, then downloads):

| Kind | Priority | `ordinal` | `run_after` on enqueue |
|---|---|---|---|
| `refresh` | 300 | 0 | the next refresh time (v1 §6.2), or now for a manual sync |
| `build` | 200 | 0 | now |
| `download`, auto-queued release (v1 `priority = 1`) | 110 | chapter position | now |
| `download`, user-queued | 100 | chapter position | now |

`ordinal` is copied from the chapter position when the task is enqueued. A later
sync that moves positions does not update it; order is a hint, not a contract.
For downloads, `run_after` plays the part of v1's `queued_at`, so a requeued task
keeps its place in line.

### Operations

All of these are methods on `TaskQueue`. Each method that takes `conn` writes
inside the caller's transaction, which already holds `lock_novel`. The others
are their own transaction.

| Method | Effect |
|---|---|
| `enqueue(conn, kind, entity_id, novel_id, *, priority, ordinal=0, run_after, emit_event=False, payload={}) -> TaskRow` | Inserts a queued task. If a queued task for `(kind, entity_id)` exists, merges into it: `run_after = min(old, new)`, `priority = max`, `emit_event = old OR new`; the claim and `created_at` stay. |
| `cancel(conn, kind, entity_id) -> bool` | Queued task → `cancelled` (claim cleared, `finished_at` set). A running task is not touched. |
| `delete_for_novel(conn, novel_id)` | Deletes every task of the novel (`DELETE /novels`, v1 §5.3). Writes no task events. |
| `claim(worker_id, ttl) -> TaskRow \| None` | One compare-and-set statement through `repo/claims.py` (v1 §4, unchanged shape). The task stays `queued`. Writes no events. Exempt from the lock order, like v1's batch claim. |
| `start(conn, task, worker_id) -> bool` | Fenced: queued and owned → `running`, `attempts + 1`, `started_at = now`, `error = NULL`. |
| `finish(conn, task, worker_id, outcome)` | Fenced: running and owned → `done`, `failed` (with `error`) or `skipped`, with `finished_at` and `duration_ms` set and the claim cleared. |
| `retry(conn, task, worker_id, *, run_after, error)` | Fenced: running and owned → `queued`, claim cleared, `run_after` set, `error` kept. If another task was queued for the same entity in the meantime, this one merges into it (enqueue rules) and the retried row → `cancelled`. |
| `release(task, worker_id)` | Fenced: queued, claimed by me → claim cleared. Used on stop. |
| `renew(task, worker_id, ttl) -> bool` | Heartbeat (v1 §4, unchanged). |
| `lapsed(now) -> list[TaskRow]` | Tasks whose claim lapsed (recovery, §8). |
| `prune(before)` | Deletes finished tasks with `finished_at < before`. |

Every fenced method changes nothing and returns `False` (or `None`) when the
claim was lost. The caller then throws its result away and logs it, as in v1.

### Lock order

A task belongs to its novel the same way chapters and exports do. Any
transaction that writes a task row (except `claim`, `renew` and `prune`, which
are exempt like v1's multi-novel statements) does three things in order:

1. takes `lock_novel(task.novel_id)` first;
2. writes the task and entity rows;
3. calls `events.append` last.

This is v1 §4 "Lock order", with tasks added as child rows.

## 5. Processors (`worker/tasks/`)

```
class TaskProcessor(Protocol):
    kind: ClassVar[TaskKind]
    def begin(self, conn, task) -> Begin            # inside start's transaction
    def run(self, task, ctx) -> Outcome             # network work, no transaction held
    def complete(self, conn, task, outcome) -> None # inside finish's or retry's transaction
    def recover(self, conn, task) -> None           # inside recovery's transaction (§8)
```

- `Begin` is either `Started`, or `Skip(reason)` when the task is no longer
  needed. The check runs inside `start`'s transaction, under `lock_novel`, so it
  is atomic with the state change. On `Skip`, the task goes straight to
  `skipped` and `run` is not called.
- `Outcome` is one of `Done(result)`, `Failed(error)`, or
  `Retry(run_after, error)` for an infrastructure error.
- The check at `begin` does **not** replace fencing. Every write in `complete`
  is still conditional on the task claim (`finish`/`retry` are fenced) and on
  the entity state, because the record can change while `run` is working. For
  example, the user can delete a novel mid-download.
- `ctx` holds the adapter, fetcher, storage, settings, stop event, heartbeat and
  `Reachability`, the same dependencies as v1's stages.

| Processor | `begin`: still needed when … / entity change on start | `complete` |
|---|---|---|
| `RefreshProcessor` | the novel exists / none (`syncing` turns on because the task is running) | v1 §6.2 `finish_sync` unchanged, plus: `Done` or `Failed` enqueue the successor refresh task with `run_after` = v1's `next_refresh_at`. A queued manual-sync task already exists → merge (`min`), so v1's "now if `sync_requested`" still holds. `Retry` keeps v1's infrastructure rule: `run_after = now + REFRESH_MIN_INTERVAL`, interval unchanged. |
| `DownloadProcessor` | the chapter is `pending` and has a position / `pending` → `downloading` | v1 §6.4 `complete` and `fail` unchanged. `Retry` (infrastructure error, or site unreachable below the threshold): `downloading` → `pending`, `run_after` kept. After `Done`/`Failed`: enqueue builds that are now ready (§5.1). |
| `BuildProcessor` | the export is `waiting` and buildable (v1 §6.5) / `waiting` → `building` | v1 §6.5 `finish` and `fail` unchanged. `Retry`: `building` → `waiting`, `run_after` kept. |

### 5.1 When a build task is enqueued

v1 polls for buildable exports (`claim_ready_export`). Here a build task is
enqueued, in the same transaction, by any write that can make an export
buildable:

- **Export created** (v1 §5.5): if it is already buildable.
- **Chapter downloaded or failed:** for each `waiting` export of the novel whose
  range contains the chapter, if it is now buildable.
- **Chapter dequeued or removed by a sync:** same as above. This includes v1's
  "a cancelled chapter that a waiting export needs fails the export", which
  stays unchanged.

The merge rule makes a duplicate enqueue harmless.

### 5.2 Callers that set `emit_event`

| Enqueue site | `emit_event` |
|---|---|
| `POST /novels` (first refresh) | true |
| `POST /novels/{id}/sync` (manual sync) | true |
| scheduled successor refresh | false |
| export build (§5.1) | true |
| chapter download (user-queued and auto-queued) | false. The chapter's `CH` events already show every step. |

## 6. Runner (`worker/runner.py`)

This replaces v1 §6.1's refresh, build and download stages. Recovery,
notifications, trash and maintenance stay where they are in the loop.

```
loop until stop requested:
    recover()                                    # v1 claims (consumers) + tasks (§8)
    behind = dispatch_notifications()            # unchanged (v1 §6.6)
    empty_trash()                                # unchanged
    if task = queue.claim(worker_id, CLAIM_TTL):
        run_task(task)
        continue
    if behind: continue
    maintenance()                                # + task retention and repair (§8)
    stop.wait(IDLE_SLEEP)

run_task(task):
    processor = processors[task.kind]
    in one transaction (lock_novel → rows → events):
        if not queue.start(task): log task_lost; return
        begin = processor.begin(task)
        if begin is Skip: queue.finish(task, skipped); log task_skipped; return
    with heartbeat(task):
        outcome = processor.run(task, ctx)       # exceptions → Failed('<Type>: <summary>')
    in one transaction:
        if outcome is Retry: queue.retry(...) else: queue.finish(...)
        processor.complete(task, outcome)        # nothing written if the fence failed
    log task_finished
    if outcome is Retry: raise to the loop's backoff (v1 §6.1, unchanged)
    if task.kind == download: stop.wait(request delay for the host)   # v1 §6.3 politeness
```

- The runner claims one task at a time, so an urgent refresh never waits behind
  a batch of downloads. Downloads therefore cost one claim statement each. That
  is accepted; batching is future work.
- On stop: a task that was claimed but not started is released. A running task
  finishes first (v1 §6.1, unchanged).
- Site-unreachable counting (v1 §6.1) is unchanged. `Reachability` stays per
  worker and in memory, and the processors decide between `Retry` and `Failed`
  exactly as v1's stages do.

### Logging

Every transition logs one structured line with `task_id`, `kind`, `entity_id`
and `attempts`:

- `task_started`
- `task_skipped` (`reason`)
- `task_finished` (`outcome`, `duration_ms`, `error`)
- `task_retry` (`run_after`)
- `task_lost`
- `task_recovered`
- `task_repaired` (`problem`)

## 7. Task events

- When a task with `emit_event = true` changes state (start, finish, retry,
  skip, cancel, recovery), the same transaction appends one `TK` event after the
  entity's own events. Claim and renew are not state changes and write no
  events.
- Payload: the `Task` object
  `{id, kind, entity_id, novel_id, state, attempts, error, run_after,
  created_at, started_at, finished_at, duration_ms}`.
  `payload` and the claim are not exposed.
- This spec adds no REST route for tasks. That deviates from v1's rule that an
  event payload is "as `GET` would return it"; the `Task` model in
  `domain.payloads` is the single definition.
- **SSE** sends `TK` events like any other. The web client ignores entity
  prefixes it does not know. Nothing in the v1 UI uses `TK` events yet.
- **The notifier** ignores `TK` events. Notification rules (v1 §6.6, §8.6) do not
  change.
- **Pruning** (v1 §4) keeps its "newest row of each `entity_id`" exception for
  `NV`, `CH` and `EX` only. `TK` events are pruned purely by age, so pruned tasks
  leave no rows behind.

## 8. Recovery, retention, repair

- **Recovery** (in `recover()`, each lapsed task in its own transaction under
  `lock_novel`):
  - a queued task with a lapsed claim: the claim is cleared;
  - a running task with a lapsed claim: back to `queued` with `run_after`
    kept, then `processor.recover` applies v1's reset: chapter `downloading` →
    `pending`; export `building` → `waiting` with an `EX` event; refresh → an
    `NV` event so `syncing` turns off;
  - if a queued task for the same entity already exists, the recovered one
    merges into it and becomes `cancelled`.
- **Retention:** maintenance deletes finished tasks whose `finished_at` is older
  than `TASK_RETENTION` (7 d).
- **Repair:** maintenance checks the §2 pairs. Each violation is fixed in its
  own transaction and logged as `task_repaired`:

  | Problem | Fix |
  |---|---|
  | chapter `pending` with no live download task | enqueue (user-queued priority, `run_after` = now) |
  | chapter `downloading` with no running task | → `pending`, enqueue |
  | export `waiting` and buildable, no live build task | enqueue |
  | export `building` with no running build task | → `waiting`, enqueue |
  | novel with no queued refresh task | enqueue with `run_after = now` |
  | live task whose entity is gone or in a terminal state | → `cancelled` |

## 9. Migration

One Alembic revision, following v1 §4's rules (both databases; SQLite batch
mode with foreign keys off). Stop the workers before upgrading.

1. Create `tasks`. Add `TK` to `ck_events_entity`.
2. Fill it from the v1 columns, dropping all claims:
   - each novel → one queued refresh task: `run_after = now` if
     `sync_requested`, else `next_refresh_at`;
   - each chapter `pending` or `downloading` → one queued download task:
     `run_after = queued_at`, priority 110 or 100 from `priority`,
     `ordinal = position`. A `downloading` chapter becomes `pending`;
   - each export `building` → `waiting`; each `waiting` export that is
     buildable → one queued build task.
3. Drop the columns, checks and indexes listed in §3.

`emit_event` is false for every migrated task. The downgrade restores the
columns from the live tasks (`next_refresh_at`, `sync_requested`, `queued_at`,
`priority`), then drops `tasks` and its `TK` events.

## 10. Code layout

| Path | Change |
|---|---|
| `repo/schema.py`, `repo/migrations/` | `tasks` table, column drops (§3, §9) |
| `repo/tasks.py` | new: `TaskQueue` (§4) |
| `repo/work.py` | loses `claim_batch` and chapter/novel/export recovery; keeps consumer recovery |
| `repo/chapters.py`, `novels.py`, `exports.py` | entity transitions call `TaskQueue` in the same transaction; derived fields via join |
| `domain/payloads.py` | `TaskPayload`; `NovelPayload`/`ChapterPayload` read derived fields |
| `worker/tasks/{base,refresh,download,build}.py` | new: protocol and the three processors; they take over the bodies of `worker/refresh.py`, `downloads.py`, `builds.py` |
| `worker/runner.py` | new: `run_task`, processor registry |
| `worker/loop.py` | stages 4–6 replaced by the runner (§6) |
| `worker/maintenance.py` | retention and repair (§8) |
| `config.py` | drops `batch_size` |

## 11. Testing

Same layout and gates as v1 §13. The v1 worker integration tests
(`tests/integration/worker/`) are the behaviour contract. They are changed only
where they read removed columns directly, and they must keep asserting the same
outcomes.

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
        - returns the queued task with highest priority, then earliest run_after, then lowest ordinal
        - skips tasks with run_after in the future
        - takes a task whose claim lapsed
        - two workers racing claim different tasks or one gets None (SQLite and Postgres)
        - writes no events
    start
        When queued and owned
            - sets running, attempts + 1, started_at, clears error
        When the claim was lost or the task was cancelled
            - changes nothing and returns False
    finish
        - sets done / failed (error) / skipped with finished_at, duration_ms, claim cleared
        When the claim was lost
            - changes nothing
    retry
        - returns the task to queued with run_after and error kept, claim cleared
        When another queued task for the entity appeared meanwhile
            - merges into it and cancels the retried row
    cancel
        - turns a queued task into cancelled; leaves a running task alone
    delete_for_novel
        - removes every task of the novel, no TK events
    prune
        - deletes finished tasks older than TASK_RETENTION, keeps live and recent ones
Task events
    When emit_event is true
        - start, finish, retry, skip, cancel and recovery each append one TK event after the entity's events
        - payload is the Task object without payload and claim
    When emit_event is false
        - no TK events
    Notifier
        - ignores TK events
    Pruning
        - TK events are pruned by age even when they are an entity's newest row
Runner.run_task
    When begin says Skip
        - task becomes skipped, run is not called, entity unchanged
    When run returns Done
        - task done, processor.complete writes the result in the same transaction
    When run raises
        - task failed with '<Type>: <summary>', traceback logged
    When run returns Retry
        - task queued with run_after, entity reset, loop backs off
    When the claim is lost during run
        - result discarded, task_lost logged, entity not overwritten
    On stop
        - a claimed, unstarted task is released; a running task finishes
    After a download task
        - waits the host's request delay
RefreshProcessor
    - "still needed" = the novel exists
    On Done or Failed
        - applies v1 finish_sync and enqueues the successor at v1's next_refresh_at
    When a manual sync was queued during the run
        - the successor merges and runs now (v1 sync_requested)
    On Retry
        - successor at now + REFRESH_MIN_INTERVAL, interval unchanged
    NV payload
        - syncing is true while the task runs; next_refresh_at is the queued task's run_after
DownloadProcessor
    - "still needed" = the chapter is pending and positioned; start turns it downloading
    When the chapter was dequeued after claim
        - task skipped (or not startable, because cancel already cancelled it)
    On Done / Failed
        - v1 complete / fail; enqueues builds of now-buildable waiting exports
    On Retry
        - chapter back to pending, run_after kept (keeps its place in line)
    Site unreachable
        - below SITE_UNREACHABLE_RETRIES → Retry; at the threshold → Failed "Site unreachable: <message>"
BuildProcessor
    - "still needed" = the export is waiting and buildable; start turns it building
    On Done / Failed
        - v1 finish / fail
    When the export was deleted meanwhile
        - result discarded, stored key trashed
Build enqueueing
    - export created already buildable → build task queued in the same transaction
    - last missing chapter downloaded → build task queued
    - needed chapter cancelled → export failed (v1), no build task
Recovery
    - lapsed running download → queued, chapter pending, run_after kept
    - lapsed running build → queued, export waiting with an EX event
    - lapsed running refresh → queued, NV event (syncing off)
    - lapsed claimed queued task → claim cleared
Repair (maintenance)
    - each problem in §8's table is fixed and logged as task_repaired
    - a consistent database produces no repairs
Migration
    - v1 rows become the tasks of §9; downloading chapters and building exports come back pending / waiting
    - downgrade restores next_refresh_at, sync_requested, queued_at, priority
    - autogenerate against schema.metadata reports no differences
API (unchanged contract)
    - Novel.next_refresh_at, Novel.syncing and Chapter.queued_at have the same values as before
```

## 12. Out of scope (later)

- **Pausing an unreachable host** (the open v1 question "M2"). With tasks this
  becomes "push `run_after` forward for that host's queued download tasks", so
  the chapters stay queued.
- A task list in the UI or REST API, and task kinds without a novel.
- Batch claims for downloads, and per-host queues or concurrency.
- Task dependencies in general. Builds use the enqueue rule in §5.1 instead.

## 13. Open questions

1. Is `TASK_RETENTION = 7 d` right, or should failed tasks be kept longer than
   successful ones?
2. Are the `emit_event` defaults in §5.2 right? Should scheduled refreshes emit
   too?
