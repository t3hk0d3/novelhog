# Install

novelhog runs from a published Docker image, `ghcr.io/t3hk0d3/novelhog`, for `linux/amd64` and
`linux/arm64`. You need Docker with the Compose plugin; nothing else.

## Quick start

```sh
mkdir novelhog && cd novelhog
curl -fsSLO https://raw.githubusercontent.com/t3hk0d3/novelhog/main/compose.yaml
export NOVELHOG_PUBLIC_URL=https://novelhog.example.com   # needed for Web Push and the host check
export NOVELHOG_VAPID_SUBJECT=mailto:you@example.com      # needed for Web Push
docker compose up -d
```

Open `http://localhost:8080`. The environment variables are optional for a first try on your own machine;
see [Configuration](configuration.md) for what they do. To keep them across shells, put them in a `.env`
file next to `compose.yaml`.

## Image tags

| Tag | Meaning |
|---|---|
| `latest` | The newest build of the `main` branch |
| `1.2.3`, `1.2` | A release and its minor line |
| `sha-<short>` | A specific commit of `main` |

`compose.yaml` uses `latest`. To pin a release, set `NOVELHOG_VERSION`, for example
`NOVELHOG_VERSION=1.2.3 docker compose up -d`.

## What runs

`compose.yaml` runs one image in three roles plus a FlareSolverr:

| Service | Command | Notes |
|---|---|---|
| `novelhog-migrate` | `novelhog db migrate` | One-shot; `web` and `worker` wait for it to exit 0, so every `up` migrates exactly once |
| `novelhog-web` | `novelhog web` | Caddy (the only published port, `8080` by default) and the API on `127.0.0.1:8000`, under `tini` |
| `novelhog-worker` | `novelhog worker` | Scraper, exports, notifications; `stop_grace_period: 2m` lets a chapter finish. Its work runs as tasks, readable at `GET /api/v1/tasks` |
| `flaresolverr` | | On a private network shared only with the worker; `NOVELHOG_FLARESOLVERR_URL` points at it |

Every worker is paired 1:1 with its own FlareSolverr. Do not use `--scale` on the worker; add another
worker/FlareSolverr pair in an override file instead.

The image runs as a non-root user (uid 10001).

!!! warning "There is no login"
    `compose.yaml` publishes the port on all interfaces. Keep novelhog on a trusted network, or put a
    [reverse proxy](reverse-proxy.md) in front of it. When the proxy runs on the same host, change the
    mapping to `127.0.0.1:8080:8080` in an override file so only the proxy can reach it.

## Upgrading

```sh
docker compose stop novelhog-worker
docker compose pull
docker compose up -d
```

See [Storage and upgrades](storage.md) for why the worker is stopped first, and for backups.
