#!/usr/bin/env bash
# Fails when src/api/schema.d.ts differs from what the Python API's OpenAPI document generates.
set -euo pipefail

cd "$(dirname "$0")/.."

generated="$(mktemp --suffix=.d.ts)"
trap 'rm -f "${generated}"' EXIT

uv run --project .. python -m novelhog.api.export_openapi | npx openapi-typescript -o "${generated}" >/dev/null
if ! diff -q "${generated}" src/api/schema.d.ts >/dev/null; then
    echo "src/api/schema.d.ts is stale: run 'npm --prefix web run gen:api' and commit it" >&2
    exit 1
fi
