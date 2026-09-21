"""AGENTS.md "Consumer enforcement": no OnStep access outside OnStepAdapter.

Two guards besides the import-linter contract in pyproject.toml:
- the app's default mount/focuser/pulse objects are the OnStepAdapter shims,
  all sharing ONE connection;
- no source file speaks the OnStep INDI driver's mount/focuser properties or
  names the "LX200 OnStep" device (INDI stays valid for cameras/filter wheels).
"""

from __future__ import annotations

from pathlib import Path

from astrotool_core.onstep import (
    OnStepFocuserAdapter,
    OnStepMountParkAdapter,
    OnStepMountPulseAdapter,
)
from collimation_tool.main import (
    _default_focuser,
    _default_mount,
    _default_onstep_connection,
    _default_pulse_mount,
)

_ROOT = Path(__file__).resolve().parents[2]

#: Things only a direct OnStep-over-INDI (or raw) implementation would contain.
_FORBIDDEN = (
    "LX200 OnStep",
    "indi_lx200_OnStep",
    "TELESCOPE_MOTION_NS",
    "TELESCOPE_MOTION_WE",
    "TELESCOPE_SLEW_RATE",
    "TELESCOPE_PARK",
    "TELESCOPE_TRACK_STATE",
    "FOCUS_ABORT_MOTION",
    "TELESCOPE_ABORT_MOTION",
)


def test_default_onstep_objects_are_the_adapter_shims_on_one_connection() -> None:
    connection = _default_onstep_connection()
    focuser = _default_focuser(connection)
    park = _default_mount(connection)
    pulse = _default_pulse_mount(connection)
    assert isinstance(focuser, OnStepFocuserAdapter)
    assert isinstance(park, OnStepMountParkAdapter)
    assert isinstance(pulse, OnStepMountPulseAdapter)
    for shim in (focuser, park, pulse):
        assert shim._connection is connection  # noqa: SLF001 -- the point of the test


def test_no_source_file_bypasses_onstepadapter() -> None:
    offenders: list[str] = []
    for base in ("packages", "apps", "scripts"):
        for path in (_ROOT / base).rglob("*.py"):
            if "testing" in path.parts:  # generic INDI test double, not app code
                continue
            text = path.read_text(encoding="utf-8")
            offenders += [f"{path.relative_to(_ROOT)}: {t}" for t in _FORBIDDEN if t in text]
    assert not offenders, (
        "direct OnStep access outside OnStepAdapter (AGENTS.md):\n" + "\n".join(offenders)
    )
