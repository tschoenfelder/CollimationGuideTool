# CollimationGuideTool

Two PySide6 desktop apps — **CollimationTool** and **GuideTool** — sharing one
UI-independent core library, `astrotool_core`, for camera access, frame
handling, target/ROI tracking, mount access, acquisition, and session
logging.

- Architecture and rationale: [`collimation-guidetool-architektur.md`](collimation-guidetool-architektur.md)
- Implementation plan and staged build order: [`PLAN.md`](PLAN.md)
- Contribution/change-safety rules: [`CONTRIBUTING.md`](CONTRIBUTING.md)
- Installing and upgrading on a Raspberry Pi: [`install.md`](install.md)

## Status

All 8 stages complete (see `PLAN.md`), tagged `v0.1.0`: shared core, both
apps' domain and application layers, a minimal PySide6 UI for each, and
regression-protection hardening (contract tests for every adapter,
tolerance-based golden-master replay tests, a Ruff `C90` complexity gate).
Run `collimation-tool` / `guide-tool` after installing to see both live
against synthetic data (a donut sequence and a fake guide star,
respectively) with no hardware attached.

## Prior art

This project draws on two sibling projects by the same author:

- [`smart_telescope`](../smart_telescope) — camera/mount/frame/session
  patterns are ported and adapted from here (not a runtime dependency).
- [`SmartTScopeLiveAnalysis`](https://github.com/tschoenfelder/SmartTScopeLiveAnalysis) —
  a runtime dependency for multi-source star detection and multi-frame
  temporal track-linking.
- [`OnStepAdapter`](https://github.com/tschoenfelder/OnStepAdapter) — a
  runtime dependency for mount/focuser control.

## Development setup

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
```

```powershell
pytest tests/
ruff check .
mypy .
lint-imports
```

## Deliberate additions vs. the architecture doc

- `packages/astrotool_core/focus/` — a focuser port (mirroring `mount/`:
  port + no-op + fake) is not in the doc's literal tree but is needed for
  CollimationTool's focus control, and a focuser is a separate physical
  device from the mount. See `PLAN.md` for the full rationale.
