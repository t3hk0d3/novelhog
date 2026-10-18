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

- Library, add novel (preview → create), automatic and manual TOC sync with
  adaptive interval.
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

```
 Browser (React PWA + service worker)
      │  REST /api/v1  +  SSE /api/v1/events        static: STATIC_BASE_URL (/static)
      ▼
 HTTPS reverse proxy (user's)
      ▼
 api (FastAPI) ─────── reads/writes ───────►  SQLite  /data/app.db  (WAL)
      │  serves SPA at /, /static, /sw.js              ▲
      │  preview: read-only site fetch                 │ lease novel → sync TOC / claim batch,
      │                                                │ write chapters, content, changes
 /data/static ◄── covers ── worker ────────────────────┘
 (exports written by api)     │ web_novel_scraper (library)         Web Push
                              ├──────────► FlareSolverr ──► sites   ─────────► browser push service
```

| Component | Responsibility |
|---|---|
| `api` | FastAPI app. REST under `/api/v1`, SSE stream, serves the built SPA at `/`, `sw.js` and `manifest.webmanifest` at the root, and local static files at `/static`. Builds EPUBs synchronously from the database. The only site access is `preview` (read-only, no scraper files written). |
| `worker` | Separate process/container from the same image. The only writer of scraper working files. Runs due TOC syncs (with adaptive scheduling and push notifications) and downloads `pending` chapters in batches. |
| `web` | React SPA + service worker, built to static files and served by `api` (single origin, no CORS). |
| SQLite | Single source of truth: novels, chapters, chapter content (Markdown), exports, push subscriptions, change log. Accessed only through the repository layer. |
| Static storage | Covers and EPUB files behind `StaticStorage`; v1 backend is local disk under `/data/static`. |

### Deployment

- `compose.yaml` with services `novelhog-api` and `novelhog-worker`, one image,
  different commands (`novelhog api`, `novelhog worker`).
- Both mount the `/data` volume and join FlareSolverr's Docker network.
- `novelhog-api` publishes one port (default `8080`) for the reverse proxy.
- The reverse proxy must terminate HTTPS (required for service workers and push),
  disable response buffering for `/api/v1/events`, and not cache `/sw.js`. The
  README includes Caddy and nginx snippets.
- Scaling workers (`--scale novelhog-worker=N`) is safe on one host; multi-host
  clustering requires a networked database (§12).

## 3. Identifiers

- Format: two-letter uppercase type prefix + 12 random characters from
  `[a-z0-9]`, generated with `secrets`.
  - `NV` novel, `CH` chapter, `EX` export, `PS` push subscription.
    Example: `CHk3x9q2m7b4wz`.
- 36¹² ≈ 4.7·10¹⁸ (~62 bits). Primary keys are unique; an insert that collides is
  retried with a fresh ID.
- IDs carry no ordering. Every listing orders by an explicit column
  (`created_at`, `number`).
- An ID with the wrong prefix or format in a path or body is treated as unknown →
  `404`.

## 4. Database schema (SQLite)

SQLite ≥ 3.45, WAL mode, `foreign_keys = ON`. Timestamps are UTC ISO-8601 TEXT
(`2026-10-19T08:15:00Z`). Schema version is tracked with `PRAGMA user_version`;
migrations are numbered SQL files `migrations/NNNN_<name>.sql`, applied at startup
in one transaction each. This DDL is the contract.

```sql
CREATE TABLE novels (
    id                TEXT PRIMARY KEY
                      CHECK (length(id) = 14 AND substr(id, 1, 2) = 'NV'
                             AND substr(id, 3) NOT GLOB '*[^a-z0-9]*'),
    title             TEXT NOT NULL UNIQUE,      -- also the scraper's novel identifier
    host              TEXT NOT NULL,
    toc_url           TEXT NOT NULL,
    cover_key         TEXT,                      -- static storage key; NULL = no cover
    followed          INTEGER NOT NULL DEFAULT 1 CHECK (followed IN (0, 1)),
    refresh_interval  INTEGER NOT NULL CHECK (refresh_interval > 0),  -- seconds, current
    next_refresh_at   TEXT NOT NULL,
    last_refreshed_at TEXT,                      -- NULL = initial import not done yet
    seen_at           TEXT,                      -- last time the novel page was opened
    lease_owner       TEXT,                      -- worker id
    lease_expires_at  TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    CHECK ((lease_owner IS NULL) = (lease_expires_at IS NULL))
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
    claimed_by   TEXT,
    updated_at   TEXT NOT NULL,
    UNIQUE (novel_id, number),
    CHECK ((state = 'pending') = (queued_at IS NOT NULL)),
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
    storage_key  TEXT NOT NULL UNIQUE,
    size_bytes   INTEGER NOT NULL CHECK (size_bytes >= 0),
    created_at   TEXT NOT NULL,
    CHECK (chapter_to >= chapter_from)
) STRICT;

CREATE INDEX exports_by_novel ON exports (novel_id, created_at);

CREATE TABLE push_subscriptions (
    id         TEXT PRIMARY KEY
               CHECK (length(id) = 14 AND substr(id, 1, 2) = 'PS'
                      AND substr(id, 3) NOT GLOB '*[^a-z0-9]*'),
    endpoint   TEXT NOT NULL UNIQUE,
    p256dh     TEXT NOT NULL,
    auth       TEXT NOT NULL,
    user_agent TEXT,
    created_at TEXT NOT NULL
) STRICT;

CREATE TABLE changes (
    seq       INTEGER PRIMARY KEY AUTOINCREMENT, -- SSE event id
    novel_id  TEXT NOT NULL,                     -- for ?novel_id filtering
    entity_id TEXT NOT NULL                      -- NV…/CH…/EX…; prefix = event type
              CHECK (substr(entity_id, 1, 2) IN ('NV', 'CH', 'EX')),
    payload   TEXT NOT NULL CHECK (json_valid(payload)),
    at        TEXT NOT NULL
) STRICT;

CREATE INDEX changes_by_novel  ON changes (novel_id, seq);
CREATE INDEX changes_by_entity ON changes (entity_id, seq);
CREATE INDEX changes_by_time   ON changes (at);
```

Notes:

- Every state-changing write appends to `changes` in the same transaction.
- Pruning: the API deletes rows older than 24 h on a timer, **except the newest
  row of each `entity_id`**, so every novel, chapter and export always keeps its
  latest event.
- Error messages are not stored on `chapters`. A chapter that fails gets
  `state = 'error'` and a `CH` change whose payload carries `"error": "<message>"`.
  `Chapter.error` in the API is read from the chapter's newest change when its
  state is `error`, and is `null` otherwise. Re-queuing writes a new change, so
  the old message stops being current without any clearing.
- The VAPID key pair lives in `/data/vapid.json`, not in the database, so a
  database restore never changes the server's push identity. It is generated on
  first start and never regenerated.
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
| `syncing` | `next_refresh_at <= now` or the novel is leased by a worker running a sync |
| `cover_url`, `Export.url` | `STATIC_BASE_URL` + storage key |

## 5. API

Base path `/api/v1`. JSON in/out. OpenAPI generated by FastAPI.

### 5.1 Routes

```
GET    /api/v1/settings
GET    /api/v1/hosts
GET    /api/v1/novels
POST   /api/v1/novels/preview
POST   /api/v1/novels
GET    /api/v1/novels/{novel_id}
PATCH  /api/v1/novels/{novel_id}
POST   /api/v1/novels/{novel_id}/sync
GET    /api/v1/novels/{novel_id}/chapters
PATCH  /api/v1/novels/{novel_id}/chapters
GET    /api/v1/novels/{novel_id}/exports
POST   /api/v1/novels/{novel_id}/exports
POST   /api/v1/push/subscriptions
DELETE /api/v1/push/subscriptions/{subscription_id}
GET    /api/v1/events
```

Static files (covers, EPUBs) are not API routes; they are addressed by URLs built
from `STATIC_BASE_URL` (§8.3).

### 5.2 Objects

**Host**: `{host: string, has_pagination: bool}`, read from the installed
scraper's decode guide.

**Novel**

```
id                 string          NV…
title              string
host               string
toc_url            string
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
error        string | null
has_images   bool
released_at  string | null
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
size_bytes   int
url          string
created_at   string
```

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
| `POST /novels/preview` | `{url}` | `{title, host, toc_url, cover_url}`; `cover_url` is the remote URL | `422 unsupported_host`, `502 upstream_failed` |
| `POST /novels` | `{title, toc_url, followed?: bool = true}` | `201 Novel` immediately, with no chapters and `syncing = true` | `409 title_exists`, `422 unsupported_host` |
| `GET /novels/{id}` | — | `Novel` | `404` |
| `PATCH /novels/{id}` | `{followed?: bool, seen?: true}` | `Novel` | `404`, `422` |
| `POST /novels/{id}/sync` | — | `202 Novel` | `404` |

- `POST /novels` sets `refresh_interval = REFRESH_MIN_INTERVAL` and
  `next_refresh_at = now`; the worker performs the initial import.
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
`downloaded` or `error` → `422`. Each updated chapter appends a `chapter` change.

### 5.5 Exports

| Route | Request | Response | Errors |
|---|---|---|---|
| `GET /novels/{id}/exports` | — | `Export[]` by `created_at` desc | `404` |
| `POST /novels/{id}/exports` | `{chapters: {start, end}, format?: "epub", chapters_per_book?: int}` | `201 Export[]` (one per book) | `404`, `422` (list selector, unsupported format, or range not fully `downloaded`; `details.missing: int[]`) |

`format` defaults to `"epub"`, the only format in v1. Formats live in a registry
in `exporter` mapping `format → (mime, extension, builder)`; `Export.format` is
derived from the stored `mime` through that registry, so adding PDF or FB2 later
needs no schema change. `chapters_per_book` defaults to the full range. Exports read only the database
and the cover, so they need no lease and can run while the worker is busy.

### 5.6 Settings and push

`GET /api/v1/settings` returns the server settings the UI needs at startup:

```
push_enabled           bool
vapid_key              string | null    VAPID public key (URL-safe base64); null when push is disabled
static_base_url        string
refresh_min_interval   int              seconds
refresh_max_interval   int              seconds
```

The UI loads it once, hides the notifications toggle when `push_enabled` is
false, and uses `vapid_key` for `PushManager.subscribe`.

| Route | Request | Response | Errors |
|---|---|---|---|
| `POST /push/subscriptions` | `{endpoint, keys: {p256dh, auth}}` | `201 {id}`; an existing `endpoint` is replaced and keeps its `id` (`200`) | `422` |
| `DELETE /push/subscriptions/{id}` | — | `204` | `404` |

### 5.7 Events (SSE)

`GET /api/v1/events?novel_id=<id>` (filter optional).

| Event | Source (`entity_id` prefix) | Data |
|---|---|---|
| `novel` | `NV` | `Novel` |
| `chapter` | `CH` | `Chapter` plus `novel_id` |
| `export` | `EX` | `Export` |
| `resync` | not stored | `{}`: client must refetch over REST |

- The event name is derived from the `entity_id` prefix; `changes` stores no kind.
- New chapters found by a sync arrive as `chapter` events with `released_at`
  set; the UI updates "new" badges from them. There is no separate release event.
- `id:` of every event is `changes.seq`.
- On connect with `Last-Event-ID`, the API replays later changes. Because pruning
  keeps the newest row per entity, older history has gaps: if the row with that
  seq no longer exists or is older than 24 h, the API sends one `resync` instead
  of replaying.
- The API polls `changes` about once per second and pushes new rows.
- A comment line is sent every 15 s as keepalive.

### 5.8 Status codes

`404` unknown or malformed ID; `409 title_exists`; `422` validation (including
FastAPI's own errors, reshaped into the error body); `502 upstream_failed`
(preview only); `503 push_disabled` (push routes when push is not configured);
`500` unexpected, generic message, traceback only in logs.

## 6. Worker

### 6.1 Loop

```
on start: recover()
loop:
    if shutting_down: exit
    recover()                                        # expired leases only
    if novel = lease_due_refresh():                  # next_refresh_at <= now, free lease,
        refresh(novel); release_lease(novel)         # oldest next_refresh_at first
        continue
    if novel = lease_next_download():                # oldest queued_at with free lease
        batch = claim(novel, BATCH_SIZE)             # lowest numbers → downloading
        for chapter in batch:
            if shutting_down: requeue(rest); break
            if state(chapter) != downloading: continue   # dequeued meanwhile
            try: fetch → convert → store → downloaded
            except: state = error; change payload carries the message
            renew_lease(novel); sleep(REQUEST_DELAY)
        release_lease(novel)
        continue
    sleep(IDLE_SLEEP)
```

- Each lease/claim step is one atomic transaction.
- `recover()` moves `downloading` chapters of novels whose lease expired back to
  `pending` and clears the lease.
- SIGTERM/SIGINT: finish the current chapter, requeue the rest of the batch,
  release the lease, exit 0.
- Errors in the loop itself (e.g. database locked) are logged, then back off 5 s,
  10 s, 20 s, … capped at `IDLE_SLEEP`.
- Refreshes take priority over downloads because they are short.

### 6.2 Refresh (TOC sync)

```
refresh(novel):
    initial = novel.last_refreshed_at is null
    try:
        toc = scraper_adapter.sync_toc(novel.title, novel.toc_url)   # [(number, url)]
    except:
        log; interval = min(interval * 2, MAX); schedule; return
    new = upsert chapters by number (existing rows keep state; changed URL updated)
    for each new chapter:
        released_at = null if initial else now
        state = pending (queued_at = now) if novel.followed and not initial else not_requested
    if cover_key is null: try fetch cover (failure is logged, never fatal)
    last_refreshed_at = now
    interval = MIN if new and not initial else min(interval * 2, MAX)
    next_refresh_at = now + interval ± 10% jitter
    append changes: one per new or changed chapter, one for the novel
    if new and not initial and novel.followed: notifier.notify_release(novel, numbers)
```

- Initial import never sets `released_at`, never auto-queues, never notifies.
- With `MIN = 6 h`, `MAX = 30 d` the interval walks 6 h → 12 h → 1 d → 2 d → 4 d
  → 8 d → 16 d → 30 d while nothing is found, and resets to 6 h on any release.
- A failed sync backs off the same way, so a dead site settles at monthly; a
  manual sync forces an immediate retry.

### 6.3 Novel lease

- One writer of a novel's scraper working files at a time. Only the worker takes
  leases (`lease_owner = worker id`, `lease_expires_at = now + LEASE_TTL`),
  renewed after each chapter.
- The API never touches scraper working files and never takes a lease.

### 6.4 Per-chapter pipeline

1. `scraper_adapter.fetch_chapter(title, number)` → `{title, body_html, http_session}`.
   The body is selected with the scraper decoder's content selector for the host;
   the scraper's own cleaner is not used.
2. `html_to_markdown.convert(body_html, fetch_image)` → `{markdown, has_images, warnings}`.
3. `chapter_store.put(chapter_id, markdown)`; the chapter row gets `title`,
   `has_images`, `state = downloaded`.

## 7. Modules

Package `novelhog`, under `src/novelhog/`.

| Module | Responsibility |
|---|---|
| `ids` | `new_id(prefix) -> str`, `is_valid(id, prefix) -> bool` |
| `config` | Reads `NOVELHOG_*` env vars into a typed settings object |
| `domain` | Chapter state rules, selector validation, refresh interval rule, domain exceptions (`NotFound`, `Conflict`, `ValidationFailed`, `UpstreamFailed`) |
| `repo` | All SQL, migrations, leases, claiming, change log. The only module that talks to SQLite |
| `scraper_adapter` | The only importer of `web_novel_scraper` (pinned `==2.13.1`). `supported_hosts()`, `preview(url)`, `sync_toc(title, toc_url) -> [(number, url)]` (creates the scraper novel on first use), `fetch_chapter(title, number)`, `find_cover_url(toc_html, toc_url)`. Builds scraper config from `NOVELHOG_*` (base dir `/data/work`, FlareSolverr URL, force FlareSolverr) |
| `scraper_adapter.overrides` | One module per host with fixes for scraper defects, registered when the adapter is imported. v1: `fanmtl_com` (next-page link written as `&gt;`) |
| `html_to_markdown` | Chapter body → Markdown (§8.1) |
| `image_fetcher` | Plain HTTP image download reusing FlareSolverr cookies and user agent; validation (§8.2) |
| `chapter_store` | `put(chapter_id, markdown)`, `get(chapter_id)` over `chapter_contents` |
| `storage` | `StaticStorage`: `put(key, data, mime, download_name=None)`, `delete(key)`, `url(key) -> str`. v1: `LocalStaticStorage` |
| `exporter` | Format registry (`format → mime, extension, accepted image types, builder`). EPUB: Markdown → XHTML (`markdown-it-py`, raw HTML disabled) → `ebooklib`; cover; inline images extracted to EPUB items and converted per §8.4 |
| `images` | Pillow helpers: `downscale(data, mime, max_dimension)`, `convert_for(data, mime, accepted) -> (data, mime)` |
| `notifier` | `notify_release(novel, numbers)`: sends one Web Push to every subscription via `pywebpush`; deletes subscriptions answered with `404`/`410`; logs other failures, no retry. VAPID key load/generate |
| `api` | FastAPI app, routers, SSE, error handlers, static mounts, `sw.js` and manifest |
| `worker` | Loop, refresh, downloads, signal handling, recovery |
| `cli` | `novelhog api`, `novelhog worker` |

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
- `LocalStaticStorage` writes under `/data/static`; the API serves it at
  `/static` with long-lived cache headers; export files are served with their
  stored `mime` and `Content-Disposition: attachment`.

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

### 8.6 Push notification

- Payload: `{title: <novel title>, body: "<N> new chapters (<first>–<last>)",
  url: <PUBLIC_URL>/novels/<id>, icon: <cover_url or app icon>}`.
- The service worker shows it and opens/focuses `url` on click.

## 9. Configuration

| Variable | Default | Meaning |
|---|---|---|
| `NOVELHOG_DATA_DIR` | `/data` | Holds `app.db`, `vapid.json`, `work/`, `static/` |
| `NOVELHOG_PUBLIC_URL` | — (required for push) | External HTTPS URL, used in push links and the VAPID claim |
| `NOVELHOG_VAPID_SUBJECT` | — (required for push) | `mailto:` contact for the VAPID claim |
| `NOVELHOG_FLARESOLVERR_URL` | `http://flaresolverr:8191` | |
| `NOVELHOG_STATIC_BACKEND` | `local` | Only `local` in v1 |
| `NOVELHOG_STATIC_BASE_URL` | `/static` | May be an absolute URL (separate domain/CDN) |
| `NOVELHOG_REFRESH_MIN_INTERVAL` | `21600` (6 h) | |
| `NOVELHOG_REFRESH_MAX_INTERVAL` | `2592000` (30 d) | |
| `NOVELHOG_BATCH_SIZE` | `10` | Chapters claimed per batch |
| `NOVELHOG_LEASE_TTL` | `300` s | |
| `NOVELHOG_IDLE_SLEEP` | `30` s | Worker sleep when no work |
| `NOVELHOG_REQUEST_DELAY` | `1` s | Pause between chapter fetches |
| `NOVELHOG_MAX_IMAGE_BYTES` | `2097152` | Download size cap per image |
| `NOVELHOG_IMAGE_MAX_DIMENSION` | `1600` | Longest side in px before downscaling; `0` disables |
| `NOVELHOG_WORKER_ID` | `<hostname>:<pid>` | |
| `NOVELHOG_HTTP_PORT` | `8080` | API listen port |

If push variables are missing, `GET /settings` reports `push_enabled: false` and
`vapid_key: null`, the subscription routes return `503 push_disabled`, and the UI
hides the notification toggle; everything else works.

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
2. **Add novel (dialog)**: URL → Preview (title, host, cover) → editable title,
   follow toggle (on by default) → Add. Closes immediately; the new card shows
   "Importing table of contents…" until the first `novel` event. `422
   unsupported_host` shows the supported-host list; `502` shows the message with
   Retry.
3. **Novel (`/novels/:id`)**: sends `PATCH {seen: true}` on open.
   - Header: cover, title, host, TOC link, follow toggle, Sync button,
     "checked 2 h ago · next in 4 h", counters.
   - Chapter grid: one cell per chapter coloured by state (`not_requested` grey,
     `pending` outlined, `downloading` pulsing, `downloaded` solid, `error` red),
     "new" marker for chapters released since the previous visit; hover/tap shows
     number, title, state, error.
   - Selection: click, shift-click range (touch: range toggle), From/To inputs,
     state filter chips.
   - Sticky action bar: Download, Cancel, Retry errors, Export EPUB… (range +
     chapters per book). PATCH results shown as a toast: "Queued N · skipped M".
   - Exports panel: filename, range, size, date, download link.
- **Follow toggle**: a pill button with a star icon (`aria-pressed`). Following:
  solid gold star, label "Following". Not following: grey outline star, label
  "Follow". Used on library cards, the novel header and the add dialog.
- **Device notifications** (only when `/settings` reports `push_enabled`):
  - Header: a small icon-only bell button (`aria-label` "Notifications on this
    device: on/off"); outline when this device is not subscribed, filled when it
    is. Clicking it subscribes or unsubscribes this device.
  - Banner below the header, shown only while this device is not subscribed and
    the banner has not been dismissed: "Get a notification on this device when a
    followed novel has new chapters." with **Turn on** and a dismiss (×) button.
    Dismissal is stored in `localStorage` (`novelhog.pushBannerDismissed`); if
    storage is unavailable the banner simply shows again next load. Subscribing
    hides it.
  - Subscribing requests permission, calls `PushManager.subscribe` with
    `vapid_key`, then `POST /push/subscriptions`; unsubscribing calls
    `unsubscribe()` and `DELETE`. Over plain HTTP the bell is disabled with a
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
- **Postgres** for multi-host clustering, behind the `repo` layer.
- **S3 static backend** (`S3StaticStorage`), setting `Content-Type` and
  `Content-Disposition` per object.
- **Image table**: move inline images of chapters with `has_images` into a
  content-addressed table.
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
- **Integration (real temporary SQLite):** lease and claim with two competing
  workers; recovery of expired leases; SIGTERM requeue; dequeue during a claimed
  batch; refresh scheduling and priority over downloads; initial import vs later
  release (`released_at`, auto-queue for followed only); `new_pages` vs
  `seen_at`; SSE replay from `Last-Event-ID` and `resync`; pruning keeps the
  newest change per entity; `Chapter.error` comes from the newest change only
  while the state is `error` and survives pruning.
- **Notifier (fake sender):** exactly one push per sync with new chapters of a
  followed novel; none for no new chapters, initial import or unfollowed novels;
  `410` deletes the subscription; VAPID keys stable across restarts.
- **API:** `httpx` against the app with a fake `scraper_adapter`; every route,
  success and each error code, including `503 push_disabled`.
- **Regression (offline HTML fixtures in the repo):** fanmtl `&gt;` pagination;
  chapter files located without relying on `main.json`; no scripts and no empty
  paragraphs in output.
- **Frontend:** Vitest + Testing Library for grid selection, SSE cache updates,
  the notifications toggle states, and the theme switcher (stored choice applied,
  System follows `prefers-color-scheme` changes, storage unavailable falls back
  to System); one Playwright smoke test of the main flow against a fake backend.
- No live-site tests in CI. `scripts/smoke_live.sh <url>` exists for manual
  checks.

## 14. Styleguide (to be written to `STYLEGUIDE.md` as the plan's first step)

- **Python:** 3.14, `uv`; `ruff format` + `ruff check`; `mypy --strict`;
  `pytest`. `snake_case`; tests named `test_<unit>__<behaviour>`, e.g.
  `test_claim_batch__skips_leased_novel`. No bare `except:`.
- **TypeScript:** strict mode; `eslint` + `prettier`; components
  `PascalCase.tsx`, hooks `useX.ts`, tests colocated as `*.test.tsx`.
- **Layout:**

  ```
  src/novelhog/{api,worker,domain,repo,scraper_adapter,storage,exporter,notifier,
                html_to_markdown,image_fetcher,images,chapter_store,ids.py,config.py,cli.py}
  migrations/
  web/src/{routes,components,api,hooks}   web/public/{sw.js,manifest.webmanifest}
  tests/{unit,integration,api,fixtures}
  docker/   compose.yaml   STYLEGUIDE.md   docs/superpowers/specs/
  ```
