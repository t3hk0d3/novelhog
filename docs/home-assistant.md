# Home Assistant

novelhog is available as a Home Assistant add-on. It runs the same image as the Docker install, for `amd64` and
`aarch64`, with its options in the add-on's configuration form.

## Install

[![Add the novelhog add-on repository to Home Assistant](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Ft3hk0d3%2Fnovelhog)

Or add it by hand: in Home Assistant open **Settings → Add-ons → Add-on store**, open the menu (⋮) and choose
**Repositories**, add `https://github.com/t3hk0d3/novelhog`, then install **novelhog** from the store and start it.

Press **Open Web UI**, or open `http://<your Home Assistant>:8080`, and add your first novel.

!!! warning "There is no login"
    Anyone who can reach port 8080 can use novelhog. Keep it on a trusted network, or put an authenticating
    [reverse proxy](reverse-proxy.md) in front of it. You can switch the port off in the add-on's **Network**
    section.

## Options

Every option is optional.

| Option | What it does |
|---|---|
| `public_url` | The external HTTPS address you open novelhog at, for example `https://novelhog.example.com`. Needed for push notifications and for installing novelhog as an app. |
| `vapid_subject` | A contact for push notifications, such as `mailto:you@example.com`. |
| `allowed_hosts` | Extra host names the web UI may be opened by. To open it as `http://homeassistant.local:8080`, add `homeassistant.local`; otherwise the page says "This host name is not allowed". |
| `flaresolverr_url` | The address of a FlareSolverr, for example `http://<host>:8191`, for Cloudflare-protected sites. |
| `database_url` | A database connection string. Empty (the default) means SQLite, kept in the add-on's own storage. For PostgreSQL use `postgresql+psycopg://user:password@host:5432/novelhog`. Hidden in the UI because it holds a password. |

These are the same settings as the `NOVELHOG_*` variables of the Docker install; see
[Configuration](configuration.md) for what the host check and push notifications need.

### FlareSolverr

A Home Assistant add-on is a single container, so FlareSolverr is not bundled. Run it as a separate add-on, or
on another machine, and put its address in `flaresolverr_url`. Sites without Cloudflare need nothing.

## Backups

The database (with SQLite), covers and exported EPUBs live in the add-on's own storage, which Home Assistant
backups include. An external database (`database_url`) is **not** included: back it up yourself, for example with
`pg_dump`.

## Updating

Update the add-on from the add-on store. It migrates its database when it starts.
