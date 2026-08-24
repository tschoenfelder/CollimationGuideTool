# CollimationGuideTool

Two PySide6 desktop apps — **CollimationTool** and **GuideTool** — sharing one
UI-independent core library, `astrotool_core`, for camera access, frame
handling, target/ROI tracking, mount access, acquisition, and session
logging.

- Architecture and rationale: [`collimation-guidetool-architektur.md`](collimation-guidetool-architektur.md)
- Implementation plan and staged build order: [`PLAN.md`](PLAN.md)
- Contribution/change-safety rules: [`CONTRIBUTING.md`](CONTRIBUTING.md)

## Status

Scaffolding stage. No production code yet — see `PLAN.md` for the staged
build order (Stage 0 of 8).

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
