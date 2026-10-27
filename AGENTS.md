# novelhog Agent Guidelines

This file serves as the definitive reference for AI agents working in this repository. Follow all conventions and architectural rules outlined below.

---

## 1. Project Overview

**novelhog** is a self-hosted web UI, API, and background worker for downloading, following, and reading web novels in a self-owned library. It supports automated chapter scraping, live reading via a mobile-friendly PWA, push notifications, and EPUB export.

- **Backend**: Python 3.14 (managed via `uv`), FastAPI, SQLAlchemy 2 (Alembic), structlog.
- **Frontend**: React 19, TypeScript (strict mode), Vite, Tailwind CSS v4, TanStack Query v5, React Router v8.
- **Testing & Verification**: Pytest, Vitest, Playwright, Ruff, Mypy (`--strict`), import-linter.
- **Deployment**: Multi-arch Docker container (Caddy + FastAPI + Worker), Home Assistant Add-on.

---

## 2. Repository Layout

```
src/novelhog/
├── api/                  # FastAPI HTTP and SSE endpoints; request validation; schema export
├── domain/               # Pure domain models, business validation, domain errors
├── repo/                 # Database schema, migrations, transactional data access, RepoContext
│   ├── migrations/       # Alembic migration scripts
│   └── task_kinds/       # Transactional half of task handling (NO network I/O)
├── worker/               # Background task queue & scheduler runner
│   └── tasks/            # Network/processing half of task handling (NO DB transactions)
├── scraper_adapter/      # Adapter wrapping web-novel-scraper (isolated boundary)
├── exporter/             # EPUB building and novel book export
├── notifications/        # Web push and notification dispatching
├── chapter_store.py      # Storage abstraction for downloaded chapter text
├── clearance.py          # Cloudflare clearance / FlareSolverr integration
├── cli.py                # CLI commands (serve, worker, db migrate, etc.)
├── config.py             # App configuration and environment variable parsing
├── html_to_markdown.py   # HTML cleanup and markdown conversion for reader
├── ids.py                # Unique ID generators and format validation
├── image_fetcher.py      # Cover art fetching and caching
├── images.py             # Image manipulation and resizing
├── log.py                # Structlog configuration
├── net.py                # Network client utilities
├── storage.py            # Local filesystem disk storage management
└── vapid.py              # Web Push VAPID keys handling
web/                      # React 19 SPA frontend
├── src/
│   ├── api/              # API client and generated OpenAPI TypeScript types (schema.d.ts)
│   ├── components/       # UI components (PascalCase.tsx) and colocated tests (*.test.tsx)
│   ├── hooks/            # Custom React hooks (useX.ts)
│   ├── routes/           # React Router route pages and views
│   └── theme/            # Theme management (Light / Dark / System)
tests/
├── unit/                 # Pure unit tests (isolated domain, text processing, utils)
├── integration/          # Integration tests using real temporary SQLite/PostgreSQL
│   ├── repo/             # Database access and locking tests
│   ├── worker/           # Worker task coordination tests
│   └── notifications/    # Push notifications tests
├── api/                  # API endpoints and SSE stream tests
├── container/            # Docker container smoke tests (requires NOVELHOG_CONTAINER_TESTS=1)
├── addon/                # Home Assistant add-on tests
└── conftest.py           # Pytest fixtures (FakeClock, migrated_database, etc.)
tools/                    # Tooling, including custom import-linter contracts
scripts/                  # Verification and automation scripts (check.sh, test_pg.sh, etc.)
docker/                   # Dockerfile and Caddyfile
homeassistant/novelhog/   # Home Assistant Add-on configuration and runner
docs/                     # MkDocs documentation site source
```

---

## 3. Strict Architectural Boundaries

These boundaries are strictly enforced in CI via `import-linter` (`uv run lint-imports`):

1. **`novelhog.domain` is pure**:
   - May only import standard library and `novelhog.ids`.
   - Never import database models, SQLAlchemy, HTTP clients, or framework code.
   - Raises domain errors (`NotFound`, `Conflict`, `ValidationFailed`).

2. **`novelhog.repo` is standalone**:
   - May only import `sqlalchemy`, `alembic`, `novelhog.ids`, `novelhog.domain`, and stdlib.
   - **Never logs directly**: returns data transfer objects / reports and raises domain errors.
   - **No external `Connection` parameter**: transactions are strictly opened and composed inside `repo`.
   - Sub-repositories (`Repo.events`, `.novels`, `.tasks`, etc.) receive a single frozen `RepoContext` (engine, clock, config).
   - **Lock Order**:
     - Transactions writing novel rows must first call `novels.lock_novel` (no-op `UPDATE novels`, never `SELECT ... FOR UPDATE`) and abandon if it returns `False`.
     - In event-writing transactions: all row writes and locking reads first; `events.append` is called last; no new locks after the first append.
     - Snapshots are created lazily inside `events.append` via callable payloads after the clock is sampled.

3. **`novelhog.api` is pure**:
   - Forbidden from importing `scraper_adapter`, `exporter`, `images`, `image_fetcher`, `html_to_markdown`, `notifications.channels`, `notifications.dispatcher`, `net`, `worker`, `PIL`, `pywebpush`, or `web_novel_scraper`.
   - Maps domain errors to HTTP status codes with `snake_case` error codes.
   - All wire timestamps must be RFC 3339 UTC with milliseconds and `Z` (e.g. `2026-10-27T12:00:00.000Z`).

4. **`novelhog.scraper_adapter` is isolated**:
   - The **only** package permitted to import `web_novel_scraper`. The rest of the codebase must never directly import it.

5. **Task Execution Separation**:
   - **Task Handlers** (`repo/task_kinds/`): The transactional half of a task. They run in DB transactions and **never do network I/O**.
   - **Task Processors** (`worker/tasks/`): The network/compute half of a task. They execute scraping, network downloads, and image processing, and **never open DB transactions**.
   - Task log events follow the naming convention `task_<verb>` (e.g. `task_started`, `task_retry`).

---

## 4. Development & Quality Gate Commands

### The Complete Verification Gate
Before completing any task or pushing changes, run:
```bash
./scripts/check.sh
```
This runs the full suite in order and fails fast:
1. `uv run ruff format --check`
2. `uv run ruff check`
3. `uv run mypy --strict src tests tools`
4. `uv run lint-imports`
5. `uv run pytest`
6. `npm --prefix web run check`

> [!IMPORTANT]
> **Restart/Rebuild Dev Docker Compose**: After changes are complete and quality gates pass, always restart or rebuild the development Docker Compose environment so the running stack reflects your updates:
> ```bash
> docker compose -f compose.dev.yaml up -d --build
> ```

### Python Commands (`uv`)
- **Format code**: `uv run ruff format`
- **Lint code**: `uv run ruff check --fix`
- **Typecheck**: `uv run mypy --strict src tests tools`
- **Check architectural boundaries**: `uv run lint-imports`
- **Run all tests**: `uv run pytest`
- **Run a specific test**: `uv run pytest tests/unit/domain/test_chapters.py`
- **Run Postgres integration tests**: `./scripts/test_pg.sh` (spins up temporary Docker PG 16 container)

### Frontend Commands (`web/`)
- **Full frontend check**: `npm --prefix web run check` (runs lint, typecheck, check:api, test)
- **Lint & format check**: `npm --prefix web run lint`
- **Format with Prettier**: `npm --prefix web run format`
- **Typecheck**: `npm --prefix web run typecheck`
- **Run Vitest tests**: `npm --prefix web run test`
- **Generate API types from OpenAPI**:
  ```bash
  uv run python -m novelhog.api.export_openapi | npx --prefix web openapi-typescript -o web/src/api/schema.d.ts
  # Or use the npm script:
  npm --prefix web run gen:api
  ```
- **Check API schema drift**: `npm --prefix web run check:api`
- **Run E2E tests**: `npm --prefix web run e2e`

### Documentation & Compose
- **Serve documentation**: `uv run --group docs mkdocs serve`
- **Start dev environment with watch**: `docker compose -f compose.dev.yaml up --build --watch`
- **Restart / rebuild dev environment**: `docker compose -f compose.dev.yaml up -d --build` (or restart individual services: `docker compose -f compose.dev.yaml restart novelhog-web novelhog-worker`)

---

## 5. Coding & Testing Conventions

### Python Style
- Python 3.14; line length 100 characters.
- `snake_case` for modules, functions, methods, and variables; `PascalCase` for classes.
- **Module docstrings**: Every module must begin with a clear module docstring.
- **Typing**: All public functions and methods must be fully typed.
- **Error handling**: Avoid bare `except:`. Domain errors live in `novelhog.domain`.

### Python Testing Conventions
- All test directories must contain an `__init__.py`.
- **Test naming**: `test_<unit>__<behaviour>`, e.g.:
  `test_claim_batch__skips_claimed_novel`
- **Class grouping**: Tests are grouped in BDD scenario classes:
  - `class TestClaimBatch__OnClaimedNovel:`
  - `class TestNovelRefresh__<scenario>:`
  - `class TestChapterDownload__<scenario>:`
  - `class TestExportBuild__<scenario>:`
- **Integration testing rule**: Integration tests run against real temporary databases (SQLite by default, PostgreSQL via `NOVELHOG_TEST_DATABASE_URL`). **Never mock `repo`** in integration tests.
- **Deterministic time**: Use `FakeClock` from `tests/conftest.py` instead of mocking `datetime.now()` or sleeping.

### TypeScript / React Style
- TypeScript strict mode enabled; styled with Tailwind CSS v4.
- **Components**: `PascalCase.tsx`, colocated tests `PascalCase.test.tsx`.
- **Hooks**: `useX.ts`, colocated tests `useX.test.ts`.
- **TanStack Query Keys**: Maintain canonical query key patterns:
  - `['novels']`
  - `['novel', id]`
  - `['chapters', novelId]`
  - `['chapter-content', novelId, chapterId, updatedAt]` (includes `updatedAt` so re-downloads trigger refetch)
  - `['exports', novelId]`
  - `['settings']`
  - `['live-probe']`

### Git & Commit Guidelines
- Commit message format: `<type>: <summary>`
  - Permitted types: `feat`, `fix`, `test`, `docs`, `chore`, `refactor`.
  - Example: `fix: cycle theme through system light and dark`
