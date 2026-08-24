#!/usr/bin/env bash
# Run before considering any change done. Always runs core+collimation+guide
# regardless of which app the change appears to touch (see CONTRIBUTING.md).
set -euo pipefail
cd "$(dirname "$0")/.."

echo "== ruff =="
ruff check .

echo "== mypy =="
mypy .

echo "== import-linter =="
lint-imports

echo "== pytest: core, collimation, guide =="
pytest tests/core tests/collimation tests/guide

echo "All checks passed."
