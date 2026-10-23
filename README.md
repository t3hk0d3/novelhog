# novelhog

Self-hosted web-novel downloader: a web UI and a worker around
[web-novel-scraper](https://pypi.org/project/web-novel-scraper/). Import a novel by its table-of-contents
URL, follow it, download chapters and export EPUBs.

## Deploy with Compose

```sh
git clone <this repository> novelhog && cd novelhog
export NOVELHOG_PUBLIC_URL=https://novelhog.example.com   # needed for Web Push and the host check
export NOVELHOG_VAPID_SUBJECT=mailto:you@example.com      # needed for Web Push
docker compose up -d --build
```

`compose.yaml` runs one image in three roles plus a FlareSolverr:

| Service | Command | Notes |
|---|---|---|
| `novelhog-migrate` | `novelhog db migrate` | One-shot; `web` and `worker` wait for it to exit 0, so every `up` migrates exactly once |
| `novelhog-web` | `novelhog web` | Caddy (the only published port, `8080` by default) and the API on `127.0.0.1:8000`, under `tini` |
| `novelhog-worker` | `novelhog worker` | Scraper, exports, notifications; `stop_grace_period: 2m` lets a chapter finish |
| `flaresolverr` | | On a private network shared only with the worker; `NOVELHOG_FLARESOLVERR_URL` points at it |

Every worker is paired 1:1 with its own FlareSolverr. Do not use `--scale` on the worker; add another
worker/FlareSolverr pair in an override file instead.

Settings are environment variables prefixed `NOVELHOG_`. The common ones:

| Variable | Default | |
|---|---|---|
| `NOVELHOG_HTTP_PORT` | `8080` | Caddy's listen port and the published port |
| `NOVELHOG_PUBLIC_URL` | unset | External HTTPS URL; its host passes the API's host check |
| `NOVELHOG_VAPID_SUBJECT` | unset | `mailto:` contact for Web Push |
| `NOVELHOG_ALLOWED_HOSTS` | unset | Extra comma-separated host names the API accepts |
| `NOVELHOG_DATABASE_URL` | `sqlite:////data/db/app.db` | Use `postgresql+psycopg://...` for Postgres |

The image runs as a non-root user (uid 10001).

### Reverse proxy

Terminate HTTPS in front of novelhog (service workers and push require it). The proxy must pass the original
`Host` header (including a non-default port) and must not buffer `/api/v1/events`.

Caddy:

```caddyfile
novelhog.example.com {
	reverse_proxy 127.0.0.1:8080 {
		flush_interval -1
	}
}
```

nginx:

```nginx
server {
    listen 443 ssl;
    server_name novelhog.example.com;
    # ssl_certificate / ssl_certificate_key ...

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $http_host;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location /api/v1/events {
        proxy_pass http://127.0.0.1:8080;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_set_header Host $http_host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 1h;
    }
}
```

`$http_host` (not `$host`) keeps the port, so the API's origin check passes when the site is served on a
non-default port.

## Volumes

| Path in the containers | Compose volume | Access |
|---|---|---|
| `/data/db` | `novelhog-db` | read-write for all three services (SQLite creates `app.db-wal` and `app.db-shm` next to the database); unused with Postgres |
| `/data/static` | `novelhog-static` | read-write for `novelhog-worker`, read-only for `novelhog-web`; covers and exports |
| `/var/lib/novelhog/scraper` | none | worker-local scraper cache; safe to delete at any time |

Bind-mounted host directories (instead of the named volumes) must be writable by uid 10001, for example
`chown 10001 ./data`.

There is no login. `compose.yaml` publishes the port on all interfaces; when the reverse proxy runs on the same
host, change the mapping to `127.0.0.1:8080:8080` in an override file so only the proxy can reach it.

## Backups

- SQLite: while running, `docker compose exec novelhog-web python -c "import sqlite3; s = sqlite3.connect('/data/db/app.db'); s.execute(\"VACUUM INTO '/data/db/backup.db'\")"`, then copy `backup.db` out and delete it.
  Never copy `app.db` while the WAL is live.
- Postgres: `pg_dump` your database.
- Static files: copy the `novelhog-static` volume (`/data/static`).

Restoring an older database restores its VAPID keys and subscriptions; browsers whose subscription no longer
matches re-subscribe.

## Upgrades

```sh
git pull
docker compose up -d --build
```

`novelhog-migrate` runs `novelhog db migrate` first. To run it by hand: `docker compose run --rm novelhog-migrate`.
`VACUUM` is a manual operation if the database file has grown after large deletions.

## Development

```sh
uv sync
uv run scripts/check.sh          # all gates
scripts/test_pg.sh               # database tests on PostgreSQL 16
NOVELHOG_CONTAINER_TESTS=1 uv run pytest tests/container   # builds the image; needs Docker
```
