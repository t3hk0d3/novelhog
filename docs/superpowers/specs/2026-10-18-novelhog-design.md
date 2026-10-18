# novelhog — Design Spec

- **Date:** 2026-10-18
- **Status:** Approved in brainstorming; pending written-spec review
- **Repo:** `~/dev/novel-shelf` (directory name is incidental; the project is `novelhog`)

## 1. Purpose

novelhog is a self-hosted web UI around
[`web-novel-scraper`](https://github.com/ImagineBrkr/web-novel-scraper). It lets
one person on a home LAN keep a library of web novels, queue chapters for
background download, and export chapter ranges to EPUB for phones and e-readers.

### Success criteria

1. Add a novel from any URL whose host `web-novel-scraper` supports; the title and
   cover are detected, and the TOC is synced.
2. Queue a single chapter or a range; downloads continue with the browser closed
   and progress is visible live from any device.
3. Export a downloaded range to an EPUB with a cover, without ads or empty
   paragraphs, and download it from any LAN device.
4. Running `docker compose up` next to an existing FlareSolverr is the whole
   deployment.

### In scope (v1)

- Library, add novel (preview → create), TOC sync.
- Chapter queueing (single, list, range), dequeue, retry errors, live progress.
- EPUB export and download.
- Cover detection and display.

### Out of scope (v1)

- In-browser reader, full-text search, metadata editing, deleting novels.
- Authentication (LAN-only, no auth; a reverse proxy can add it later).
- Manual cover upload.
- Splitting site "pages" into the novel's real chapters (titles are shown as the
  site provides them, e.g. fanmtl's "Page N").
- S3 static backend (the interface exists; only the local backend ships).
- Worker wake-up ("poke"); see §11.

## 2. Architecture

```
 Browser (React SPA)
      │  REST /api/v1  +  SSE /api/v1/events      static: STATIC_BASE_URL (/static)
      ▼
 api (FastAPI) ───────── reads/writes ─────────►  SQLite  /data/app.db  (WAL)
      │  serves SPA at /, static files at /static        ▲
      │                                                  │ lease novel, claim batch,
      ▼                                                  │ write content + changes
 /data/static  ◄──── writes covers/exports ──── worker ──┘
                                                  │ web_novel_scraper (library)
                                                  ▼
                                             FlareSolverr ──► novel sites
```

| Component | Responsibility |
|---|---|
| `api` | FastAPI app. REST under `/api/v1`, SSE stream, serves the built SPA at `/` and local static files at `/static`. Performs short synchronous operations: preview, export, and create/sync (the latter two under the novel lease). |
| `worker` | Separate process/container from the same image. Polls SQLite for `pending` chapters, leases a novel, claims a batch, downloads → converts → stores each chapter. |
| `web` | React SPA, built to static files and served by `api` (single origin, no CORS). |
| SQLite | Single source of truth: novels, chapters, chapter content (Markdown), exports, change log. Accessed only through the repository layer. |
| Static storage | Covers and EPUB files, behind `StaticStorage`; v1 backend is local disk under `/data/static`. |

### Deployment

- `compose.yaml` with services `novelhog-api` and `novelhog-worker`, one image,
  different commands (`novelhog api`, `novelhog worker`).
- Both mount the `/data` volume and join FlareSolverr's Docker network.
- `novelhog-api` publishes one LAN port (default `8080`).
- Scaling workers (`--scale novelhog-worker=N`) is safe on one host; multi-host
  clustering requires a networked database (§11).

## 3. Identifiers

- Format: two-letter uppercase type prefix + 12 random characters from
  `[a-z0-9]`, generated with `secrets`.
  - `NV` novel, `CH` chapter, `EX` export. Example: `CHk3x9q2m7b4wz`.
- 36¹² ≈ 4.7·10¹⁸ (~62 bits). Primary keys are unique; an insert that collides is
  retried with a fresh ID.
- IDs carry no ordering. Every listing orders by an explicit column
  (`created_at`, `number`).
- An ID with the wrong prefix or format in a path or body is treated as unknown →
  `404`.

## 4. Data model (SQLite)

All timestamps are UTC ISO-8601 strings. Schema is the contract; column types are
SQLite affinities.

### `novels`

| Column | Type | Notes |
|---|---|---|
| `id` | TEXT PK | `NV…` |
| `title` | TEXT UNIQUE | Also the scraper's novel identifier |
| `host` | TEXT | From the TOC URL |
| `toc_url` | TEXT | |
| `cover_key` | TEXT NULL | Static storage key |
| `lease_owner` | TEXT NULL | Worker ID or `api:<uuid>` |
| `lease_expires_at` | TEXT NULL | |
| `created_at`, `updated_at` | TEXT | |

### `chapters`

| Column | Type | Notes |
|---|---|---|
| `id` | TEXT PK | `CH…` |
| `novel_id` | TEXT FK | |
| `number` | INTEGER | 1-based position in the TOC; `UNIQUE(novel_id, number)` |
| `url` | TEXT | |
| `title` | TEXT NULL | Set after download |
| `state` | TEXT | `not_requested \| pending \| downloading \| downloaded \| error` |
| `error` | TEXT NULL | Short message for `error` |
| `has_images` | INTEGER | 0/1 |
| `queued_at` | TEXT NULL | Set when moved to `pending` |
| `claimed_by` | TEXT NULL | Worker ID while `downloading` |
| `updated_at` | TEXT | |

Index: `(state, novel_id, number)` for claiming.

### `chapter_contents`

| Column | Type | Notes |
|---|---|---|
| `chapter_id` | TEXT PK FK | |
| `markdown` | TEXT | Chapter body; images inline as `data:` URIs |
| `fetched_at` | TEXT | |

### `exports`

| Column | Type | Notes |
|---|---|---|
| `id` | TEXT PK | `EX…` |
| `novel_id` | TEXT FK | |
| `start`, `end` | INTEGER | Inclusive chapter numbers |
| `filename` | TEXT | `<Title> Chapters <start>-<end>.epub` |
| `storage_key` | TEXT | |
| `size_bytes` | INTEGER | |
| `created_at` | TEXT | |

### `changes`

| Column | Type | Notes |
|---|---|---|
| `seq` | INTEGER PK AUTOINCREMENT | SSE event ID |
| `novel_id` | TEXT | |
| `kind` | TEXT | `chapter \| novel \| export` |
| `payload` | TEXT | JSON event body (§5.6) |
| `at` | TEXT | |

Every state-changing write appends to `changes` in the same transaction. Rows
older than 24 h are pruned by the API on a timer.

## 5. API

Base path `/api/v1`. JSON in/out. OpenAPI generated by FastAPI.

### 5.1 Routes

```
GET   /api/v1/hosts
GET   /api/v1/novels
POST  /api/v1/novels/preview
POST  /api/v1/novels
GET   /api/v1/novels/{novel_id}
POST  /api/v1/novels/{novel_id}/sync
GET   /api/v1/novels/{novel_id}/chapters
PATCH /api/v1/novels/{novel_id}/chapters
GET   /api/v1/novels/{novel_id}/exports
POST  /api/v1/novels/{novel_id}/exports
GET   /api/v1/events
```

Static files (covers, EPUBs) are not API routes; they are addressed by URLs built
from `STATIC_BASE_URL` (§7).

### 5.2 Objects

**Host**: `{host: string, has_pagination: bool}`, read from the installed
scraper's decode guide.

**Novel**

```
id                 string   NV…
title              string
host               string
toc_url            string
cover_url          string | null    STATIC_BASE_URL-based URL
total_pages        int
downloaded_pages   int
pending_pages      int              pending + downloading
error_pages        int
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
updated_at   string
```

**Export**

```
id           string   EX…
novel_id     string
filename     string
start        int
end          int
size_bytes   int
url          string   STATIC_BASE_URL-based URL
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
| `GET /novels` | — | `Novel[]` ordered by `created_at` desc | — |
| `POST /novels/preview` | `{url}` | `{title, host, toc_url, cover_url}`; `cover_url` is the remote URL | `422 unsupported_host`, `502 upstream_failed` |
| `POST /novels` | `{title, toc_url}` | `201 Novel` after TOC sync and best-effort cover fetch | `409 title_exists`, `422 unsupported_host`, `502 upstream_failed` |
| `GET /novels/{id}` | — | `Novel` | `404` |
| `POST /novels/{id}/sync` | — | `Novel` | `404`, `409 novel_leased`, `502` |

Sync upserts chapters by `number`: new numbers are inserted as `not_requested`;
existing rows keep their state; a changed URL for an existing number updates the
URL. Cover is fetched only if `cover_key` is null.

### 5.4 Chapters

`GET /novels/{id}/chapters?offset=0&limit=1000&state=<state>` →
`{items: Chapter[], total: int}` ordered by `number`.

`PATCH /novels/{id}/chapters`

```
request:  {state: "pending" | "not_requested", chapters: <selector>}
response: {updated: int[], skipped: [{number: int, state: <current state>}]}
```

Allowed client transitions:

| From | To | Effect |
|---|---|---|
| `not_requested`, `error` | `pending` | Sets `queued_at`; keeps `error` until the next attempt |
| `pending` | `not_requested` | Clears `queued_at` and `error` |

Chapters in any other state are listed in `skipped`. Requesting `downloading`,
`downloaded` or `error` → `422`. Each updated chapter appends a `chapter` change.

### 5.5 Exports

| Route | Request | Response | Errors |
|---|---|---|---|
| `GET /novels/{id}/exports` | — | `Export[]` by `created_at` desc | `404` |
| `POST /novels/{id}/exports` | `{chapters: {start, end}, chapters_per_book?: int}` | `201 Export[]` (one per book) | `404`, `422` (list selector, or range not fully `downloaded`; `details.missing: int[]`) |

`chapters_per_book` defaults to the full range (one book). Exports read only
`chapter_contents` and the cover, never the scraper's working files, so they do
not take the novel lease and can run while the worker downloads other chapters.

### 5.6 Events (SSE)

`GET /api/v1/events?novel_id=<id>` (filter optional).

| Event | Data |
|---|---|
| `chapter` | `{novel_id, chapter_id, number, state, error, title}` |
| `novel` | `Novel` |
| `export` | `Export` |
| `resync` | `{}`: client must refetch over REST |

- `id:` of every event is `changes.seq`.
- On connect with `Last-Event-ID`, the API replays later changes; if that seq has
  been pruned it sends one `resync`.
- The API polls `changes` about once per second and pushes new rows.
- A comment line is sent every 15 s as keepalive.

### 5.7 Status codes

`404` unknown or malformed ID; `409` conflict (`novel_leased`, `title_exists`);
`422` validation (including FastAPI's own validation errors, reshaped into the
error body); `502` upstream (site or FlareSolverr) failure; `500` unexpected,
generic message, traceback only in logs.

## 6. Worker

### 6.1 Loop

```
on start:  recover()
loop:
    if shutting_down: exit
    recover()                                   # cheap; expired leases only
    novel = lease_next_novel()                  # oldest queued_at among novels with
                                                # pending chapters and free/expired lease
    if novel is none:
        sleep(IDLE_SLEEP); continue
    batch = claim(novel, BATCH_SIZE)            # lowest numbers first → downloading
    for chapter in batch:
        if shutting_down: requeue(rest of batch); break
        if state(chapter) != downloading: continue   # dequeued meanwhile
        try: fetch → convert → store → downloaded
        except: error(message)
        renew_lease(novel); sleep(REQUEST_DELAY)
    release_lease(novel)
```

- `lease_next_novel` and `claim` are each one atomic transaction.
- `recover()` moves `downloading` chapters of novels whose lease has expired back
  to `pending` and clears the lease.
- On SIGTERM/SIGINT: finish the current chapter, requeue the rest of the batch
  (`downloading` → `pending`), release the lease, exit 0.
- Errors in the loop itself (e.g. database locked) are logged, followed by a
  backoff of 5 s, 10 s, 20 s, … capped at `IDLE_SLEEP`.

### 6.2 Novel lease

- One writer per novel at a time, because the scraper rewrites its working
  `main.json` on TOC operations.
- The worker holds the lease while processing a batch and renews it after each
  chapter (`lease_expires_at = now + LEASE_TTL`).
- The API takes the same lease (`owner = api:<uuid>`, short TTL) for `create`
  and `sync`, or answers `409 novel_leased`. Export does not need it (§5.5).

### 6.3 Per-chapter pipeline

1. `scraper_adapter.fetch_chapter(title, number)` → `{title, body_html, http_session}`.
   The body is selected with the scraper's decoder content selector for the host;
   the scraper's own cleaner is not used.
2. `html_to_markdown.convert(body_html, fetch_image)` → `{markdown, has_images, warnings}`.
3. `chapter_store.put(chapter_id, markdown)`; the chapter row gets `title`,
   `has_images`, `state = downloaded`, `error = null`.

## 7. Modules

Package `novelhog`, under `src/novelhog/`.

| Module | Responsibility |
|---|---|
| `ids` | `new_id(prefix) -> str`, `is_valid(id, prefix) -> bool` |
| `config` | Reads `NOVELHOG_*` env vars into a typed settings object |
| `domain` | Chapter state rules, selector validation, domain exceptions (`NotFound`, `Conflict`, `ValidationFailed`, `UpstreamFailed`) |
| `repo` | All SQL: novels, chapters, contents, exports, changes, leases, claiming. The only module that talks to SQLite |
| `scraper_adapter` | The only importer of `web_novel_scraper` (pinned `==2.13.1`). `supported_hosts()`, `preview(url)`, `create(title, toc_url)`, `sync_toc(title) -> [(number, url)]`, `fetch_chapter(title, number)`, `find_cover_url(toc_html, toc_url)`. Builds `SCRAPER_*` env/config from `NOVELHOG_*` (base dir `/data/work`, FlareSolverr URL, force FlareSolverr). |
| `scraper_adapter.overrides` | One module per host with fixes for scraper defects, registered when the adapter is imported. v1: `fanmtl_com` (next-page link written as `&gt;`) |
| `html_to_markdown` | Converts the chapter body to Markdown (§8.1) |
| `image_fetcher` | Plain HTTP image download reusing FlareSolverr cookies and user agent; validation (§8.2) |
| `chapter_store` | `put(chapter_id, markdown)`, `get(chapter_id)` over `chapter_contents` |
| `storage` | `StaticStorage` interface: `put(key, data, mime, download_name=None)`, `delete(key)`, `url(key) -> str`. v1: `LocalStaticStorage` |
| `exporter` | Markdown → XHTML (`markdown-it-py`, raw HTML disabled) → EPUB (`ebooklib`); cover; inline images extracted to EPUB items |
| `api` | FastAPI app, routers, SSE, error handlers, static mounts |
| `worker` | Loop, signal handling, recovery |
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

- Allowed tags: `p`, `br`, `em`/`i`, `strong`/`b`, `img`. Other tags are dropped,
  keeping their text.
- `script`, `style`, `noscript`, `iframe` and their contents are removed entirely.
- All text is escaped so Markdown metacharacters stay literal: backslash,
  asterisk, underscore, hash, greater-than, square brackets, backtick, pipe,
  tilde, and a leading `-`, `+` or `<digits>.` at the start of a line.
- Paragraphs are separated by one blank line; empty paragraphs are dropped.
- In-text headings such as "Chapter 29 …" remain plain paragraphs.
- `img` → `![alt](data:<mime>;base64,…)` via `image_fetcher`; failure →
  the literal text `[image unavailable]` plus a warning; the chapter still
  succeeds. `has_images` is true if at least one image was embedded.

### 8.2 Images (chapter images and covers)

- Fetched over plain HTTP with the cookies and user agent from the FlareSolverr
  session (FlareSolverr itself returns HTML only).
- Type detected from magic bytes; allowed: PNG, JPEG, GIF, WebP. SVG and
  anything else are rejected.
- Size cap `NOVELHOG_MAX_IMAGE_BYTES` (default 2 MiB).
- Chapter images are inlined in Markdown. Covers go to static storage.
- A cover failure never fails create or sync.

### 8.3 Static storage keys

- Covers: `covers/<novel_id>/<sha256[:16]>.<ext>` (content-addressed, cached as
  immutable).
- Exports: `exports/<novel_id>/<export_id>/<Title> Chapters <start>-<end>.epub`.
  The last path segment is the download filename.
- `LocalStaticStorage` writes under `/data/static` and the API serves it at
  `/static` with long-lived cache headers; EPUBs get
  `Content-Disposition: attachment`.

### 8.4 EPUB

- One XHTML document per chapter, in `number` order; its heading is the chapter
  title as stored.
- The cover, when present, is set as the book cover.
- Inline `data:` images become EPUB image items.
- Metadata: title, language from the scraper metadata if known, identifier =
  export ID.

## 9. Configuration

| Variable | Default | Meaning |
|---|---|---|
| `NOVELHOG_DATA_DIR` | `/data` | Holds `app.db`, `work/`, `static/` |
| `NOVELHOG_FLARESOLVERR_URL` | `http://flaresolverr:8191` | |
| `NOVELHOG_STATIC_BACKEND` | `local` | Only `local` in v1 |
| `NOVELHOG_STATIC_BASE_URL` | `/static` | May be an absolute URL (separate domain/CDN) |
| `NOVELHOG_BATCH_SIZE` | `10` | Chapters claimed per batch |
| `NOVELHOG_LEASE_TTL` | `300` s | |
| `NOVELHOG_IDLE_SLEEP` | `30` s | Worker sleep when no work |
| `NOVELHOG_REQUEST_DELAY` | `1` s | Pause between chapter fetches |
| `NOVELHOG_MAX_IMAGE_BYTES` | `2097152` | |
| `NOVELHOG_WORKER_ID` | `<hostname>:<pid>` | |
| `NOVELHOG_HTTP_PORT` | `8080` | API listen port |

## 10. UI

React 19 + TypeScript + Vite; TanStack Query for server state; React Router;
Tailwind CSS with a few local components; API types generated with
`openapi-typescript` from the FastAPI schema. One app-wide `EventSource` patches
the query cache from SSE events.

### Screens

1. **Library (`/`)**: novel cards with cover (placeholder if none), title, host,
   and a stacked bar of downloaded / pending / error over total; an activity
   indicator while chapters are `downloading`. "Add novel" button. Empty state
   with explanation.
2. **Add novel (dialog)**: URL → Preview (shows title, host, cover) → editable
   title → Add. Spinner text "Fetching table of contents…" (can take 10–60 s).
   `422 unsupported_host` shows the supported-host list; `502` shows the message
   with Retry.
3. **Novel (`/novels/:id`)**
   - Header: cover, title, host, TOC link, Sync button (shows `409` as "busy
     downloading, try later"), counters.
   - Chapter grid: one cell per chapter, coloured by state (`not_requested`
     grey, `pending` outlined, `downloading` pulsing, `downloaded` solid,
     `error` red); hover/tap shows number, title, state, error.
   - Selection: click, shift-click range (touch: range toggle), From/To inputs,
     state filter chips (all `not_requested`, all `error`).
   - Sticky action bar: Download (→ `pending`), Cancel (→ `not_requested`),
     Retry errors, Export EPUB… (dialog: range + chapters per book). PATCH
     results shown as a toast: "Queued N · skipped M (reason)".
   - Exports panel: filename, range, size, date, download link.
- Works at phone width; on narrow screens the action bar becomes a bottom sheet.
- Light/dark theme follows the system. Visual design is done during
  implementation with the `frontend-design` skill.

## 11. Future work (not part of v1)

- **Worker wake-up.** A `Waker` interface (`wait(timeout) -> bool` in the worker,
  `notify()` in the API). First implementation: UDP datagram to every address the
  worker service name resolves to, with the timed sleep kept as a fallback; the
  worker always re-checks the database. With Postgres: `LISTEN/NOTIFY`.
- **Postgres** for multi-host clustering, behind the existing `repo` layer.
- **S3 static backend** (`S3StaticStorage`), setting `Content-Type` and
  `Content-Disposition` per object.
- **Image table**: move inline images of chapters with `has_images` into a
  content-addressed table.
- In-browser reader, FTS5 search, manual cover upload, splitting pages into real
  chapters.

## 12. Error handling

- `domain` and `repo` raise domain exceptions; only `api` maps them to HTTP.
- The worker never dies on a single chapter: the chapter goes to `error` with a
  short message (`<ExceptionType>: <summary>`); the traceback is logged.
- The scraper's own retry settings apply to upstream requests; when exhausted, the
  chapter goes to `error` and the user retries from the UI.
- Logging: structured JSON to stdout (`structlog`), with `novel_id`,
  `chapter_id`, `worker_id` bound where relevant.

## 13. Testing

- **Unit:** `ids`; `html_to_markdown` (allowlist, escaping, empty paragraphs,
  images, failure placeholder); `image_fetcher` (magic bytes, SVG rejection, size
  cap); `find_cover_url` (each heuristic, relative URLs); `exporter` (order, cover,
  image extraction); chapter state rules; selector validation.
- **Integration (real temporary SQLite):** lease and claim, including two
  competing workers; recovery of expired leases; SIGTERM requeue; dequeue during a
  claimed batch; SSE replay from `Last-Event-ID` and `resync`.
- **API:** `httpx` against the app with a fake `scraper_adapter`; every route,
  success and each error code.
- **Regression (offline HTML fixtures in the repo):** fanmtl `&gt;` pagination;
  chapter files located without relying on `main.json`; no scripts and no empty
  paragraphs in output.
- **Frontend:** Vitest + Testing Library for grid selection and SSE cache updates;
  one Playwright smoke test of the main flow against a fake backend.
- No live-site tests in CI. `scripts/smoke_live.sh <url>` exists for manual
  checks.

## 14. Styleguide (to be written to `STYLEGUIDE.md` as the plan's first step)

- **Python:** 3.14, `uv`; `ruff format` + `ruff check`; `mypy --strict`;
  `pytest`. `snake_case`; tests named
  `test_<unit>__<behaviour>`, e.g. `test_claim_batch__skips_leased_novel`.
  No bare `except:`.
- **TypeScript:** strict mode; `eslint` + `prettier`; components
  `PascalCase.tsx`, hooks `useX.ts`, tests colocated as `*.test.tsx`.
- **Layout:**

  ```
  src/novelhog/{api,worker,domain,repo,scraper_adapter,storage,exporter,
                html_to_markdown,image_fetcher,chapter_store,ids.py,config.py,cli.py}
  web/src/{routes,components,api,hooks}
  tests/{unit,integration,api,fixtures}
  docker/   compose.yaml   STYLEGUIDE.md   docs/superpowers/specs/
  ```
