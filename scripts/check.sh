#!/usr/bin/env bash
# Run before considering any change done. Always runs core+collimation+guide
# regardless of which app the change appears to touch (see CONTRIBUTING.md).
#
# Pass --release before pushing a release: additionally runs tests/contracts,
# tests/integration, and the below-UI tests/acceptance regression suite.
set -euo pipefail
cd "$(dirname "$0")/.."

if [ -d ".venv/Scripts" ]; then
  BIN=".venv/Scripts"
elif [ -d ".venv/bin" ]; then
  BIN=".venv/bin"
else
  echo "No .venv found — run: pip install -e \".[dev]\"" >&2
  exit 1
fi

RELEASE=0
for arg in "$@"; do
  if [ "$arg" = "--release" ]; then
    RELEASE=1
  fi
done

echo "== ruff =="
"$BIN/ruff" check .

echo "== mypy =="
"$BIN/mypy" .

echo "== import-linter =="
"$BIN/lint-imports"

if [ "$RELEASE" = "1" ]; then
  echo "== pytest: full release gate (core, collimation, guide, contracts, integration, acceptance) =="
  "$BIN/pytest" tests/core tests/collimation tests/guide tests/contracts tests/integration tests/acceptance
else
  echo "== pytest: core, collimation, guide =="
  "$BIN/pytest" tests/core tests/collimation tests/guide
fi

echo "All checks passed."
