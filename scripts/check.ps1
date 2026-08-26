# Run before considering any change done. Always runs core+collimation+guide
# regardless of which app the change appears to touch (see CONTRIBUTING.md).
#
# Pass -Release before pushing a release: additionally runs tests/contracts,
# tests/integration, and the below-UI tests/acceptance regression suite.
param(
    [switch]$Release
)
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

if (Test-Path ".venv\Scripts") {
    $Bin = ".venv\Scripts"
} elseif (Test-Path ".venv\bin") {
    $Bin = ".venv\bin"
} else {
    Write-Error "No .venv found - run: pip install -e `".[dev]`""
    exit 1
}

Write-Host "== ruff =="
& "$Bin\ruff" check .
if (-not $?) { exit 1 }

Write-Host "== mypy =="
& "$Bin\mypy" .
if (-not $?) { exit 1 }

Write-Host "== import-linter =="
& "$Bin\lint-imports"
if (-not $?) { exit 1 }

if ($Release) {
    Write-Host "== pytest: full release gate (core, collimation, guide, contracts, integration, acceptance) =="
    & "$Bin\pytest" tests/core tests/collimation tests/guide tests/contracts tests/integration tests/acceptance
} else {
    Write-Host "== pytest: core, collimation, guide =="
    & "$Bin\pytest" tests/core tests/collimation tests/guide
}
if (-not $?) { exit 1 }

Write-Host "All checks passed."
