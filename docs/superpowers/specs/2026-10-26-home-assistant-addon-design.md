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

- **Separate image and Dockerfile** (decided with the user): `homeassistant/novelhog/Dockerfile`, a thin image `FROM ghcr.io/t3hk0d3/novelhog:${BASE_TAG}` that only switches to `USER root` (a Supervisor-managed `/data` is not owned by uid 10001, and the add-on container is already isolated), sets the OCI labels Home Assistant expects, and sets `CMD ["addon"]`. The app is not rebuilt. It is published as `ghcr.io/t3hk0d3/novelhog-addon`, multi-arch.
- **FlareSolverr is external** (decided with the user): an option `flaresolverr_url`, empty by default. Users run a FlareSolverr add-on, or one elsewhere, and paste its address.
- **Ingress is out of scope** here (separate spec). Access is the published port plus an "Open Web UI" button.
- **No second copy of the app**: the add-on's behaviour lives in the main image as a `novelhog addon` command, so the same code path is tested and shipped once.

## 1. `novelhog addon` command

In `src/novelhog/cli.py` (alongside `db`, `api`, `web`, `worker`), with the parsing of the options file in its own small module so it is testable without processes.

- Reads `/data/options.json` (path overridable for tests) and maps its options onto the same settings the environment variables set today: `public_url` -> `NOVELHOG_PUBLIC_URL`, `vapid_subject` -> `NOVELHOG_VAPID_SUBJECT`, `allowed_hosts` (a list) -> `NOVELHOG_ALLOWED_HOSTS` (comma-joined), `flaresolverr_url` -> `NOVELHOG_FLARESOLVERR_URL` (only when non-empty; otherwise the default stays and the worker reports an infrastructure error only for Cloudflare-protected sites, as today).
- Sets `NOVELHOG_DATA_DIR=/data` (the image already uses `/data/db` and `/data/static`) and keeps SQLite as the database.
- Runs the migration, then starts the web role and the worker role as child processes, reusing the supervision pattern `novelhog web` already has for Caddy and the API (`_web`, `_terminate`).
- On SIGTERM/SIGINT forwards the signal to both children and waits (the worker finishes its chapter; the Supervisor's stop timeout applies).
- If either child exits, terminates the other and exits non-zero, so the Supervisor restarts the add-on.
- A missing options file is treated as "all defaults" (so `docker run ... addon` works outside Home Assistant); a malformed one exits non-zero with a message that names the file.

## 2. The add-on definition

`homeassistant/novelhog/`:

- `config.yaml`: `name`, `slug: novelhog`, `description`, `version` (equals the project version), `arch: [amd64, aarch64]`, `image: ghcr.io/t3hk0d3/novelhog-addon`, `init: false`, `startup: application`, `boot: auto`, `ports: {"8080/tcp": 8080}` with a description, `webui: "http://[HOST]:[PORT:8080]"`, `options` (defaults) and `schema` (`public_url: url?`, `vapid_subject: str?`, `allowed_hosts: [str]?`, `flaresolverr_url: url?`), `map: [data]` if the build-time check shows it is needed.
- `Dockerfile` (above), `icon.png` and `logo.png` (from `docs/assets`), `DOCS.md` (what Home Assistant shows on the add-on's Documentation tab: options, the no-login warning, push needs HTTPS, FlareSolverr), `CHANGELOG.md`.
- `repository.yaml` at the repository root: `name`, `url`, `maintainer`, so users add `https://github.com/t3hk0d3/novelhog` as an add-on repository.

## 3. Publishing and versioning

- A `publish-addon` job in `ci.yml`, `needs: publish`, same trigger and permissions. It builds `homeassistant/novelhog/Dockerfile` for `linux/amd64,linux/arm64` with `BASE_TAG` set to the most specific tag the `publish` job just pushed (the version on a `v*` tag, `sha-<short>` on `main`), and pushes `ghcr.io/t3hk0d3/novelhog-addon` with the same tag set (`latest` + `sha-<short>`, or `1.2.3` + `1.2`).
- Release rule: the add-on `version` in `config.yaml` equals the project version in `pyproject.toml`, and on a `v*` tag equals the tag. A release is "bump both, tag". A CI check (a small test plus a tag step) enforces it, so the Supervisor never asks for an image tag that does not exist.
- The package `novelhog-addon` is made public once (as for `novelhog`).

## 4. Tests and checks

BDD skeleton (the contract; tests come one per effect):

```
novelhog.addon_options (module)
    load
        With every option set
            - returns the public URL, VAPID subject, allowed hosts and FlareSolverr URL
        With an empty or absent file
            - returns defaults (nothing set)
        With a malformed file
            - raises an error that names the file
    to_environment
        With allowed hosts ["a", "b"]
            - NOVELHOG_ALLOWED_HOSTS is "a,b"
        With an empty FlareSolverr URL
            - NOVELHOG_FLARESOLVERR_URL is not set
        Always
            - NOVELHOG_DATA_DIR is /data

novelhog addon (cli)
    starting
        - the migration runs before any child starts
        - the web role and the worker role are started as children
    On SIGTERM
        - both children receive it and the command waits for them
    When a child exits
        - the other child is terminated
        - the command exits non-zero
    With a malformed options file
        - exits non-zero without starting any child

homeassistant/novelhog/config.yaml (contract)
    - version equals the pyproject.toml version
    - image is ghcr.io/t3hk0d3/novelhog-addon
    - arch is amd64 and aarch64
    - port 8080/tcp is exposed and the webui points at it
    - every option in `options` has a `schema` entry and vice versa

addon image (container smoke, gated like the others)
    - built FROM the main image, `addon` starts and serves GET /api/v1/settings on 8080
    - the data directory under /data is written (root-owned /data mounted)
```

Parsing and environment mapping are unit tests; the supervision cases follow the existing `novelhog web` tests' approach to child processes. `config.yaml` is parsed in the test (PyYAML in the `dev` group). CI also runs the official add-on linter (`frenck/action-addon-linter`) on `homeassistant/novelhog`. Code style follows `STYLEGUIDE.md` (module docstrings, full typing, `test_<unit>__<behaviour>`, scenario classes).

## 5. Docs

- `docs/home-assistant.md` (nav: after Install): add the repository, install, options table, first start, the no-login warning, HTTPS/push notes (a reverse proxy or Home Assistant's own Nabu Casa URL is the user's choice), FlareSolverr via an external add-on, backups (the add-on's `/data` is included in Home Assistant backups).
- A short Home Assistant mention and badge-style link in the README ("Add to Home Assistant" button URL `https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Ft3hk0d3%2Fnovelhog`).
- `STYLEGUIDE.md` layout line gains `homeassistant/`.

## Out of scope / future work

- **Ingress** (own spec): base-path support in the SPA, Caddy, API origin/host checks, SSE and the service worker; `ingress: true` in `config.yaml`.
- Bundling FlareSolverr in the add-on.
- Home Assistant integration entities, notifications to HA, backups hooks.
