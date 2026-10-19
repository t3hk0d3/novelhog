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
gate "mypy --strict" uv run mypy --strict src tests
gate "lint-imports" uv run lint-imports
gate "pytest" uv run pytest
echo "All gates passed."
