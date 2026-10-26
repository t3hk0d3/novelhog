# Storage and upgrades

## Volumes

| Path in the containers | Compose volume | Access |
|---|---|---|
| `/data/db` | `novelhog-db` | read-write for all three services (SQLite creates `app.db-wal` and `app.db-shm` next to the database); unused with Postgres |
| `/data/static` | `novelhog-static` | read-write for `novelhog-worker`, read-only for `novelhog-web`; covers and exports |
| `/var/lib/novelhog/scraper` | none | worker-local scraper cache; safe to delete at any time |

Bind-mounted host directories (instead of the named volumes) must be writable by uid 10001, for example
`chown 10001 ./data`.

## Backups

- SQLite: while running, write a consistent copy, copy it out of the container and delete it:

  ```sh
  docker compose exec novelhog-web python -c "import sqlite3; s = sqlite3.connect('/data/db/app.db'); s.execute(\"VACUUM INTO '/data/db/backup.db'\")"
  docker compose cp novelhog-web:/data/db/backup.db .
  docker compose exec novelhog-web rm /data/db/backup.db
  ```

  Never copy `app.db` while the WAL is live.
- Postgres: `pg_dump` your database.
- Static files: copy the `novelhog-static` volume (`/data/static`).

Restoring an older database restores its VAPID keys and subscriptions; browsers whose subscription no longer
matches re-subscribe.

## Upgrades

Stop the workers before upgrading, so none runs while the database migrates (also any extra worker from an
override file):

```sh
docker compose stop novelhog-worker
docker compose pull
docker compose up -d
```

`novelhog-migrate` runs `novelhog db migrate` first. To run it by hand: `docker compose run --rm novelhog-migrate`.

## Vacuum

`VACUUM` is a manual operation if the database file has grown after large deletions. On SQLite, stop the worker
while it runs:

```sh
docker compose stop novelhog-worker
docker compose exec novelhog-web python -c "import sqlite3; sqlite3.connect('/data/db/app.db').execute('VACUUM')"
docker compose start novelhog-worker
```

On Postgres, autovacuum reclaims space; run `VACUUM FULL` with `psql` only if you need the disk space back.
