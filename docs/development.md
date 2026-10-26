# Development

## Checks

```sh
uv sync
uv run scripts/check.sh          # all gates
scripts/test_pg.sh               # database tests on PostgreSQL 16
NOVELHOG_CONTAINER_TESTS=1 uv run pytest tests/container   # builds the image; needs Docker
```

`STYLEGUIDE.md` in the repository root sets the conventions (naming, layout, tests, commits).

## Running from source

`compose.dev.yaml` builds the image from the working tree instead of pulling the published one. Point
Compose at it for the shell session, or pass `-f` each time:

```sh
export COMPOSE_FILE=compose.dev.yaml
docker compose up --build --watch
```

`--watch` rebuilds and restarts a container when its sources change: a change under `web/` or to
`docker/Caddyfile` only touches `novelhog-web`; a change under `src/`, to `pyproject.toml`, `uv.lock` or
`docker/Dockerfile` touches both `novelhog-web` and `novelhog-worker`. Migrations are not re-run: after adding
one, run `docker compose run --rm novelhog-migrate`.

## Documentation

This site is built with [MkDocs Material](https://squidfunk.github.io/mkdocs-material/) from `docs/` and
`mkdocs.yml`:

```sh
uv sync --only-group docs
uv run mkdocs serve              # live preview at http://127.0.0.1:8000
uv run mkdocs build --strict     # what CI runs
```

Pushes to `main` publish the site to GitHub Pages. Once, in the repository settings, set Pages → Source to
"GitHub Actions". `docs/superpowers/` holds internal design notes and is not published.

## Releases and the image

CI publishes `ghcr.io/t3hk0d3/novelhog` for `linux/amd64` and `linux/arm64` after all checks pass: `latest` and
`sha-<short>` on pushes to `main`, and `1.2.3` and `1.2` for a `v1.2.3` tag. After the first publish, make the
package public once (GitHub → Packages → novelhog → Package settings → Change visibility), or `docker compose
pull` will ask for a login.

## Logo

`docs/logo/` holds the generated candidates; `book-2.jpeg` is the logo. `docs/assets/logo.svg` is that image
traced with the [vtracer](https://github.com/visioncortex/vtracer) CLI (`cargo install vtracer`), with the
full-canvas background path removed:

```sh
vtracer -i book-2.png -o logo.svg --colormode color --hierarchical stacked --mode spline \
  --filter_speckle 8 --color_precision 6 --gradient_step 24 --corner_threshold 60 \
  --segment_length 4 --splice_threshold 45 --path_precision 2
```

`logo.png` and the app icons (`web/public/icons/`) are rendered from the SVG with `rsvg-convert` and
ImageMagick.
