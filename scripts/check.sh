#!/usr/bin/env bash
# Developer quality gate (see CONTRIBUTING.md, "Test pyramid and the
# risk-based minimum gate"). Every mode runs ruff, mypy and import-linter
# first, then:
#
#   (default)       the tests covering what you changed vs origin/main, chosen
#                   by scripts/changed_tests.py (all fast tiers if the change
#                   can't be mapped, e.g. conftest/pyproject/shared fakes)
#   --all-fast      every fast tier: -m "unit or component or contract"
#   --integration   the slow tiers: -m "integration or acceptance"
#   --release       the full release gate with coverage (fail-under 80) --
#                   run before pushing a release; CI's coverage job mirrors it
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

MODE=changed
for arg in "$@"; do
  case "$arg" in
    --all-fast) NEW=all-fast ;;
    --integration) NEW=integration ;;
    --release) NEW=release ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
  if [ "$MODE" != changed ] && [ "$MODE" != "$NEW" ]; then
    echo "conflicting modes: --$MODE and --$NEW -- pick one" >&2
    exit 2
  fi
  MODE=$NEW
done

echo "== ruff =="
"$BIN/ruff" check .

echo "== mypy =="
"$BIN/mypy" .

echo "== import-linter =="
"$BIN/lint-imports"

FAST="unit or component or contract"

case "$MODE" in
  release)
    echo "== pytest: full release gate + coverage (core, collimation, guide, contracts, integration, regressions, acceptance) =="
    "$BIN/pytest" tests/core tests/collimation tests/guide tests/contracts tests/integration tests/regressions tests/acceptance --cov --cov-report=term-missing
    ;;
  integration)
    echo "== pytest: integration + acceptance tiers =="
    "$BIN/pytest" -m "integration or acceptance"
    ;;
  all-fast)
    echo "== pytest: all fast tiers ($FAST) =="
    "$BIN/pytest" -m "$FAST"
    ;;
  changed)
    # tr: never let a Windows "\r" end up glued to a test path
    SELECTED="$("$BIN/python" scripts/changed_tests.py | tr -d '\r')"
    if [ -z "$SELECTED" ]; then
      echo "== pytest: no changed code needs tests (docs only) =="
    elif [ "$SELECTED" = "ALL_FAST" ]; then
      echo "== pytest: change not mappable -> all fast tiers ($FAST) =="
      "$BIN/pytest" -m "$FAST"
    else
      echo "== pytest: changed-module tests: $(echo $SELECTED) =="
      # shellcheck disable=SC2086 # one test path per word, none contain spaces
      "$BIN/pytest" $SELECTED
    fi
    ;;
esac

echo "All checks passed."
