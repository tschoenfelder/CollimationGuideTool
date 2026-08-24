# Run before considering any change done. Always runs core+collimation+guide
# regardless of which app the change appears to touch (see CONTRIBUTING.md).
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

Write-Host "== ruff =="
ruff check .
if (-not $?) { exit 1 }

Write-Host "== mypy =="
mypy .
if (-not $?) { exit 1 }

Write-Host "== import-linter =="
lint-imports
if (-not $?) { exit 1 }

Write-Host "== pytest: core, collimation, guide =="
pytest tests/core tests/collimation tests/guide
if (-not $?) { exit 1 }

Write-Host "All checks passed."
