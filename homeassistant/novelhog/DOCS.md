# novelhog

novelhog downloads the web novels you follow, keeps checking for new chapters, lets you read them in a
reader made for phones and exports them as EPUBs. Full documentation:
<https://t3hk0d3.github.io/novelhog/>.

## Getting started

1. Start the add-on and press **Open Web UI**, or open `http://<your Home Assistant>:8080`.
2. Press **Add novel** and paste a novel's table-of-contents link.

**There is no login.** Anyone who can reach port 8080 can use novelhog. Keep it on a trusted network, or put an
authenticating reverse proxy in front of it. You can switch the port off in the add-on's **Network** section.

## Options

Every option is optional.

| Option | What it does |
|---|---|
| `public_url` | The external HTTPS address you open novelhog at, for example `https://novelhog.example.com`. Needed for push notifications and for installing novelhog as an app. |
| `vapid_subject` | A contact for push notifications, such as `mailto:you@example.com`. |
| `allowed_hosts` | Extra host names the web UI may be opened by. novelhog answers only requests addressed to an IP address, `localhost`, the host of `public_url` or a name listed here. To open it as `http://homeassistant.local:8080`, add `homeassistant.local`; otherwise the page says "This host name is not allowed". |
| `flaresolverr_url` | The address of a [FlareSolverr](https://github.com/FlareSolverr/FlareSolverr), for example `http://<host>:8191`, used for Cloudflare-protected sites. Run it as another add-on or on another machine; leave this empty if you do not need it. |
| `database_url` | A database connection string. Leave it empty to use SQLite, kept in the add-on's own storage. For PostgreSQL use `postgresql+psycopg://user:password@host:5432/novelhog`. The password is stored in the option, so it is hidden in the UI. |

## Data and backups

The database (with SQLite), covers and exported EPUBs live in the add-on's own storage, which Home Assistant
includes in its backups. An external database (`database_url`) is **not** included: back it up yourself, for
example with `pg_dump`.

## Updating

Update the add-on from the add-on store. It migrates its database when it starts.

## Support

Report problems at <https://github.com/t3hk0d3/novelhog/issues>.
