# Home Assistant add-on: design

Date: 2026-10-26

## Goal

Let a Home Assistant user install novelhog as an add-on: add this repository to the add-on store, install, start, open the web UI. This spec covers the add-on with **port access**. **Ingress** (novelhog inside the Home Assistant sidebar, behind Home Assistant's login) is a separate follow-up spec, because it needs base-path support across the SPA, Caddy and the API.

Builds on `docs/superpowers/specs/2026-10-26-readme-docs-publishing-design.md`: the published multi-arch image, the docs site and the `publish` job in `ci.yml`.

## Facts from the Home Assistant docs that shape the design

- An add-on config has no key for the entrypoint or command: a container always starts its image's own default. The add-on therefore needs an image whose default command runs novelhog the add-on way.
- With `image:` set in `config.yaml`, the add-on `version` is also the tag the Supervisor pulls.
- `init: false` is for images that have their own init (this one has `tini`). Architectures are `amd64` and `aarch64`.
- Options live in `/data/options.json`; persistent data in `/data`. (To verify while building: whether `/data` needs an explicit `map` entry; the docs are silent.)

## Decisions

- **Separate image and Dockerfile** (decided with the user): `homeassistant/novelhog/Dockerfile`, a thin image `FROM ghcr.io/t3hk0d3/novelhog:${BASE_TAG}`. It installs `jq` (to read the options file), copies in `run.sh`, switches to `USER root` (a Supervisor-managed `/data` is not owned by uid 10001, and the add-on container is already isolated), sets the OCI labels Home Assistant expects, and sets `ENTRYPOINT ["tini", "--", "/run.sh"]`. The app is not rebuilt. It is published as `ghcr.io/t3hk0d3/novelhog-addon`, multi-arch.
- **FlareSolverr is external** (decided with the user): an option `flaresolverr_url`, empty by default. Users run a FlareSolverr add-on, or one elsewhere, and paste its address.
- **Ingress is out of scope** here (separate spec). Access is the published port plus an "Open Web UI" button.
- **Startup is a bash script, not an app command** (decided with the user): everything specific to Home Assistant lives in `homeassistant/novelhog/run.sh`, so the `novelhog` application and its CLI know nothing about Home Assistant and no application code changes.

## 1. The startup script

`homeassistant/novelhog/run.sh` (bash, `set -euo pipefail`; passes `shellcheck`), the image's entrypoint behind `tini`.

- Reads the options file, `/data/options.json` (path overridable with `OPTIONS_FILE`, for tests), with `jq`, and exports the same environment variables the application reads today: `public_url` -> `NOVELHOG_PUBLIC_URL`, `vapid_subject` -> `NOVELHOG_VAPID_SUBJECT`, `allowed_hosts` (a list) -> `NOVELHOG_ALLOWED_HOSTS` (comma-joined), `flaresolverr_url` -> `NOVELHOG_FLARESOLVERR_URL`, `database_url` -> `NOVELHOG_DATABASE_URL`. An empty or absent option is not exported, so the application default applies (with no FlareSolverr URL, the worker reports an infrastructure error only for Cloudflare-protected sites, as today; with no database URL, the database is SQLite in `/data/db`, as in the normal image).
- Exports `NOVELHOG_DATA_DIR=/data` (the image already uses `/data/db` and `/data/static`: covers and exports always live there). A non-empty `database_url` (for example `postgresql+psycopg://user:password@host:5432/novelhog`; the PostgreSQL driver is already in the image) moves only the database; both the migration and the two roles see it, because it is exported before any of them starts.
- Runs `novelhog db migrate` and stops with its exit code if that fails.
- Starts `novelhog web` and `novelhog worker` in the background, and waits for the first to exit.
- On SIGTERM or SIGINT forwards TERM to both and waits for them (the worker finishes its chapter; the Supervisor's stop timeout applies), then exits 0.
- If either child exits on its own, terminates the other and exits non-zero, so the Supervisor restarts the add-on.
- A missing options file means "all defaults" (so `docker run` outside Home Assistant works); a malformed one exits non-zero, naming the file, before anything starts.

## 2. The add-on definition

`homeassistant/novelhog/`:

- `config.yaml`: `name`, `slug: novelhog`, `description`, `version` (equals the project version), `arch: [amd64, aarch64]`, `image: ghcr.io/t3hk0d3/novelhog-addon`, `init: false`, `startup: application`, `boot: auto`, `ports: {"8080/tcp": 8080}` with a description, `webui: "http://[HOST]:[PORT:8080]"`, `options` (defaults) and `schema` (`public_url: url?`, `vapid_subject: str?`, `allowed_hosts: [str]?`, `flaresolverr_url: url?`, `database_url: password?`; the `password` type keeps the connection string, which holds a credential, masked in the Home Assistant UI), `map: [data]` if the build-time check shows it is needed.
- `Dockerfile` and `run.sh` (above), `icon.png` and `logo.png` (from `docs/assets`), `DOCS.md` (what Home Assistant shows on the add-on's Documentation tab: options, the no-login warning, push needs HTTPS, FlareSolverr), `CHANGELOG.md`.
- `repository.yaml` at the repository root: `name`, `url`, `maintainer`, so users add `https://github.com/t3hk0d3/novelhog` as an add-on repository.

## 3. Publishing and versioning

- A `publish-addon` job in `ci.yml`, `needs: publish`, same trigger and permissions. It builds `homeassistant/novelhog/Dockerfile` for `linux/amd64,linux/arm64` with `BASE_TAG` set to the most specific tag the `publish` job just pushed (the version on a `v*` tag, `sha-<short>` on `main`), and pushes `ghcr.io/t3hk0d3/novelhog-addon` with the same tag set (`latest` + `sha-<short>`, or `1.2.3` + `1.2`).
- Release rule: the add-on `version` in `config.yaml` equals the project version in `pyproject.toml`, and on a `v*` tag equals the tag. A release is "bump both, tag". A CI check (a small test plus a tag step) enforces it, so the Supervisor never asks for an image tag that does not exist.
- The package `novelhog-addon` is made public once (as for `novelhog`).

## 4. Tests and checks

The script is tested as it ships: a pytest module runs `run.sh` in a subprocess with `OPTIONS_FILE` pointing at a temporary file and a directory of stub executables first on `PATH` (a stub `novelhog` records its arguments and environment, then sleeps or exits as the test directs). No Docker and no application code are involved; the tests skip when `jq` is missing. BDD skeleton (one test per effect), in `tests/addon/test_run_script.py` (with an `__init__.py`, like the other test directories):

```
homeassistant/novelhog/run.sh
    With every option set
        - novelhog web and novelhog worker see NOVELHOG_PUBLIC_URL, NOVELHOG_VAPID_SUBJECT,
          NOVELHOG_ALLOWED_HOSTS (comma-joined), NOVELHOG_FLARESOLVERR_URL and NOVELHOG_DATABASE_URL
        - novelhog db migrate sees NOVELHOG_DATABASE_URL too
        - they see NOVELHOG_DATA_DIR=/data
    With empty options
        - NOVELHOG_FLARESOLVERR_URL, NOVELHOG_PUBLIC_URL, NOVELHOG_VAPID_SUBJECT and
          NOVELHOG_DATABASE_URL are not set (the application's SQLite default applies)
    With no options file
        - starts with defaults
    With a malformed options file
        - exits non-zero with a message naming the file
        - starts no novelhog process
    Starting
        - runs `novelhog db migrate` before web and worker start
        - starts `novelhog web` and `novelhog worker`
    When the migration fails
        - exits with the migration's exit code
        - starts neither web nor worker
    On SIGTERM
        - both children receive SIGTERM
        - the script waits for them and exits 0
    When a child exits on its own
        - the other child receives SIGTERM
        - the script exits non-zero

homeassistant/novelhog/config.yaml (contract, tests/addon/test_config.py)
    - version equals the pyproject.toml version
    - image is ghcr.io/t3hk0d3/novelhog-addon
    - arch is amd64 and aarch64
    - port 8080/tcp is exposed and the webui points at it
    - every option in `options` has a `schema` entry and vice versa
    - repository.yaml names the repository URL

addon image (container smoke, gated like the others: NOVELHOG_CONTAINER_TESTS=1)
    - built FROM the main image, it starts and serves GET /api/v1/settings on 8080
    - with an options file mounted at /data/options.json, the options reach the app
    - with a root-owned /data mounted and no database_url, the database is written under /data/db
```

`config.yaml` is parsed in the test (PyYAML in the `dev` group). CI also runs `shellcheck` on `run.sh` and the official add-on linter (`frenck/action-addon-linter`) on `homeassistant/novelhog`. Style follows `STYLEGUIDE.md` (module docstrings, full typing, `test_<unit>__<behaviour>`, scenario classes).

## 5. Docs

- `docs/home-assistant.md` (nav: after Install): add the repository, install, options table, first start, the no-login warning, HTTPS/push notes (a reverse proxy or Home Assistant's own Nabu Casa URL is the user's choice), FlareSolverr via an external add-on, the options table (including `database_url`, empty by default for SQLite, with a PostgreSQL example), and backups: the add-on's `/data` is included in Home Assistant backups, but an external database is not, so back it up separately (`pg_dump`).
- A short Home Assistant mention and badge-style link in the README ("Add to Home Assistant" button URL `https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Ft3hk0d3%2Fnovelhog`).
- `STYLEGUIDE.md` layout line gains `homeassistant/` and `tests/addon/`.

## Out of scope / future work

- **Ingress** (own spec): base-path support in the SPA, Caddy, API origin/host checks, SSE and the service worker; `ingress: true` in `config.yaml`.
- Bundling FlareSolverr in the add-on.
- Home Assistant integration entities, notifications to HA, backups hooks.
