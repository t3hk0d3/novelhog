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
for _ in $(seq 1 60); do
    if docker exec "${container}" pg_isready -U postgres >/dev/null 2>&1; then
        break
    fi
    sleep 1
done
# pg_isready can answer during the image's bootstrap restart; require a real query.
for _ in $(seq 1 30); do
    if docker exec "${container}" psql -U postgres -tAc "select 1" >/dev/null 2>&1; then
        break
    fi
    sleep 1
done

export NOVELHOG_TEST_DATABASE_URL="postgresql+psycopg://postgres:${password}@127.0.0.1:${port}/postgres"
uv run pytest tests/integration tests/api
