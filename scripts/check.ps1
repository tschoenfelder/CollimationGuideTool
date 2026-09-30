# Developer quality gate (see CONTRIBUTING.md, "Test pyramid and the
# risk-based minimum gate"). Every mode runs ruff, mypy and import-linter
# first, then:
#
#   (default)     the tests covering what you changed vs origin/main, chosen
#                 by scripts/changed_tests.py (all fast tiers if the change
#                 can't be mapped, e.g. conftest/pyproject/shared fakes)
#   -AllFast      every fast tier: -m "unit or component or contract"
#   -Integration  the slow tiers: -m "integration or acceptance"
#   -Release      the full release gate with coverage (fail-under 80) --
#                 run before pushing a release; CI's coverage job mirrors it
param(
    [switch]$AllFast,
    [switch]$Integration,
    [switch]$Release
)
$ErrorActionPreference = "Stop"
if (@($AllFast, $Integration, $Release | Where-Object { $_ }).Count -gt 1) {
    [Console]::Error.WriteLine("Conflicting modes: pass at most one of -AllFast, -Integration, -Release")
    exit 2
}
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
if ($LASTEXITCODE -ne 0) { exit 1 }

Write-Host "== mypy =="
& "$Bin\mypy" .
if ($LASTEXITCODE -ne 0) { exit 1 }

Write-Host "== import-linter =="
& "$Bin\lint-imports"
if ($LASTEXITCODE -ne 0) { exit 1 }

$Fast = "unit or component or contract"

if ($Release) {
    Write-Host "== pytest: full release gate + coverage (core, collimation, guide, contracts, integration, regressions, acceptance) =="
    & "$Bin\pytest" tests/core tests/collimation tests/guide tests/contracts tests/integration tests/regressions tests/acceptance --cov --cov-report=term-missing
} elseif ($Integration) {
    Write-Host "== pytest: integration + acceptance tiers =="
    & "$Bin\pytest" -m "integration or acceptance"
} elseif ($AllFast) {
    Write-Host "== pytest: all fast tiers ($Fast) =="
    & "$Bin\pytest" -m $Fast
} else {
    $Selected = @(& "$Bin\python" scripts/changed_tests.py | ForEach-Object { $_.Trim() } | Where-Object { $_ })
    if ($LASTEXITCODE -ne 0) { exit 1 }
    if ($Selected.Count -eq 0) {
        Write-Host "== pytest: no changed code needs tests (docs only) =="
    } elseif ($Selected -contains "ALL_FAST") {
        Write-Host "== pytest: change not mappable -> all fast tiers ($Fast) =="
        & "$Bin\pytest" -m $Fast
    } else {
        Write-Host "== pytest: changed-module tests: $($Selected -join ' ') =="
        & "$Bin\pytest" @Selected
    }
}
if ($LASTEXITCODE -ne 0) { exit 1 }

Write-Host "All checks passed."
