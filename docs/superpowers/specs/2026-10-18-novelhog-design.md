# novelhog — Design Spec

- **Date:** 2026-10-18 (revised 2026-10-19)
- **Status:** Approved in brainstorming; pending written-spec review
- **Repo:** `~/dev/novel-shelf` (directory name is incidental; the project is `novelhog`)

## 1. Purpose

novelhog is a self-hosted web UI around
[`web-novel-scraper`](https://github.com/ImagineBrkr/web-novel-scraper). It lets
one person keep a library of web novels, have new chapters discovered and
downloaded in the background, get push notifications for new releases, and export
chapter ranges to EPUB for phones and e-readers.

### Success criteria

1. Add a novel from any URL whose host `web-novel-scraper` supports; title and
   cover are detected and the TOC is imported in the background.
2. Queue a single chapter, a list or a range; downloads continue with the browser
   closed and progress is visible live from any device.
3. Every novel's TOC is re-checked automatically, more often for active novels
   and down to monthly for dormant ones.
4. For followed novels, new chapters download automatically and a push
   notification arrives even when no novelhog tab is open.
5. Export a downloaded range to an EPUB with a cover, without ads or empty
   paragraphs, and download it from any device.
6. `docker compose up` next to an existing FlareSolverr, behind the user's own
   HTTPS reverse proxy, is the whole deployment.

### In scope (v1)

- Library, add novel from a URL (details filled in by the first import),
  automatic and manual TOC sync with adaptive interval.
- Follow switch: auto-download of new chapters + Web Push notifications.
- Chapter queueing (single, list, range), dequeue, retry, live progress.
- "New" badges for chapters released since the novel page was last opened.
- EPUB export and download; cover detection and display.
- Installable PWA (required for push on iOS).

### Out of scope (v1)

- In-browser reader, full-text search, metadata editing, deleting novels.
- Authentication (no auth; the reverse proxy can add it).
- TLS termination (done by the user's reverse proxy).
- Manual cover upload.
- Splitting site "pages" into the novel's real chapters (titles are shown as the
  site provides them, e.g. fanmtl's "Page N").
- S3 static backend (interface exists; only the local backend ships).
- Worker wake-up ("poke"), see §12.

## 2. Architecture

```mermaid
flowchart TB
    browser["Browser<br/>React PWA + service worker"]
    proxy["User's HTTPS reverse proxy"]

    subgraph web["novelhog-web container"]
        caddy["Caddy :8080<br/>/ → SPA · /sw.js · /static/*"]
        api["API (FastAPI, stateless)<br/>/api/v1 · SSE"]
    end

    subgraph data["/data volume"]
        db[("Database<br/>SQLite app.db (default) or Postgres")]
        static[["static/<br/>covers, exports"]]
    end

    subgraph wk["novelhog-worker container"]
        worker["Worker<br/>syncs · downloads · export builds<br/>notification delivery · pruning"]
    end

    flaresolverr["FlareSolverr"]
    sites["Novel sites"]
    channels["Notification channels<br/>Web Push (v1) · Telegram (later)"]

    browser --> proxy --> caddy
    caddy -- "/api/*" --> api
    caddy -. "reads" .-> static
    api -- "novelhog.repo" --> db
    worker -- "novelhog.repo" --> db
    worker --> static
    worker --> flaresolverr --> sites
    worker --> channels
    channels -. "push" .-> browser
```

Responsibilities are split by concern, not by load:

| Component | Responsibility |
|---|---|
| `novelhog-web` (container) | Everything that faces people, and nothing about novel sites. Runs two processes: **Caddy** (the only exposed port) and the **API**. |
| Caddy (in `novelhog-web`) | Serves the built SPA at `/` (with SPA fallback to `index.html`), `sw.js` and `manifest.webmanifest` at the root, and local static files from `/data/static` at `/static`; reverse-proxies `/api/*` to the API on `127.0.0.1:8000` with response buffering off (SSE). Owns cache and download headers (§8.3). |
| API (in `novelhog-web`) | FastAPI app, `/api/v1` only: REST, SSE stream, settings (including generating the VAPID key pair into the database), Web Push subscription registration. Stateless: no local files of its own, no background jobs; every instance is interchangeable. Never serves files and never talks to novel sites, FlareSolverr or the scraper. |
| `worker` | Everything about novels. Separate container from the same image. The only user of the scraper and FlareSolverr; uses the scraper as a stateless library (no scraper files, no shared filesystem needed between workers). Publishes supported hosts, runs TOC syncs (initial import included), downloads chapters, builds exports, delivers notifications through channels, prunes the event log it writes. |
| SPA | React app + service worker, built to static files baked into the image at `/srv/web` and served by Caddy (single origin, no CORS). |
| `novelhog.repo` | The database interface: a standalone package built on SQLAlchemy Core (no FastAPI, scraper or image imports) used by `api`, `worker` and future services such as a Telegram bot. |
| Database (SQLite by default, Postgres supported) | Single source of truth: novels, chapters, chapter content (Markdown), exports, hosts, settings, subscriptions, the event log and its consumers. Accessed only through `novelhog.repo`. |
| Static storage | Covers and export files behind `StaticStorage`; v1 backend is local disk under `/data/static`. |

### Deployment

- `compose.yaml` with services `novelhog-web` and `novelhog-worker`, one image,
  different commands (`novelhog web`, `novelhog worker`).
- `novelhog web` starts the API (uvicorn on `127.0.0.1:8000`) and Caddy
  (`:8080`, config in `docker/Caddyfile`) under `tini`; if either process exits,
  the other is stopped and the container exits so Docker restarts it.
- Both services mount the `/data` volume (`novelhog-web` read-only except
  `app.db`); only `novelhog-worker` joins FlareSolverr's Docker network.
- `novelhog-web` publishes one port (default `8080`) for the user's HTTPS
  reverse proxy.
- The API process needs only FastAPI, the repository layer and `py-vapid` for key
  generation; the scraper, FlareSolverr access, Pillow, `ebooklib` and
  `pywebpush` are used only by the worker. One image still serves both for
  simplicity; the import-linter contract (§13) keeps the boundary.
- Both `NOVELHOG_PUBLIC_URL` and `NOVELHOG_VAPID_SUBJECT` are set on both
  services: the API reports whether the `webpush` channel is enabled, the worker
  builds links and VAPID claims.
- The user's reverse proxy must terminate HTTPS (required for service workers and
  push) and must not buffer `/api/v1/events`. The README includes Caddy and
  nginx snippets. Cache headers come from the built-in Caddy.
- Scaling: `novelhog-web` (stateless) and `novelhog-worker` can both be scaled on
  one host with SQLite; multi-host clustering needs Postgres (`NOVELHOG_DATABASE_URL`, §4, §12).

## 3. Identifiers

- Format: two-letter uppercase type prefix + 12 random characters from
  `[a-z0-9]`, generated with `secrets`.
  - `NV` novel, `CH` chapter, `EX` export, `SB` subscription.
    Example: `CHk3x9q2m7b4wz`.
- 36¹² ≈ 4.7·10¹⁸ (~62 bits). Primary keys are unique; an insert that collides is
  retried with a fresh ID.
- IDs carry no ordering. Every listing orders by an explicit column
  (`created_at`, `number`).
- An ID with the wrong prefix or format in a path or body is treated as unknown →
  `404`.

## 4. Database schema

The database is accessed through **SQLAlchemy Core 2.x** (no ORM) inside
`novelhog.repo`, so the same code runs on **SQLite ≥ 3.45** (default) and
**PostgreSQL ≥ 16**. The database is chosen by `NOVELHOG_DATABASE_URL` (§9).

- The schema is defined once as SQLAlchemy `Table` objects in
  `novelhog/repo/schema.py`; that module is the contract. The DDL below is the
  logical schema, written in its SQLite form; `schema.py` reproduces every
  table, column, default, foreign key, unique constraint, check and index,
  applying the portability mapping after the DDL.
- Migrations are **Alembic** revisions under `migrations/`, applied by the API
  and the worker at startup (`alembic upgrade head`, serialised by the
  database). Each revision must run on both SQLite (batch mode) and Postgres.
- SQLite connections use WAL, `foreign_keys = ON` and `busy_timeout = 5000`.

```sql
CREATE TABLE novels (
    id                TEXT PRIMARY KEY
                      CHECK (length(id) = 14 AND substr(id, 1, 2) = 'NV'
                             AND substr(id, 3) NOT GLOB '*[^a-z0-9]*'),
    title             TEXT,                      -- display only; NULL until the initial import
    host              TEXT NOT NULL REFERENCES hosts (host),
    toc_url           TEXT NOT NULL UNIQUE,
    import_error      TEXT,                      -- set if the initial import failed
    cover_key         TEXT,                      -- static storage key; NULL = no cover
    followed          INTEGER NOT NULL DEFAULT 1 CHECK (followed IN (0, 1)),
    refresh_interval  INTEGER NOT NULL CHECK (refresh_interval > 0),  -- seconds, current
    next_refresh_at   TEXT NOT NULL,
    last_refreshed_at TEXT,                      -- NULL = initial import not done yet
    seen_at           TEXT,                      -- last time the novel page was opened
    claimed_by        TEXT,                      -- worker id holding the novel claim
    claim_expires_at  TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    CHECK ((claimed_by IS NULL) = (claim_expires_at IS NULL))
) STRICT;

CREATE INDEX novels_due_refresh ON novels (next_refresh_at);

CREATE TABLE chapters (
    id           TEXT PRIMARY KEY
                 CHECK (length(id) = 14 AND substr(id, 1, 2) = 'CH'
                        AND substr(id, 3) NOT GLOB '*[^a-z0-9]*'),
    novel_id     TEXT NOT NULL REFERENCES novels (id) ON DELETE CASCADE,
    number       INTEGER NOT NULL CHECK (number >= 1),   -- 1-based TOC position
    url          TEXT NOT NULL,
    title        TEXT,                                   -- set after download
    state        TEXT NOT NULL DEFAULT 'not_requested'
                 CHECK (state IN ('not_requested', 'pending', 'downloading',
                                  'downloaded', 'error')),
    has_images   INTEGER NOT NULL DEFAULT 0 CHECK (has_images IN (0, 1)),
    released_at  TEXT,                                   -- NULL = part of initial import
    queued_at    TEXT,
    claimed_by   TEXT,                                   -- worker id while downloading
    claim_expires_at TEXT,
    updated_at   TEXT NOT NULL,
    UNIQUE (novel_id, number),
    CHECK ((state = 'pending') = (queued_at IS NOT NULL)),
    CHECK ((claimed_by IS NULL) = (claim_expires_at IS NULL)),
    CHECK ((state = 'downloading') = (claimed_by IS NOT NULL))
) STRICT;

CREATE INDEX chapters_queue    ON chapters (queued_at) WHERE state = 'pending';
CREATE INDEX chapters_claim    ON chapters (novel_id, state, number);
CREATE INDEX chapters_released ON chapters (novel_id, released_at)
    WHERE released_at IS NOT NULL;

CREATE TABLE chapter_contents (
    chapter_id TEXT PRIMARY KEY REFERENCES chapters (id) ON DELETE CASCADE,
    markdown   TEXT NOT NULL,                    -- images inline as data: URIs
    fetched_at TEXT NOT NULL
) STRICT;

CREATE TABLE exports (
    id          TEXT PRIMARY KEY
                CHECK (length(id) = 14 AND substr(id, 1, 2) = 'EX'
                       AND substr(id, 3) NOT GLOB '*[^a-z0-9]*'),
    novel_id    TEXT NOT NULL REFERENCES novels (id) ON DELETE CASCADE,
    chapter_from INTEGER NOT NULL CHECK (chapter_from >= 1),
    chapter_to   INTEGER NOT NULL,
    mime         TEXT NOT NULL,                  -- e.g. 'application/epub+zip'
    filename     TEXT NOT NULL,                  -- '<Title> Chapters <from>-<to>.<ext>'
    state        TEXT NOT NULL DEFAULT 'waiting'
                 CHECK (state IN ('waiting', 'building', 'ready', 'failed')),
    error        TEXT,                           -- build failure message, 'failed' only
    storage_key  TEXT UNIQUE,                    -- set when ready
    size_bytes   INTEGER CHECK (size_bytes >= 0),-- set when ready
    waited       INTEGER NOT NULL DEFAULT 0 CHECK (waited IN (0, 1)), -- range was incomplete at request time
    claimed_by   TEXT,                           -- worker id while building
    claim_expires_at TEXT,
    created_at   TEXT NOT NULL,
    ready_at     TEXT,
    CHECK (chapter_to >= chapter_from),
    CHECK ((state = 'ready') = (storage_key IS NOT NULL AND size_bytes IS NOT NULL)),
    CHECK ((state = 'failed') = (error IS NOT NULL)),
    CHECK ((claimed_by IS NULL) = (claim_expires_at IS NULL)),
    CHECK ((state = 'building') = (claimed_by IS NOT NULL))
) STRICT;

CREATE INDEX exports_by_novel ON exports (novel_id, created_at);
CREATE INDEX exports_waiting  ON exports (created_at) WHERE state = 'waiting';

CREATE TABLE hosts (                             -- published by the worker from the scraper
    host           TEXT PRIMARY KEY,             -- e.g. 'fanmtl.com'
    has_pagination INTEGER NOT NULL CHECK (has_pagination IN (0, 1)),
    updated_at     TEXT NOT NULL
) STRICT;

CREATE TABLE host_slots (                        -- download concurrency per site
    host             TEXT NOT NULL REFERENCES hosts (host) ON DELETE CASCADE,
    slot             INTEGER NOT NULL CHECK (slot >= 1), -- 1..HOST_CONCURRENCY
    claimed_by       TEXT,                       -- worker downloading from this host
    claim_expires_at TEXT,
    PRIMARY KEY (host, slot),
    CHECK ((claimed_by IS NULL) = (claim_expires_at IS NULL))
) STRICT;

CREATE TABLE settings (                          -- small key/value store
    key        TEXT PRIMARY KEY,                 -- e.g. 'vapid'
    value      TEXT NOT NULL CHECK (json_valid(value)),
    updated_at TEXT NOT NULL
) STRICT;

CREATE TABLE subscriptions (                     -- where notifications are delivered, any channel
    id         TEXT PRIMARY KEY
               CHECK (length(id) = 14 AND substr(id, 1, 2) = 'SB'
                      AND substr(id, 3) NOT GLOB '*[^a-z0-9]*'),
    channel    TEXT NOT NULL,                    -- 'webpush' (v1); 'telegram', … later
    address    TEXT NOT NULL CHECK (json_valid(address)), -- channel-specific
    address_key TEXT NOT NULL,                   -- channel-specific identity, e.g. push endpoint
    label      TEXT,                             -- e.g. user agent, chat name
    created_at TEXT NOT NULL,
    UNIQUE (channel, address_key)
) STRICT;

CREATE TABLE events (                            -- the event log; every reader keeps a cursor
    seq       INTEGER PRIMARY KEY AUTOINCREMENT, -- position in the log; SSE event id
    novel_id  TEXT NOT NULL,                     -- for ?novel_id filtering
    entity_id TEXT NOT NULL                      -- NV…/CH…/EX…; prefix = event type
              CHECK (substr(entity_id, 1, 2) IN ('NV', 'CH', 'EX')),
    payload   TEXT NOT NULL CHECK (json_valid(payload)),
    at        TEXT NOT NULL
) STRICT;

CREATE INDEX events_by_novel  ON events (novel_id, seq);
CREATE INDEX events_by_entity ON events (entity_id, seq);
CREATE INDEX events_by_time   ON events (at);

CREATE TABLE consumers (                         -- durable readers of the event log
    name       TEXT PRIMARY KEY,                 -- 'notifier' (v1); 'telegram', … later
    last_seq   INTEGER NOT NULL DEFAULT 0,       -- last event fully processed
    claimed_by TEXT,                             -- process currently reading
    claim_expires_at TEXT,
    updated_at TEXT NOT NULL,
    CHECK ((claimed_by IS NULL) = (claim_expires_at IS NULL))
) STRICT;
```

**Portability mapping** (SQLite DDL above → `schema.py`):

| SQLite form | Portable form in `schema.py` |
|---|---|
| `STRICT` tables | dropped; column types enforced by SQLAlchemy types and the application |
| ID `CHECK (… GLOB …)` | `CHECK (length(id) = 14 AND substr(id, 1, 2) = '<prefix>')`; full format checked by `ids.is_valid` |
| `TEXT` timestamps | `DateTime(timezone=True)`; values are timezone-aware UTC (`TEXT` ISO-8601 in SQLite, `timestamptz` in Postgres) |
| `TEXT … CHECK (json_valid(…))` | `JSON` (`TEXT` in SQLite, `jsonb` in Postgres) |
| `INTEGER … CHECK (x IN (0, 1))` | `Boolean` |
| `INTEGER PRIMARY KEY AUTOINCREMENT` (`events.seq`) | `BigInteger` identity / autoincrement primary key |
| partial indexes (`WHERE …`) | `Index(…, sqlite_where=…, postgresql_where=…)` |
| `"end"` and other reserved words | avoided (already `chapter_from`/`chapter_to`) |

**Claims** use optimistic compare-and-set updates, identical on both
databases; there is no `SELECT … FOR UPDATE`.

Every claimable table (`novels`, `chapters`, `exports`, `consumers`,
`host_slots`) has the same two columns, set and cleared together:

| Column | Meaning |
|---|---|
| `claimed_by` | worker or process ID owning the row; `NULL` = free |
| `claim_expires_at` | when the claim lapses; renewing pushes it forward |

A row is **available** when `claimed_by IS NULL OR claim_expires_at < :now`.
Eligibility (what kind of row may be claimed: due novels, pending chapters,
complete waiting exports, a named consumer) is a separate, table-specific
condition. Every claim goes through `repo/claims.py`, which issues one
statement of this shape:

```
UPDATE <table>
SET claimed_by = :me, claim_expires_at = :now + :ttl [, state = …]
WHERE <key> IN (SELECT <key> FROM <table>
                WHERE <eligible> AND (claimed_by IS NULL OR claim_expires_at < :now)
                ORDER BY <priority> LIMIT :n)
  AND (claimed_by IS NULL OR claim_expires_at < :now)
RETURNING <columns>
```

Renewal and release are equally uniform: renewing sets
`claim_expires_at = :now + :ttl` where `claimed_by = :me`; releasing sets both
columns to `NULL` where `claimed_by = :me`. Recovery finds rows with
`claimed_by IS NOT NULL AND claim_expires_at < :now` and applies the
table-specific reset (chapter → `pending`, export → `waiting`, novel,
consumer and host slot → just freed).

- The outer `WHERE` repeats the "free or expired" condition. SQLite executes
  write statements one at a time; Postgres (`READ COMMITTED`) re-evaluates the
  outer `WHERE` against the newest row version after a concurrent update. Either
  way a row is claimed by exactly one worker, and a losing worker's statement
  matches nothing.
- `RETURNING` tells the caller which rows it now owns.
- A single-row claim that returns nothing while eligible rows remain is retried
  up to 3 times, picking the next candidate.
- Expiry compares against the claiming process's clock; hosts are assumed to be
  NTP-synchronised, which is safe with `CLAIM_TTL` measured in minutes.
- Ownership lives only in these columns; no database lock is held while work is
  done, so a slow download never blocks another worker.
- On SQLite, write transactions start with `BEGIN IMMEDIATE` so a transaction
  that reads before it writes waits for the write lock instead of failing with
  "database is locked". This is a connection setting, not part of claiming.

Notes:

- **Event rows are state snapshots.** Every state-changing write appends one row
  per changed entity to `events` in the same transaction; its payload is the
  entity as it is after the change (the API object, built by typed models in
  `domain`), plus optional facts about what just happened (e.g. a novel's
  `released`, §6.2). Readers: the SSE stream (stateless, per connection, from
  `Last-Event-ID`) and durable consumers with a row in `consumers`.
- Pruning: the worker, which writes `events`, deletes rows older than 24 h on its
  idle passes, **except the newest row of each `entity_id`** (so every novel,
  chapter and export keeps its latest state) **and except rows after the
  slowest consumer's `last_seq`**. A consumer more than 7 days behind is moved
  forward to the 7-day boundary with a warning, so a dead consumer cannot grow
  the log forever.
- Error messages are not stored on `chapters`. A chapter that fails gets
  `state = 'error'` and a `CH` event whose payload carries `"error": "<message>"`.
  `Chapter.error` in the API is read from the chapter's newest event when its
  state is `error`, and is `null` otherwise. Re-queuing writes a new event, so
  the old message stops being current without any clearing.
- The VAPID key pair lives in `settings` under `vapid`
  (`{"public_key", "private_key"}`). The API creates it on startup when it is
  missing, with an insert that is a no-op if another instance won the race, and
  never regenerates it. The worker reads it to deliver Web Push.
- The database is the only persistence for novels and chapters. The scraper is
  used as a stateless library: novelhog never creates scraper `Novel` objects,
  never writes `main.json` or other scraper files, and workers need no shared
  filesystem for scraping. `title` is display only.
- `ON DELETE CASCADE` is declared now so that deleting a novel later needs only a
  route.

### Derived values (not stored)

| Value | Definition |
|---|---|
| `total_pages` | count of chapters |
| `downloaded_pages` | count with `state = 'downloaded'` |
| `pending_pages` | count with `state IN ('pending', 'downloading')` |
| `error_pages` | count with `state = 'error'` |
| `new_pages` | count with `released_at IS NOT NULL AND (seen_at IS NULL OR released_at > seen_at)` |
| `syncing` | `next_refresh_at <= now` or the novel is claimed by a worker running a sync |
| `cover_url`, `Export.url` | `STATIC_BASE_URL` + storage key |

## 5. API

Base path `/api/v1`. JSON in/out. OpenAPI generated by FastAPI.

### 5.1 Routes

```
GET    /api/v1/settings
GET    /api/v1/hosts
GET    /api/v1/novels
POST   /api/v1/novels
GET    /api/v1/novels/{novel_id}
PATCH  /api/v1/novels/{novel_id}
POST   /api/v1/novels/{novel_id}/sync
GET    /api/v1/novels/{novel_id}/chapters
PATCH  /api/v1/novels/{novel_id}/chapters
GET    /api/v1/novels/{novel_id}/exports
POST   /api/v1/novels/{novel_id}/exports
DELETE /api/v1/novels/{novel_id}/exports/{export_id}
POST   /api/v1/webpush/subscriptions
GET    /api/v1/subscriptions
DELETE /api/v1/subscriptions/{subscription_id}
GET    /api/v1/events
```

Static files (covers, EPUBs) are not API routes; they are addressed by URLs built
from `STATIC_BASE_URL` (§8.3).

### 5.2 Objects

**Host**: `{host: string, has_pagination: bool}`, read from the `hosts` table
(published by the worker from the installed scraper's decode guide).

**Novel**

```
id                 string          NV…
title              string | null   null until the initial import
host               string
toc_url            string
import_error       string | null   initial import failed; cleared by a later successful sync
cover_url          string | null
followed           bool
syncing            bool
refresh_interval   int             seconds, current adaptive interval
next_refresh_at    string
last_refreshed_at  string | null
total_pages        int
downloaded_pages   int
pending_pages      int
error_pages        int
new_pages          int
created_at         string
updated_at         string
```

**Chapter**

```
id           string   CH…
number       int
url          string
title        string | null
state        "not_requested" | "pending" | "downloading" | "downloaded" | "error"
error        string | null    from the newest event, only in "error" state
error_at     string | null    time of that event, only in "error" state
has_images   bool
released_at  string | null
queued_at    string | null    only in "pending" state
updated_at   string
```

**Export**

```
id           string   EX…
novel_id     string
filename     string
chapter_from int
chapter_to   int
format       "epub"           v1; later "pdf", "fb2", …
mime         string           e.g. "application/epub+zip"
state        "waiting" | "building" | "ready" | "failed"
progress     {downloaded: int, total: int}      chapters of the range downloaded
blocked      {errors: int[], not_queued: int[]} chapter numbers holding a waiting export back
error        string | null    only in "failed"
waited       bool             the range was incomplete when the export was requested
size_bytes   int | null       set when ready
url          string | null    set when ready
created_at   string
ready_at     string | null
```

`progress` and `blocked` are computed from the chapters of the range; `blocked`
is empty unless the export is `waiting`.

**Chapter selector** (`chapters` field): `int[] | {start: int, end: int}`.
The range is inclusive. A list must be non-empty; duplicates are ignored; every
number must be in `1..total_pages`. A range requires `start ≤ end`, both in
bounds, no extra keys.

**Error body**: `{"error": {"code": string, "message": string, "details"?: object}}`.

### 5.3 Novels

| Route | Request | Response | Errors |
|---|---|---|---|
| `GET /hosts` | — | `Host[]` | — |
| `GET /novels` | — | `Novel[]` by `created_at` desc | — |
| `POST /novels` | `{toc_url, followed?: bool = true}` | `201 Novel` immediately: `title = null`, no chapters, `syncing = true` | `409 novel_exists` (same `toc_url`; `details.novel_id`), `422 unsupported_host` |
| `GET /novels/{id}` | — | `Novel` | `404` |
| `PATCH /novels/{id}` | `{followed?: bool, seen?: true, title?: string}` | `Novel` | `404`, `422` |
| `POST /novels/{id}/sync` | — | `202 Novel` | `404` |

- `POST /novels` only writes the database: it checks the URL's host against the
  `hosts` table, then inserts the novel with `refresh_interval =
  REFRESH_MIN_INTERVAL` and `next_refresh_at = now`. The worker's initial import
  fills in title, cover and chapters; a failure sets `import_error`.
- `PATCH {title}` renames the novel (display only, non-empty, trimmed). A later
  sync never overwrites a title set by the user.
- `POST /sync` sets `next_refresh_at = now`. The result arrives over SSE.
- `PATCH {seen: true}` sets `seen_at = now` (resets `new_pages`). The UI sends it
  when the novel page is opened.
- `PATCH {followed}` only changes the flag; it does not change the schedule.

### 5.4 Chapters

`GET /novels/{id}/chapters?offset=0&limit=1000&state=<state>` →
`{items: Chapter[], total: int}` ordered by `number`.

`PATCH /novels/{id}/chapters`

```
request:  {state: "pending" | "not_requested", chapters: <selector>}
response: {updated: int[], skipped: [{number: int, state: <current state>}]}
```

| From | To | Effect |
|---|---|---|
| `not_requested`, `error` | `pending` | Sets `queued_at` |
| `pending` | `not_requested` | Clears `queued_at` |

Chapters in any other state are listed in `skipped`. Requesting `downloading`,
`downloaded` or `error` → `422`. Each updated chapter appends a `chapter` event.

Chapter states (solid: API, via `PATCH` or `POST /exports`; dashed labels:
worker):

```mermaid
stateDiagram-v2
    [*] --> not_requested: TOC sync (initial import, unfollowed)
    [*] --> pending: TOC sync (followed, later release)
    not_requested --> pending: queue
    error --> pending: retry / queue
    pending --> not_requested: dequeue
    pending --> downloading: worker claims batch
    downloading --> downloaded: worker stores Markdown
    downloading --> error: worker fetch/convert fails
    downloading --> pending: worker recovery / shutdown requeue
```

### 5.5 Exports

| Route | Request | Response | Errors |
|---|---|---|---|
| `GET /novels/{id}/exports` | — | `Export[]` by `created_at` desc | `404` |
| `POST /novels/{id}/exports` | `{chapters: {start, end}, format?: "epub"}` | `201 Export`; an identical export (same range and format) that is still `waiting` or `building` is returned instead with `200` | `404`, `422` (list selector, unsupported format) |
| `DELETE /novels/{id}/exports/{export_id}` | — | `204`; only for `waiting` exports | `404`, `409` (already building or finished) |

- One export is one file. Several books are several exports with their own
  ranges; there is no chapters-per-book option.
- `POST` is asynchronous. In one transaction it moves every chapter of the range
  that is `not_requested` or `error` to `pending` (same rules and events as
  `PATCH /chapters`) and creates the export in `waiting`, with `waited = 1` if
  any chapter of the range was not yet `downloaded`.
- The worker builds it once every chapter of the range is `downloaded` (§6.5).
  If the range was already complete, that happens on the worker's next pass.
- A `waiting` export never fails because of chapters. If chapters of the range
  are in `error` or were dequeued (`not_requested`), it stays `waiting` and
  reports them in `blocked`; re-queuing them lets it continue.
- `DELETE` cancels a waiting export and removes its row; it does not dequeue
  the chapters it queued.
- `format` defaults to `"epub"`, the only format in v1. Formats live in a
  registry in `exporter` mapping `format → (mime, extension, accepted image
  types, builder)`; `Export.format` is derived from the stored `mime` through
  that registry, so adding PDF or FB2 later needs no schema change.
- Every state change of an export appends an `export` event.

```mermaid
stateDiagram-v2
    [*] --> waiting: POST /exports (queues missing chapters)
    waiting --> [*]: DELETE (cancel)
    waiting --> building: worker - whole range downloaded
    building --> ready: build stored (notify if it waited)
    building --> failed: build error
    building --> waiting: claim expired (recovery)
    ready --> [*]
    failed --> [*]: "Try again" creates a new export
    note right of waiting
        blocked = chapters in error or not queued;
        stays waiting until they are downloaded
    end note
```

### 5.6 Settings and subscriptions

`GET /api/v1/settings` returns the server settings the UI needs at startup:

```
notifications: {
  channels: {
    webpush: { enabled: bool, vapid_public_key: string | null }   URL-safe base64
    // later, e.g.: telegram: { enabled: bool, bot_username: string }
  }
}
static_base_url        string
refresh_min_interval   int              seconds
refresh_max_interval   int              seconds
```

The UI loads it once, hides the notifications bell when
`notifications.channels.webpush.enabled` is false, and uses `vapid_public_key`
for `PushManager.subscribe`. The key is read from the `settings` table.

**Subscription** (any channel):
`{id: "SB…", channel: string, label: string | null, created_at: string}`; the
channel-specific address (push endpoint and keys, chat ID…) is never returned.

| Route | Request | Response | Errors |
|---|---|---|---|
| `POST /webpush/subscriptions` | `{endpoint, keys: {p256dh, auth}}` | `201 Subscription`; an existing `endpoint` is replaced and keeps its `id` (`200`) | `422`, `503 channel_disabled` |
| `GET /subscriptions` | — | `Subscription[]` by `created_at` | — |
| `DELETE /subscriptions/{id}` | — | `204` | `404` |

- Storage is channel-neutral (`subscriptions`). Only **enrolment** is
  channel-specific, because each channel proves ownership differently: the
  browser creates a Web Push subscription itself, while a Telegram chat would be
  enrolled by its bot service writing through `novelhog.repo`. There is no
  generic "create subscription" route.
- A Web Push subscription is stored with `channel = 'webpush'`,
  `address = {endpoint, keys}`, `address_key = endpoint`, `label = User-Agent`.
- "Turn off on this device" deletes the device's own subscription through the generic
  route; the UI keeps its subscription ID in `localStorage` and falls back to looking
  it up by re-subscribing (same endpoint → same ID).

### 5.7 Events (SSE)

`GET /api/v1/events?novel_id=<id>` (filter optional).

| Event | Source (`entity_id` prefix) | Data |
|---|---|---|
| `novel` | `NV` | `Novel`, plus `released: int[]` on the event written by a sync |
| `chapter` | `CH` | `Chapter` plus `novel_id` |
| `export` | `EX` | `Export` |
| `resync` | not stored | `{}`: client must refetch over REST |

- The SSE event name is derived from the `entity_id` prefix; `events` stores no kind.
- New chapters found by a sync arrive as `chapter` events with `released_at`
  set; the UI updates "new" badges from them. There is no separate release event.
- `id:` of every SSE event is `events.seq`. The browser's `EventSource` keeps
  the last one and sends it as `Last-Event-ID` on reconnect, so the server keeps
  no per-client state and SSE has no row in `consumers`.
- On connect with `Last-Event-ID`, the API replays later events. Because pruning
  keeps the newest row per entity, older history has gaps: if the row with that
  seq no longer exists or is older than 24 h, the API sends one `resync` instead
  of replaying.
- The API polls `events` about once per second and pushes new rows.
- A comment line is sent every 15 s as keepalive.

### 5.8 Status codes

`404` unknown or malformed ID; `409 novel_exists`; `422` validation (including
FastAPI's own errors, reshaped into the error body, and `unsupported_host`);
`503 channel_disabled` (enrolment for a channel that is not configured); `500`
unexpected, generic message, traceback only in logs. The API never calls
external services, so it has no `502`.

## 6. Worker

### 6.1 Loop

```
on start: publish_hosts(); recover()                 # hosts table from the decode guide
loop:
    if shutting_down: exit
    recover()                                        # expired claims only
    dispatch_notifications()                         # event log → channels (§6.6)
    if novel = claim_due_refresh():                  # next_refresh_at <= now, available,
        refresh(novel); release(novel)               # oldest next_refresh_at first
        continue
    if export = claim_ready_export():                # waiting, range fully downloaded,
        build(export)                                # oldest created_at first (§6.5)
        continue
    if slot = claim_host_slot():                     # host of the oldest queued chapter
        batch = claim_batch(slot.host, BATCH_SIZE)   # that host's pending chapters → downloading
        for chapter in batch:                        # (no novel claim involved)
            if shutting_down: requeue(rest); break
            if state(chapter) != downloading: continue   # dequeued meanwhile
            try: fetch(host, url) → convert → store → downloaded (if url unchanged)
            except: state = error; event payload carries the message
            renew(slot and the rest of the batch); sleep(REQUEST_DELAY)
        release(slot)
        continue
    prune()                                          # event log, at most hourly
    sleep(IDLE_SLEEP)
```

- Each claim step is one compare-and-set `UPDATE … RETURNING` through `repo/claims.py` (§4), in its own transaction.
- `publish_hosts()` replaces the `hosts` table with the hosts in the installed
  scraper's decode guide (one transaction). Novels whose host is no longer
  listed keep working until a sync fails.
- `recover()` applies the uniform rule from §4 to every claimable table:
  rows whose `claim_expires_at` has passed are freed; expired `downloading`
  chapters go back to `pending` and expired `building` exports back to
  `waiting`.
- SIGTERM/SIGINT: finish the current chapter, requeue the rest of the batch,
  release the novel claim, exit 0.
- Errors in the loop itself (e.g. database locked) are logged, then back off 5 s,
  10 s, 20 s, … capped at `IDLE_SLEEP`.
- Priority: refreshes, then export builds, then downloads. Refreshes and builds
  are short; downloads are batched.

### 6.2 Refresh (TOC sync)

```
refresh(novel):
    initial = novel.last_refreshed_at is null
    try:
        toc = scraper_adapter.sync_toc(novel.host, novel.toc_url)  # {title, cover_url, chapters: [(number, url)]}
    except:
        log
        if initial: import_error = '<ExceptionType>: <summary>'
        interval = min(interval * 2, MAX); schedule; append novel event; return
    import_error = null
    if novel.title is null: title = toc.title          # never overwrite a user-set title
    new = upsert chapters by number (existing rows keep state; changed URL updated)
    for each new chapter:
        released_at = null if initial else now
        state = pending (queued_at = now) if novel.followed and not initial else not_requested
    if cover_key is null: try fetch cover (failure is logged, never fatal)
    last_refreshed_at = now
    interval = MIN if new and not initial else min(interval * 2, MAX)
    next_refresh_at = now + interval ± 10% jitter
    append events: one per new or changed chapter, one for the novel
    the novel event's payload also carries released = [numbers of new chapters]
        (empty on the initial import); the notifier reads it (§6.6)
```

- The initial import is the first sync after `POST /novels`: it fills in title,
  cover and chapters. If it fails, `import_error` is set and shown on the card;
  the novel keeps its backoff schedule and a manual sync retries.
- Initial import never sets `released_at`, never auto-queues, never notifies.
- With `MIN = 6 h`, `MAX = 30 d` the interval walks 6 h → 12 h → 1 d → 2 d → 4 d
  → 8 d → 16 d → 30 d while nothing is found, and resets to 6 h on any release.
- A failed sync backs off the same way, so a dead site settles at monthly; a
  manual sync forces an immediate retry.

### 6.3 Novel claims and host slots

- **Novel claim: syncs only.** A worker claims the novel (`claimed_by`,
  `claim_expires_at = now + CLAIM_TTL`) for a TOC sync and releases it
  afterwards, so two workers never sync the same novel at once (which would
  insert the same new chapters twice and notify twice). Chapter downloads never
  claim the novel; a sync and downloads of the same novel can run at the same
  time.
- **Host slots: download politeness.** `publish_hosts()` keeps exactly
  `HOST_CONCURRENCY` rows per host in `host_slots` (adding missing slots,
  deleting free surplus ones). To download, a worker claims a free slot of the
  host whose pending chapter has the oldest `queued_at` (compare-and-set like
  every other claim; a lost race moves on to the next host), then claims a batch
  of that host's pending chapters, oldest `queued_at` first, then by number,
  across all novels of the host. At most `HOST_CONCURRENCY` workers download
  from one site at any time; different sites download in parallel.
- The slot and the batch's chapter claims share one expiry and are renewed
  together after each chapter; the slot is released after the batch. Expired
  slots are freed and their chapters return to `pending` (§4 recovery).
- The API never uses the scraper and never claims anything.

### 6.4 Per-chapter pipeline

1. `scraper_adapter.fetch_chapter(host, url)` → `{title, body_html, http_session}`.
   Stateless: it requests the URL through FlareSolverr with the scraper's request
   helper and selects the body with the scraper decoder's content selector for
   the host. It does not create a scraper `Novel` object, does not use the
   scraper's cleaner and writes no files.
2. `html_to_markdown.convert(body_html, fetch_image)` → `{markdown, has_images, warnings}`.
3. `chapter_store.put(chapter_id, markdown)`; the chapter row gets `title`,
   `has_images`, `state = downloaded`. If a sync changed the chapter's URL
   while it was downloading, the result is discarded and the chapter goes back
   to `pending`.

### 6.5 Export builds

```
claim_ready_export():
    one transaction: pick the oldest export in 'waiting' whose chapters
    chapter_from..chapter_to are all 'downloaded'; set state = 'building',
    claimed_by = me, claim_expires_at = now + CLAIM_TTL; append an export event

build(export):
    try:
        data = exporter.build(export.format, novel, chapters of the range, cover)
        storage.put(key, data, mime, download_name = filename)
        state = 'ready', storage_key, size_bytes, ready_at = now
    except:
        state = 'failed', error = '<ExceptionType>: <summary>' (traceback logged)
    clear claimed_by / claim_expires_at; append an export event
```

- Builds read only the database, the cover and static storage; they take no
  novel claim and can run while chapters of the same novel download.
- A `failed` export is final; the UI offers "Try again", which creates a new
  export for the same range.
- Exports whose range was already complete at request time (`waited = 0`) do
  not notify; the user is still on the page.

### 6.6 Notification delivery

Notifications are derived from the event log; nothing extra is written when the
event happens. The worker's `notifier` consumer reads `events` after its cursor,
turns the relevant ones into messages and delivers them through channels. A
crash before the cursor moves causes at most one batch to be delivered again
(at-least-once); a channel outage never rolls back novel work.

**Rules** (`notifications.rules`, pure functions `event → NotificationMessage | None`):

| Rule | Matches | Message |
|---|---|---|
| `release` | `NV` event whose payload has a non-empty `released` and `followed = true` | "<N> new chapters (<first>–<last>)" |
| `export_ready` | `EX` event with `state = 'ready'` and `waited = true` | "<filename> is ready" |

`followed` is taken from the event payload, i.e. as it was when the sync ran.

```mermaid
sequenceDiagram
    participant W as Worker (sync / export build)
    participant DB as Database (novelhog.repo)
    participant N as Notifier consumer (worker)
    participant C as Channel (WebPush, later Telegram)
    participant S as Subscriber (browser, chat)

    W->>DB: one transaction: state change + events
    loop every worker pass
        N->>DB: claim consumer 'notifier', read events after last_seq
        loop each event
            N->>N: rules → message or nothing
            loop each subscription
                N->>C: send(subscription, message)
                C->>S: deliver
                alt endpoint gone (404/410)
                    C-->>N: Gone
                    N->>DB: delete subscription
                else other failure
                    C-->>N: Failed (logged)
                end
            end
        end
        N->>DB: last_seq = last event read, release claim
    end
```

```
dispatch_notifications():
    claim consumers['notifier'] if available
      (claimed_by = me, claim_expires_at = now + CLAIM_TTL); otherwise return
    events = events with seq > last_seq, ordered, at most 100
    for each event:
        message = rules.match(event)                     # None for most events
        if message:
            for each subscription in subscriptions:
                channel = channels[subscription.channel] # unknown channel: skip, log
                result = channel.send(subscription, message)
                Gone   → delete the subscription
                Failed → log (no retry)
    last_seq = seq of the last event read; release the claim   (one transaction)
```

- Only one process advances a consumer at a time; other workers skip the step.
- Every subscription receives every notification in v1 (one person, no per-subscription
  filters).
- If push is not configured (no `NOVELHOG_PUBLIC_URL` / `VAPID_SUBJECT`), the
  `webpush` channel is not registered; the cursor still advances.
- Future services (e.g. a Telegram bot) add their own row to `consumers` and
  read the same log at their own pace.

## 7. Modules

Package `novelhog`, under `src/novelhog/`.

| Module | Responsibility |
|---|---|
| `ids` | `new_id(prefix) -> str`, `is_valid(id, prefix) -> bool` |
| `config` | Reads `NOVELHOG_*` env vars into a typed settings object |
| `domain` | Chapter state rules, selector validation, refresh interval rule, domain exceptions (`NotFound`, `Conflict`, `ValidationFailed`) |
| `repo` | The database interface on SQLAlchemy Core: `schema.py` (tables), `claims.py` (compare-and-set claim, renew, release, recover), Alembic migrations, work claiming, event log, consumers, subscriptions, settings. The only package that talks to the database. Importable on its own (depends only on the standard library, `sqlalchemy`, `alembic`, `ids` and `domain`), so the API, the worker and future services (e.g. a Telegram bot) share it |
| `scraper_adapter` | The only importer of `web_novel_scraper` (pinned `==2.13.1`). `supported_hosts() -> [Host]`, `sync_toc(host, toc_url) -> {title, cover_url, chapters: [(number, url)]}` and `fetch_chapter(host, url)`, both stateless: pages are requested through FlareSolverr with the scraper's request helper and decoded with the host's decoder rules (title, index, next page, content) plus `overrides`; no scraper `Novel` object, no files written. `find_cover_url(toc_html, toc_url)`. Builds scraper request/decoder config from `NOVELHOG_*` (FlareSolverr URL, force FlareSolverr). Used only by the worker |
| `scraper_adapter.overrides` | One module per host with fixes for scraper defects, registered when the adapter is imported. v1: `fanmtl_com` (next-page link written as `&gt;`) |
| `html_to_markdown` | Chapter body → Markdown (§8.1) |
| `image_fetcher` | Plain HTTP image download reusing FlareSolverr cookies and user agent; validation (§8.2) |
| `chapter_store` | `put(chapter_id, markdown)`, `get(chapter_id)` over `chapter_contents` |
| `storage` | `StaticStorage`: `put(key, data, mime, download_name=None)`, `delete(key)`, `url(key) -> str`. v1: `LocalStaticStorage` |
| `exporter` | Format registry (`format → mime, extension, accepted image types, builder`). EPUB: Markdown → XHTML (`markdown-it-py`, raw HTML disabled) → `ebooklib`; cover; inline images extracted to EPUB items and converted per §8.4 |
| `images` | Pillow helpers: `downscale(data, mime, max_dimension)`, `convert_for(data, mime, accepted) -> (data, mime)` |
| `notifications.rules` | `match(event) -> NotificationMessage(title, body, url, icon) \| None`: which events notify and their channel-neutral wording (§6.6, §8.6) |
| `notifications.channels` | `NotificationChannel` protocol: `name: str`, `send(subscription, message) -> Delivered \| Gone \| Failed(reason)`. Registry `name → channel`. v1: `WebPushChannel` (`pywebpush`, VAPID keys from `settings`; `404`/`410` → `Gone`). Later: `TelegramChannel` |
| `notifications.dispatcher` | `dispatch()`: the `notifier` consumer; claims its cursor, reads new events, applies the rules, delivers through the registry, advances the cursor (§6.6) |
| `vapid` | `ensure_keys(repo)`: creates the VAPID key pair in `settings` if missing (API startup) |
| `api` | FastAPI app under `/api/v1`: routers, SSE, error handlers. Serves no files. Uses only `repo`, `domain`, `ids`, `vapid`, `storage.url` |
| `worker` | Loop, host publishing, refresh, downloads, export builds, notification dispatch, pruning, signal handling, recovery |
| `cli` | `novelhog web` (API + Caddy), `novelhog api` (API only, for development), `novelhog worker` |

### Site support

- Supported hosts are exactly those in the installed scraper's decode guide (21 at
  2.13.1). novelhog keeps no host list of its own.
- No module outside `scraper_adapter.overrides` names a specific site.
- `find_cover_url` tries, in order: `og:image` / `twitter:image` meta; schema.org
  `image`; common selectors (`.cover img`, `.book img`, `.novel-cover img`,
  `figure img`); the first `<img>` whose `alt` equals the novel title. An override
  may register a host-specific rule ahead of these. Relative URLs are resolved
  against the TOC URL.

## 8. Content rules

### 8.1 HTML → Markdown

- Allowed tags: `p`, `br`, `em`/`i`, `strong`/`b`, `img`, and the list tags
  `ul`, `ol`, `li`, `dl`, `dt`, `dd`. Other tags are dropped, keeping their text.
- Lists:
  - `ul` → `- ` items; `ol` → `1. ` items, keeping a `start` attribute as the
    first number.
  - Nested lists are indented by 4 spaces per level (CommonMark nesting).
  - A `li` containing several paragraphs keeps them as indented continuation
    paragraphs of that item.
  - Definition lists have no CommonMark form: each `dt` becomes a bold paragraph
    and each `dd` a paragraph after it.
  - Empty `li`/`dd` are dropped; a list left with no items is dropped.
  - Text inside items is escaped like any other text, so a literal leading `-`
    or `1.` inside an item never starts a new list.
- `script`, `style`, `noscript`, `iframe` and their contents are removed entirely.
- All text is escaped so Markdown metacharacters stay literal: backslash,
  asterisk, underscore, hash, greater-than, square brackets, backtick, pipe,
  tilde, and a leading `-`, `+` or `<digits>.` at the start of a line.
- Paragraphs are separated by one blank line; empty paragraphs are dropped.
- In-text headings such as "Chapter 29 …" remain plain paragraphs.
- `img` → `![alt](data:<mime>;base64,…)` via `image_fetcher`; failure → the
  literal text `[image unavailable]` plus a warning; the chapter still succeeds.
  `has_images` is true if at least one image was embedded.

### 8.2 Images (chapter images and covers)

- Fetched over plain HTTP with the cookies and user agent from the FlareSolverr
  session (FlareSolverr itself returns HTML only).
- Type detected from magic bytes; allowed: PNG, JPEG, GIF, WebP. SVG and anything
  else are rejected.
- Size cap `NOVELHOG_MAX_IMAGE_BYTES` (default 2 MiB), checked on the downloaded
  bytes before any processing.
- **Originals are kept**: images are stored in the format they were served in; no
  format conversion at ingest.
- **Oversized images are downscaled** at ingest, in their original format, when
  either side exceeds `NOVELHOG_IMAGE_MAX_DIMENSION` (default 1600 px; `0`
  disables). Aspect ratio is preserved; animated GIF/WebP are left untouched;
  JPEG is re-encoded at quality 90; images within the limit are stored byte for
  byte as downloaded.
- Chapter images are inlined in Markdown; covers go to static storage. The UI
  shows covers as stored (all four types render in current browsers).
- Image processing uses Pillow.

### 8.3 Static storage keys

- Covers: `covers/<novel_id>/<sha256[:16]>.<ext>` (content-addressed, cached as
  immutable).
- Exports: `exports/<novel_id>/<export_id>/<Title> Chapters <from>-<to>.<ext>`;
  the last path segment is the download filename (URL-encoded in URLs).
- `LocalStaticStorage` writes under `/data/static` (worker only). Caddy in
  `novelhog-web` serves it read-only at `/static`:
  - `/static/covers/*`: `Cache-Control: public, max-age=31536000, immutable`.
  - `/static/exports/*`: `Content-Disposition: attachment`, and an explicit
    `Content-Type` per extension (`.epub` → `application/epub+zip`), since
    Caddy's default type table may not know it.
  - `/sw.js`: `Cache-Control: no-cache`; other SPA assets are hashed by Vite and
    cached as immutable; `index.html` is `no-cache`.

### 8.4 Export image compatibility

Every export format declares the image types it accepts. The exporter converts
any other image to an accepted type at export time; stored originals are never
modified, so each export starts from the best available source.

| Format | Accepted | Conversion of anything else |
|---|---|---|
| EPUB | JPEG, PNG, GIF | WebP with alpha → PNG; other WebP → JPEG (quality 90); animated WebP → first frame |
| FB2 (future) | JPEG, PNG | as EPUB; GIF → PNG (first frame) |

The cover goes through the same conversion.

### 8.5 EPUB

- One XHTML document per chapter in `number` order, headed by the stored title.
- The cover, when present, is the book cover.
- Inline `data:` images become EPUB image items, converted per §8.4.
- Metadata: title; identifier = export ID.

### 8.6 Notification messages

`rules.match` turns a matching event into a channel-neutral
`NotificationMessage`; each channel decides how to present it.

| Kind | title | body | url |
|---|---|---|---|
| `release` | novel title | "<N> new chapters (<first>–<last>)" | `<PUBLIC_URL>/novels/<id>` |
| `export_ready` | novel title | "<filename> is ready" | `<PUBLIC_URL>/novels/<id>#exports` |

`icon` is the novel's cover URL, or the app icon. Web Push sends the message as
the JSON payload; the service worker shows it and opens/focuses `url` on click.

## 9. Configuration

| Variable | Default | Meaning |
|---|---|---|
| `NOVELHOG_DATA_DIR` | `/data` | Holds `static/` (and `app.db` with the default database URL) |
| `NOVELHOG_DATABASE_URL` | `sqlite:////data/app.db` | SQLAlchemy URL; `postgresql+psycopg://…` for Postgres |
| `NOVELHOG_PUBLIC_URL` | — (required for push) | External HTTPS URL, used in push links and the VAPID claim |
| `NOVELHOG_VAPID_SUBJECT` | — (required for push) | `mailto:` contact for the VAPID claim |
| `NOVELHOG_FLARESOLVERR_URL` | `http://flaresolverr:8191` | |
| `NOVELHOG_STATIC_BACKEND` | `local` | Only `local` in v1 |
| `NOVELHOG_STATIC_BASE_URL` | `/static` | May be an absolute URL (separate domain/CDN) |
| `NOVELHOG_REFRESH_MIN_INTERVAL` | `21600` (6 h) | |
| `NOVELHOG_REFRESH_MAX_INTERVAL` | `2592000` (30 d) | |
| `NOVELHOG_BATCH_SIZE` | `10` | Chapters claimed per batch |
| `NOVELHOG_HOST_CONCURRENCY` | `1` | Workers allowed to download from one site at the same time (`host_slots` per host) |
| `NOVELHOG_CLAIM_TTL` | `300` s | Lifetime of every claim (novels, chapters, exports, consumers) before renewal |
| `NOVELHOG_IDLE_SLEEP` | `30` s | Worker sleep when no work |
| `NOVELHOG_REQUEST_DELAY` | `1` s | Pause between chapter fetches |
| `NOVELHOG_MAX_IMAGE_BYTES` | `2097152` | Download size cap per image |
| `NOVELHOG_IMAGE_MAX_DIMENSION` | `1600` | Longest side in px before downscaling; `0` disables |
| `NOVELHOG_WORKER_ID` | `<hostname>:<pid>` | |
| `NOVELHOG_HTTP_PORT` | `8080` | API listen port |

If push variables are missing, `GET /settings` reports
`notifications.channels.webpush = {enabled: false, vapid_public_key: null}`,
`POST /webpush/subscriptions` returns `503 channel_disabled`, and the UI hides the
notifications bell; everything else works.

## 10. UI

React 19 + TypeScript + Vite; TanStack Query; React Router; Tailwind CSS with a
few local components; API types generated with `openapi-typescript`. One app-wide
`EventSource` patches the query cache from SSE events. Service worker (`sw.js`)
handles `push` and `notificationclick` only (no offline caching in v1). A web app
manifest makes the app installable.

### Screens

1. **Library (`/`)**: cards with cover (placeholder if none), title, host, a
   stacked bar of downloaded / pending / error over total, a "N new" badge, the
   follow toggle, and a syncing/downloading indicator. "Add novel" button. Empty
   state with explanation.
2. **Add novel (dialog)**: one step: table-of-contents URL, follow toggle (on by
   default), **Add**. The host check is a database lookup, so `422
   unsupported_host` (with the supported-host list) and `409 novel_exists` (with
   a link to the existing novel) appear immediately. On success the dialog
   closes and the new card shows the URL's host and "Importing…" until the first
   `novel` event fills in title and cover. If the import fails, the card shows
   "Import failed: <message>" with **Retry** (`POST /sync`). The title can be
   edited later on the novel page (`PATCH {title}`).
3. **Novel (`/novels/:id`)**: sends `PATCH {seen: true}` on open.
   - Header: cover, title, host, TOC link, follow toggle, Sync button,
     "checked 2 h ago · next in 4 h", counters.
   - **Overview bar** (both layouts): a full-width strip of the whole novel, one
     segment per run of consecutive chapters in the same state, widths
     proportional to run length (minimum 2 px). Colours: downloaded solid blue,
     pending light blue, downloading amber, error red, not requested grey. Tick
     labels at the start, end and two evenly spaced points. A dark frame marks
     the chapters currently shown below. Tapping or clicking the bar jumps to
     the range containing that chapter (the frame snaps to ranges; no
     fine-grained drag). It updates live from SSE events. While a search or
     filter is active, non-matching segments are dimmed. It has a text
     alternative summarising the runs.
   - **Search and filters** (both layouts): a search field matching chapter
     number or title, and filter chips per state (each with its status icon and
     count) plus "New". While search or a filter is active, results from all
     ranges are shown, grouped under range headings.
   - **Range chips** (both layouts): ranges of 50 chapters ("1–50" … "601–630"),
     each with a mini bar of downloaded / pending / error. Default: the range
     holding the newest chapter. Hidden while search or a filter is active. On
     desktop they wrap onto several rows; on phones (< 720 px) they scroll
     sideways.
   - **Chapter table** of the chosen range (both layouts). Error messages are
     never shown in the table; an error is visible only as the red status tab.
     - Desktop (≥ 720 px) columns: `#` · title (with "new" dot) · **Added** ·
       **Updated** · status tab. Added shows the date only, from
       `released_at`; chapters from the initial import show "—" with the
       tooltip "Part of the initial import". Updated is relative ("12 min ago")
       from `updated_at`.
     - Phone (< 720 px) rows: number · title (with "new" dot) · status tab. No
       Added/Updated columns.
     - **Status tab**: a full-row-height block at the end of each row, about
       44–52 px wide, holding the status icon (display only; text label for
       screen readers). Calm states use soft tints, attention states full
       colour; icon colours keep at least 3:1 contrast in both themes:

       | State | Tab | Icon |
       |---|---|---|
       | not requested | pale grey | grey empty circle |
       | pending | pale blue | blue clock |
       | downloading | amber | dark down arrow |
       | downloaded | pale blue | blue check in a circle |
       | error | red | white warning triangle |

       In normal mode the tab's leading edge is a chevron point facing into the
       row (mirrored for right-to-left); in select mode it is a plain
       rectangle.
   - **Normal mode** (default):
     - Clicking or tapping a row opens chapter details. Rows are links named
       "<title>, <status>, open details".
     - The toolbar has a **Select** button. Long-press (phone) or
       Shift-/Ctrl-/Cmd-click (desktop) on a row enters select mode with that
       row (or range) selected.
     - No action bar.
   - **Select mode**:
     - A drawn rounded-square checkbox slides in at the start of every row
       (visual only, hidden from assistive tech; reduced motion: no slide).
       Selected rows show a filled checkbox with a check mark and a tinted
       background distinct from the downloaded tint.
     - Clicking or tapping anywhere on a row toggles it; long-press (phone) or
       Shift-click (desktop) selects from the last selected row to that one;
       Ctrl/Cmd-click toggles one.
     - Rows form a multi-select list (`aria-multiselectable`, `aria-selected`
       per row); entering the mode is announced ("Select mode, N selected").
     - Header shows "N selected", **Select all** (all visible rows, including
       search/filter results) and **Done**. Desktop also has a header checkbox
       (a real button with none / some / all states).
     - Action bar: "N selected · from–to · summary" with Download, Cancel
       queued, Retry errors and Export EPUB… (phone: Download, Retry, More…).
     - Leaving: Done, Escape, the back gesture, or completing any bulk action
       (toast "Queued N · skipped M", then back to normal mode, selection
       cleared).
     - Selection survives live SSE updates of the selected chapters.
   - **Chapter details**: opened from a row in normal mode; a modal dialog on
     desktop and a bottom sheet on phones, with the same content:
     - Heading: "Chapter N of M" and the chapter title; previous/next buttons to
       step through chapters without closing; close (also Escape / swipe down).
     - Error box (only in `error` state): when it failed (relative, from the
       newest event) and the full error message in monospace with a copy
       button.
     - Fields: status (icon + name), source URL (link), added (date only, from
       `released_at`, or "—"), queued (`queued_at`, when pending), last change
       (`updated_at`), images (yes / no / not downloaded yet).
     - Actions: "Open on site" plus one state-dependent primary action:
       Download (`not_requested`), Cancel (`pending`), Retry download
       (`error`), none for `downloading`/`downloaded`. Actions use the same
       `PATCH /chapters` call with a one-item list.
   - **Export dialog** (Export EPUB… in the action bar; dialog on desktop, bottom
     sheet on phones): one export, one file.
     - From/To inputs prefilled from the selection's first and last chapter,
       with a small bar showing the range's chapter states.
     - Format (EPUB only in v1) and the resulting file name.
     - If every chapter is downloaded: "110 chapters, all downloaded" and the
       button **Export**.
     - Otherwise a calm note "30 chapters will be downloaded first (601–630)"
       and the button **Download & export**; chapters in `error` are counted
       there too and re-queued.
     - Closing after submit shows a toast ("Export queued" or "Exporting…");
       progress continues in the exports panel.
   - **Exports panel**: one row per export, newest first: filename, range,
     state and actions.

     | State | Row shows | Actions |
     |---|---|---|
     | `waiting` | progress bar "84 / 110 downloaded" | Cancel |
     | `waiting`, blocked | "Blocked by 2 errors" / "3 chapters not queued" | Retry / Queue them, Cancel |
     | `building` | spinner "Building…" | — |
     | `ready` | size and date | Download |
     | `failed` | error message | Try again (new export, same range) |

   - Dequeuing chapters (select mode → Cancel queued) that a waiting export
     needs shows a confirmation naming the export.
- **Follow toggle**: a pill button with a star icon (`aria-pressed`). Following:
  solid gold star, label "Following". Not following: grey outline star, label
  "Follow". Used on library cards, the novel header and the add dialog.
- **Device notifications** (only when `/settings` reports
  `notifications.channels.webpush.enabled`):
  - Header: a small icon-only bell button (`aria-label` "Notifications on this
    device: on/off"); outline when this device is not subscribed, filled when it
    is. Clicking it subscribes or unsubscribes this device.
  - Banner below the header, shown only while this device is not subscribed and
    the banner has not been dismissed: "Get a notification on this device when a
    followed novel has new chapters." with **Turn on** and a dismiss (×) button.
    Dismissal is stored in `localStorage` (`novelhog.pushBannerDismissed`); if
    storage is unavailable the banner simply shows again next load. Subscribing
    hides it.
  - **Blocked variant**: when `Notification.permission` is `denied`, the banner
    turns amber with a struck-through bell and reads "Notifications are blocked
    for this site. Allow them in your browser's site settings, then reload the
    page." It has no Turn on button (the browser will not prompt again), only
    dismiss, stored separately as `novelhog.pushBlockedBannerDismissed` so a
    later block shows it again. The header bell is shown struck through and
    disabled with the tooltip "Blocked in browser settings".
  - Subscribing requests permission, calls `PushManager.subscribe` with
    `vapid_public_key`, then `POST /webpush/subscriptions` and keeps the returned
    subscription ID; unsubscribing calls `unsubscribe()` and
    `DELETE /subscriptions/{id}`. Over plain HTTP the bell is disabled with a
    tooltip that HTTPS is required and the banner is not shown; on iOS outside an
    installed PWA the banner explains "Add to Home Screen" instead of Turn on.
- Works at phone width; on narrow screens the action bar becomes a bottom sheet.
- **Theme switcher**: an icon-only button in the header, immediately left of the
  notifications bell (`aria-haspopup="menu"`, `aria-label` "Theme: <choice>").
  Its icon shows the current choice: monitor (System), sun (Light), moon (Dark).
  It opens a three-item menu (`menuitemradio`, check mark on the current one).
  - System follows `prefers-color-scheme` and reacts to OS changes live; Light
    and Dark set `data-theme="light"|"dark"` on `<html>`.
  - The choice is stored per device in `localStorage` (`novelhog.theme`, default
    `system`); if storage is unavailable the app uses System.
  - A small inline script in `index.html` applies the stored theme before first
    paint, so a reload never flashes the wrong theme.
  - All colours are design tokens defined for both themes.
- Visual design is done during implementation with the `frontend-design` skill.

### Sketches

Static captures of the approved sketches, as a visual guide for implementers.
Novel titles, chapter states and numbers are made-up sample data. Where a
sketch and the text above differ, the text wins.

**Library**: cards, header theme switcher and bell, push banner (device not
subscribed).

![Library](sketches/library.png)

**Add novel dialog**: one step; the host check is a database lookup and the
details arrive with the first import.

![Add novel dialog](sketches/add-novel.png)

**Notification banner**: default, iOS not installed, permission blocked, phone
width, header bell states, theme menu.

![Notification banner states](sketches/notification-banner.png)

**Chapters, desktop, normal mode**: search, state filters, overview bar with
frame, range chips, table with chevron status tabs; clicking a row opens
details.

![Desktop chapters, normal mode](sketches/desktop-normal-mode.png)

**Chapters, desktop, select mode**: drawn checkboxes, header checkbox in the
"some" state, plain status tabs, action bar.

![Desktop chapters, select mode](sketches/desktop-select-mode.png)

**Chapter details, desktop dialog**

![Chapter details dialog](sketches/desktop-chapter-details.png)

**Chapters, phone**: normal mode and select mode.

![Phone chapters, normal mode](sketches/phone-normal-mode.png)
![Phone chapters, select mode](sketches/phone-select-mode.png)

**Chapter details, phone bottom sheet**

![Chapter details bottom sheet](sketches/phone-chapter-details.png)

**Export dialog, desktop**: all chapters downloaded; one file.

![Export dialog](sketches/export-dialog.png)

**Export sheet, phone, chapters missing**: the missing chapters are downloaded
first and the export waits for them.

![Export sheet with missing chapters](sketches/phone-export-missing.png)

**Dark mode**: generated from the light sketches with one dark palette (library,
desktop chapters, export dialog, phone normal and select mode).

![Dark library](sketches/dark-library.png)

![Dark desktop chapters](sketches/dark-desktop-normal-mode.png)

![Dark export dialog](sketches/dark-export-dialog.png)

![Dark phone chapters, normal mode](sketches/dark-phone-normal-mode.png)
![Dark phone chapters, select mode](sketches/dark-phone-select-mode.png)

## 11. Error handling

- `domain` and `repo` raise domain exceptions; only `api` maps them to HTTP.
- The worker never dies on a single chapter or refresh: the chapter goes to
  `error` with a short message (`<ExceptionType>: <summary>`); a failed refresh is
  logged and backed off; tracebacks go to logs.
- The scraper's own retry settings apply to upstream requests.
- Push failures are logged and not retried; `404`/`410` deletes the subscription.
- Logging: structured JSON to stdout (`structlog`) with `novel_id`, `chapter_id`,
  `worker_id`, `host` bound where relevant.

## 12. Future work (not part of v1)

- **Worker wake-up.** A `Waker` interface (`wait(timeout) -> bool` in the worker,
  `notify()` in the API). First implementation: UDP datagram to every address the
  worker service name resolves to, with the timed sleep kept as fallback; the
  worker always re-checks the database. With Postgres: `LISTEN/NOTIFY`.
- **Multi-host clustering** on Postgres: already supported by the code and tested in CI; needs only `NOVELHOG_DATABASE_URL`, shared static storage (S3) and workers on several hosts.
- **S3 static backend** (`S3StaticStorage`), setting `Content-Type` and
  `Content-Disposition` per object.
- **Image table**: move inline images of chapters with `has_images` into a
  content-addressed table.
- **Telegram notifications.** A `TelegramChannel` registered in the worker's
  channel registry (`address = {chat_id}`), plus a separate `novelhog-telegram`
  bot service that uses `novelhog.repo` to register subscriptions on `/start`, remove
  them on `/stop`, and answer simple commands (library, new chapters). It may
  also read the event log with its own `consumers` row. No change to the event
  log or the worker loop.
- **Delete novel** (`DELETE /novels/{id}`); the schema already cascades.
- In-browser reader, FTS5 search, manual cover upload, offline PWA caching,
  splitting pages into real chapters.

## 13. Testing

- **Unit:** `ids`; `html_to_markdown` (allowlist, escaping, empty paragraphs,
  `ul`/`ol` with `start`, nested lists, multi-paragraph items, `dl` → bold term +
  paragraph, empty items and lists dropped, round trip through `markdown-it-py`
  yields the same list structure,
  images, failure placeholder); `image_fetcher` (magic bytes, SVG rejection, size
  cap); `images` (downscale only above the limit, aspect ratio kept, animated
  untouched, `0` disables, small images byte-identical; WebP → PNG with alpha /
  JPEG without, animated → first frame); `find_cover_url` (each heuristic,
  relative URLs); `exporter` (order, cover, image extraction, no WebP in EPUB
  output, stored originals unchanged); chapter state rules; selector validation; refresh interval
  rule (reset, doubling, cap, jitter bounds).
- **Integration (real temporary database; SQLite by default, Postgres in CI, see below):** claims with two competing
  workers; recovery of expired claims on every table; SIGTERM requeue; dequeue during a claimed
  batch; refresh scheduling and priority over downloads; initial import vs later
  release (`released_at`, auto-queue for followed only); `new_pages` vs
  `seen_at`; SSE replay from `Last-Event-ID` and `resync`; pruning keeps the
  newest event per entity; `Chapter.error` comes from the newest event only
  while the state is `error` and survives pruning.
- **Notifications (fake channel):** a sync with new chapters of a followed novel
  writes exactly one novel event with non-empty `released`; the initial import
  writes `released = []`; `rules.match` returns a `release` message only for
  non-empty `released` with `followed = true`, an `export_ready` message only for
  a ready export with `waited = true`, and nothing for every other event; the
  dispatcher delivers each matching event to every subscription once, skips unknown
  channels, deletes subscriptions on `Gone`, logs `Failed`, and advances `last_seq`
  only after the batch; only one process holds the `notifier` claim at a time
  and a stale claim is taken over; a crash before advancing re-delivers at most
  one batch; `WebPushChannel` maps `404`/`410` to `Gone`.
- **Event log:** every state-changing write appends its events in the same
  transaction; pruning keeps the newest event per entity, never deletes events
  after the slowest consumer's `last_seq`, and moves a consumer more than 7 days
  behind forward with a warning.
- **Subscriptions API:** `POST /webpush/subscriptions` creates a `webpush` subscription and
  returns the same ID for the same endpoint; `GET /subscriptions` lists
  all channels without addresses; `DELETE /subscriptions/{id}` removes
  any channel's subscription.
- **VAPID:** `ensure_keys` creates the pair once, is a no-op when it exists, and
  two concurrent callers end with one pair; `/settings` exposes only the public
  key.
- **Hosts and novels:** `publish_hosts` replaces the table from the decode guide;
  `POST /novels` rejects unknown hosts and duplicate `toc_url`; the initial import
  sets the title only when none is set and records `import_error` on failure; a
  later successful sync clears it; `PATCH {title}` survives syncs.
- **Process boundaries:** the API package imports neither `scraper_adapter`,
  `exporter`, `images` nor `notifications.channels` (import-linter contract);
  `repo` imports nothing beyond the standard library, `ids` and `domain`.
- **Exports (real temporary database):** `POST` queues `not_requested` and `error`
  chapters of the range and creates a `waiting` export with the right `waited`;
  an identical waiting export is returned instead of a duplicate; the worker
  builds only when the whole range is `downloaded`; `waiting → building →
  ready` sets storage key, size and `ready_at`; a build exception gives `failed`
  with a message; `blocked` lists errors and dequeued chapters and the export
  continues after they are re-queued and downloaded; `DELETE` removes a waiting
  export, leaves its chapters queued and answers `409` once building; expired
  `building` claims return to `waiting`; every state change emits an `export`
  event.
- **Database portability:** every repository and integration test takes its
  database from `NOVELHOG_TEST_DATABASE_URL` (default: a temporary SQLite file).
  CI runs the suite twice, on SQLite and on PostgreSQL 16 (service container);
  `scripts/test_pg.sh` does the same locally with Docker. Claim tests check
  on both databases that concurrent workers never claim the same row, that a
  worker losing a race retries and gets the next candidate, and that an expired
  claim is taken over.
- **Migrations:** `alembic upgrade head` on an empty database produces the same
  schema as `schema.py` (Alembic autogenerate reports no differences) on both
  databases.
- **API:** `httpx` against the app with a real temporary database and no
  scraper, network or worker; every route,
  success and each error code, including `503 channel_disabled`.
- **Regression (offline HTML fixtures in the repo):** fanmtl `&gt;` pagination;
  `fetch_chapter` writes no scraper files and needs no scraper `Novel`; no
  scripts and no empty paragraphs in output.
- **Host slots (real database, both engines):** `publish_hosts` keeps exactly
  `HOST_CONCURRENCY` slots per host without deleting claimed ones; with
  `HOST_CONCURRENCY = 1` and three workers, never more than one downloads from a
  host at a time while other hosts proceed; with `2`, at most two; a lost slot
  race moves to the next host; expired slots are freed and their chapters
  requeued; a sync and a download batch of the same novel run concurrently; a
  chapter whose URL changed during download returns to `pending`.
- **Frontend:** Vitest + Testing Library for normal mode (row click/tap opens
  details; Select, long-press and Shift/Ctrl-click enter select mode with the
  right selection), select mode (tap toggles, long-press and Shift-click select
  a range, Select all covers search/filter results, header checkbox none/some/
  all, Done/Escape/back and finishing a bulk action leave the mode and clear the
  selection, selection survives SSE updates), table rendering (no error text in
  rows; status tab colour and icon per state; chevron only in normal mode;
  Added shows date only from `released_at`, "—" for initial import), chapter
  details (dialog on desktop, sheet on phone, error box only in `error` state,
  copy button, previous/next, state-dependent primary action sends the right
  PATCH), overview bar (runs and proportions from chapter states, frame position,
  tap jumps to the containing range, dimming under search/filter, live update
  from a `chapter` event), range chips (default to newest, hidden while
  searching), search by number and title, state filters across all ranges, SSE
  cache updates,
  the notifications toggle states, and the theme switcher (stored choice applied,
  System follows `prefers-color-scheme` changes, storage unavailable falls back
  to System); one Playwright smoke test of the main flow against a fake backend.
- **Container smoke test** (`docker compose up` in CI, no worker network access):
  Caddy serves the SPA with fallback to `index.html`, `/sw.js` with `no-cache`,
  a file under `/static/exports/` with `Content-Disposition: attachment` and
  `application/epub+zip`, proxies `/api/v1/settings`, and streams
  `/api/v1/events` without buffering; the container exits when either process
  dies.
- No live-site tests in CI. `scripts/smoke_live.sh <url>` exists for manual
  checks.

## 14. Styleguide (to be written to `STYLEGUIDE.md` as the plan's first step)

- **Python:** 3.14, `uv`; `ruff format` + `ruff check`; `mypy --strict`;
  `pytest`. `snake_case`; tests named `test_<unit>__<behaviour>`, e.g.
  `test_claim_batch__skips_claimed_novel`. No bare `except:`.
- **TypeScript:** strict mode; `eslint` + `prettier`; components
  `PascalCase.tsx`, hooks `useX.ts`, tests colocated as `*.test.tsx`.
- **Layout:**

  ```
  src/novelhog/{api,worker,domain,repo,scraper_adapter,storage,exporter,notifications,
                html_to_markdown,image_fetcher,images,chapter_store,vapid.py,ids.py,
                config.py,cli.py}
  migrations/        (Alembic: env.py, versions/)
  web/src/{routes,components,api,hooks}   web/public/{sw.js,manifest.webmanifest}
  tests/{unit,integration,api,fixtures}
  docker/{Dockerfile,Caddyfile}   compose.yaml   STYLEGUIDE.md   docs/superpowers/specs/
  ```
