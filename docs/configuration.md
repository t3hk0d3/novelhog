# Configuration

Settings are environment variables prefixed `NOVELHOG_`. The common ones:

| Variable | Default | |
|---|---|---|
| `NOVELHOG_HTTP_PORT` | `8080` | Caddy's listen port and the published port |
| `NOVELHOG_PUBLIC_URL` | unset | External HTTPS URL; its host passes the API's host check |
| `NOVELHOG_VAPID_SUBJECT` | unset | `mailto:` contact for Web Push |
| `NOVELHOG_ALLOWED_HOSTS` | unset | Extra comma-separated host names the API accepts |
| `NOVELHOG_DATABASE_URL` | `sqlite:////data/db/app.db` | Use `postgresql+psycopg://...` for Postgres |
| `NOVELHOG_VERSION` | `latest` | Image tag that `compose.yaml` runs (a Compose setting, not read by the app) |

## The host check

The API answers only requests addressed to an IP address, `localhost`, the host of `NOVELHOG_PUBLIC_URL` or a
name in `NOVELHOG_ALLOWED_HOSTS`. To open novelhog on your LAN by a host name, for example
`http://homeserver:8080`, add that name: `NOVELHOG_ALLOWED_HOSTS=homeserver`. Otherwise the page shows
"This host name is not allowed".

## Web Push

Push notifications need an HTTPS origin (`NOVELHOG_PUBLIC_URL`) and a contact (`NOVELHOG_VAPID_SUBJECT`).
Service workers, and so installing novelhog as an app, need HTTPS too (`localhost` excepted).
See [Reverse proxy](reverse-proxy.md).

## Removed settings

`NOVELHOG_BATCH_SIZE` is removed. If it is still set, it is ignored and the worker logs `config_ignored` at
startup.

## Sites that push back

A site that throttles, blocks or stops answering is paused for a while (up to an hour, growing with each failure)
instead of failing chapters; its work waits and resumes on its own. `GET /api/v1/hosts` shows a paused host's
`reach_after`.
