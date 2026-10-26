# README, docs site, published image: design

Date: 2026-10-26

## Goal

Make novelhog easy to discover and to deploy for people who do not want to clone the repository:

- A short, appealing `README.md` that sells the project and gets a reader to a running instance in three steps.
- The technical documentation moves out of the README into `docs/` and is published as a GitHub Pages site.
- CI publishes a multi-arch Docker image to GitHub Container Registry (GHCR).
- `compose.yaml` becomes the end-user file that pulls the published image; today's build-from-source file becomes `compose.dev.yaml`.

Not in scope: changing application behavior, versioned docs, a custom domain.

## Existing conventions followed

`STYLEGUIDE.md` governs naming, layout and tests (file layout line at `STYLEGUIDE.md:54` is updated). Container tests keep the `test_<subject>__<scenario>` style used in `tests/container/test_smoke.py`.

## 1. Compose split

- `compose.dev.yaml`: `git mv` of the current `compose.yaml`, unchanged apart from a header comment saying it is the development file (builds `novelhog:local`, has `develop.watch`).
- `compose.yaml` (new, end users): same services (`novelhog-migrate`, `novelhog-web`, `novelhog-worker`, `flaresolverr`), same networks, volumes, `depends_on`, restart policies and `stop_grace_period`; but `image: ghcr.io/t3hk0d3/novelhog:${NOVELHOG_VERSION:-latest}` and no `build:` or `develop:`. Users download this one file.
- `compose.test.yaml` becomes an overlay of `compose.dev.yaml` (comment updated).
- Repointed to `compose.dev.yaml`: `tests/container/test_smoke.py` (`FILES`, and the `config` call that uses `FILES[:2]`) and the `container` job in `.github/workflows/ci.yml`.
- A fresh-clone developer runs `docker compose -f compose.dev.yaml up --build --watch`. The docs say so, and `development.md` suggests `COMPOSE_FILE=compose.dev.yaml` in the shell.

## 2. Image publishing

- A `publish` job is added to `.github/workflows/ci.yml`, with `needs` on every other job, so nothing failing is published.
- Runs only on pushes to `main` and on `v*` tags; never on pull requests.
- Steps: checkout, QEMU, Buildx, login to `ghcr.io` with `GITHUB_TOKEN`, `docker/metadata-action`, `docker/build-push-action` with `platforms: linux/amd64,linux/arm64`, `context: .`, `file: docker/Dockerfile`, cache `type=gha`.
- Job permissions: `contents: read`, `packages: write` (job level only).
- Tags: `main` push gives `latest` and `sha-<short>`; tag `v1.2.3` gives `1.2.3` and `1.2`.
- Image: `ghcr.io/t3hk0d3/novelhog`. The package is made public once in the GitHub UI (documented in `development.md`, a manual step).
- Action versions follow those already used in `ci.yml` (major-version pins).

## 3. README.md

Short and promotional; no tables of settings, no proxy snippets.

- Logo (`docs/assets/logo.png`), name, one-line pitch.
- Hero screenshots: desktop light/dark pair and a phone reader shot.
- Feature list: import by table-of-contents URL; follow a novel and auto-download new chapters; read in the app with reader settings; export EPUBs split into books; installable PWA with push notifications; handles Cloudflare-protected sites via FlareSolverr; SQLite or Postgres; multi-arch image (amd64, arm64).
- Quick start in three steps: download `compose.yaml`; set `NOVELHOG_PUBLIC_URL` and `NOVELHOG_VAPID_SUBJECT`; `docker compose up -d`.
- Links: docs site, license, development guide.
- A note that there is no login (so it should sit behind a trusted network or proxy), linking to the docs.

## 4. Docs site

- Generator: MkDocs with the Material theme. `mkdocs-material` goes into a new `docs` dependency group in `pyproject.toml`, locked in `uv.lock`; the docs build uses `uv sync --locked --only-group docs`.
- `mkdocs.yml` at the repo root; `site_url` is `https://t3hk0d3.github.io/novelhog/`; `exclude_docs` lists `superpowers/`; light/dark palette toggle; logo and favicon from `docs/assets`.
- Pages (content is the old README text, moved over with minimal edits):
  - `index.md`: overview, features, screenshots.
  - `reader.md`: the built-in reader (contributed with the reader itself; wired into the nav and given the generated screenshots).
  - `install.md`: Compose quick start, image tags, `NOVELHOG_VERSION`, making the package public.
  - `configuration.md`: env vars, the allowed-hosts check, non-root user.
  - `reverse-proxy.md`: Caddy and nginx.
  - `storage.md`: volumes, backups, upgrades, vacuum.
  - `development.md`: `uv sync`, `scripts/check.sh`, `compose.dev.yaml` and `--watch`, container tests, regenerating screenshots.
- `.github/workflows/docs.yml`: on pull requests touching `docs/**`, `mkdocs.yml` or `pyproject.toml`, run `mkdocs build --strict`; on pushes to `main`, build and deploy with `actions/upload-pages-artifact` and `actions/deploy-pages` (permissions `pages: write`, `id-token: write`). Pages source is set to "GitHub Actions" once in the repository settings.

## 5. Logo

The mascot (a hedgehog in glasses reading a book) was generated by the user; candidates live in `docs/logo/`, and `book-2.jpeg` is the chosen one. `docs/logo/` stays as source material and is excluded from the site.

- `docs/assets/logo.svg`: `book-2.jpeg` traced with the `vtracer` CLI (colour mode, stacked, spline), with the full-canvas background path removed so it is transparent. The white sticker border stays.
- `docs/assets/logo.png`: rendered from the SVG with `rsvg-convert`, for README and the social preview.
- `docs/assets/favicon.png` and `web/public/icons/{192,512}.png`: rendered from a simplified crop (face and glasses) of the SVG.
- The trace and render commands are documented in `development.md`. The `vtracer` Python wheel segfaults on Python 3.14, so the Rust CLI is used; it is not a project dependency.

## 6. Screenshots

- `web/e2e/screenshots.ts` (not part of `npm run e2e` or CI), run with `npm run screenshots` against the fake backend from `tests/fixtures/fake_backend.py`.
- Demo library: five public-domain novels from Project Gutenberg with their real covers, every chapter title and the real text of the first eight chapters (a screenful and a half each), with varied download progress. It lives in `tests/fixtures/demo_backend.py` and `tests/fixtures/demo/`, built on a public `serve_backend` extracted from `fake_backend.py`, so the e2e fixtures stay unchanged. Sources are listed in `tests/fixtures/demo/SOURCES.md`, next to the data, so the docs site does not publish them.
- Shots, each on desktop and phone: library, novel page with an expanded exports panel, reader (light and dark), and, in light only, reader settings, two theme and font combinations, and a not-downloaded chapter. Twenty PNGs, shrunk to 256 colours (1.3 MB in all).
- Output in `docs/assets/screenshots/`, committed as PNG.

## 7. Behavior contract (BDD skeleton)

Most of the work is config and prose, where a skeleton does not apply. The compose files are the one testable contract:

```
tests/container/test_compose_files
    compose.yaml
        Parsed with docker compose config
            - defines novelhog-migrate, novelhog-web, novelhog-worker and flaresolverr
            - every novelhog service uses the ghcr.io/t3hk0d3/novelhog image
            - no service has a build section
            - no service has a develop section
        With NOVELHOG_VERSION=1.2.3
            - the novelhog services use the 1.2.3 tag
        Without NOVELHOG_VERSION
            - the novelhog services use the latest tag
        Network layout
            - worker and flaresolverr share a network that web does not join
    compose.dev.yaml
        Parsed with docker compose config
            - every novelhog service has a build section for docker/Dockerfile
            - web and worker have develop.watch rules
    compose.dev.yaml with compose.test.yaml
        Existing smoke tests
            - keep passing unchanged against the dev files
```

The first group does not need to start containers, so it runs in the normal test suite, not only under `NOVELHOG_CONTAINER_TESTS=1`.

## 8. Verification

- `docker compose -f compose.yaml config` and `-f compose.dev.yaml config` both succeed.
- `uv run scripts/check.sh` passes.
- `uv run mkdocs build --strict` passes.
- The `publish` job is verified on the first push to `main` (cannot be run locally); a local `docker buildx build --platform linux/arm64` is optional.

## Out of scope / future work

- Faster arm64 builds (a native arm64 runner, or running the Node stage on the build platform).
- Versioned docs, release automation, a custom domain.
