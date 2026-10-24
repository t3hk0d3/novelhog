#!/usr/bin/env bash
# Runs every quality gate in order; exits non-zero at the first failing gate.
set -euo pipefail

cd "$(dirname "$0")/.."

gate() {
    local name="$1"
    shift
    echo "==> ${name}"
    if ! "$@"; then
        echo "FAILED gate: ${name}" >&2
        exit 1
    fi
}

gate "ruff format --check" uv run ruff format --check
gate "ruff check" uv run ruff check
gate "mypy --strict" uv run mypy --strict src tests tools
gate "lint-imports" uv run lint-imports
gate "pytest" uv run pytest
# CI's python jobs have no Node toolchain and run the web check in its own job.
if [[ "${NOVELHOG_SKIP_WEB:-}" != "1" ]]; then
    gate "web check" npm --prefix web run check
fi
echo "All gates passed."
