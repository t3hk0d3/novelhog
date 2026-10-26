#!/usr/bin/env bash
# Runs the database-backed tests against a throwaway Postgres 16 container.
set -euo pipefail

cd "$(dirname "$0")/.."

password="novelhog-test"
container="novelhog-test-pg-$$"
trap 'docker rm -f "${container}" >/dev/null 2>&1 || true' EXIT

docker run -d --name "${container}" -e POSTGRES_PASSWORD="${password}" \
    -p 127.0.0.1::5432 postgres:16 >/dev/null

port="$(docker port "${container}" 5432/tcp | head -n 1 | sed 's/.*://')"

echo "==> waiting for postgres on port ${port}"
# The image's entrypoint runs a temporary server on the Unix socket only, then restarts;
# a TCP query (-h 127.0.0.1) succeeds only once the real server is up.
ready=0
for _ in $(seq 1 60); do
    if docker exec "${container}" psql -h 127.0.0.1 -U postgres -tAc "select 1" >/dev/null 2>&1; then
        ready=1
        break
    fi
    sleep 1
done
if [ "${ready}" -ne 1 ]; then
    echo "postgres did not accept TCP connections within 60 s" >&2
    exit 1
fi

export NOVELHOG_TEST_DATABASE_URL="postgresql+psycopg://postgres:${password}@127.0.0.1:${port}/postgres"
uv run pytest tests/integration tests/api
