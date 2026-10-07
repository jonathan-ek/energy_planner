#!/usr/bin/env bash
# Code quality checks: ruff lint, ruff format, ty and the tests.
# All must pass before committing (the pre-commit hook runs this script).
#
# Usage: scripts/check.sh

set -u
cd "$(dirname "$0")/.."

PATHS=(custom_components tests)
failed=()

run() {
    local name=$1
    shift
    echo "==> $name"
    if "$@"; then
        echo "    ok"
    else
        failed+=("$name")
    fi
}

run "ruff check" uv run ruff check "${PATHS[@]}"
run "ruff format" uv run ruff format --diff "${PATHS[@]}"
run "ty" uv run ty check "${PATHS[@]}"
run "pytest" uv run pytest tests -q -p no:logging

if [ ${#failed[@]} -gt 0 ]; then
    echo
    echo "Failed: ${failed[*]}"
    exit 1
fi
echo
echo "All checks passed"
