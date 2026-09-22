"""AGENTS.md "Consumer enforcement": no OnStep access outside OnStepAdapter.

Two guards besides the import-linter contract in pyproject.toml:
- the app's default mount/focuser/pulse objects are the OnStepAdapter shims,
  all sharing ONE connection;
- no source file speaks the OnStep INDI driver's mount/focuser properties or
  names the "LX200 OnStep" device (INDI stays valid for cameras/filter wheels),
  except the one place that must: `astrotool_core.onstep.settings`'s own
  `[indi] device` config default, telling OnStepAdapter's `OnStepIndiClient`
  itself which INDI device to use (>= 0.4.0, AGENTS.md: OnStepAdapter itself
  is the INDI-backed transport for this deployment) -- that is data handed
  to OnStepAdapter, not this app reaching the device directly. Its test
  double (`fake_onstep_indi_client.py`) mirrors the same default for the
  same reason.
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


#: The one legitimate exception: the `[indi] device` config default,
#: handed to OnStepAdapter's own `OnStepIndiClient` -- not this app
#: reaching the INDI device directly -- plus its test double's mirror.
_ALLOWED_DEVICE_NAME_SITES = {
    Path("packages/astrotool_core/onstep/settings.py"),
    Path("packages/astrotool_core/testing/fake_onstep_indi_client.py"),
}


def test_no_source_file_bypasses_onstepadapter() -> None:
    offenders: list[str] = []
    for base in ("packages", "apps", "scripts"):  # incl. the test doubles
        for path in (_ROOT / base).rglob("*.py"):
            relative = path.relative_to(_ROOT)
            text = path.read_text(encoding="utf-8")
            for token in _FORBIDDEN:
                if token in text and not (
                    token == "LX200 OnStep" and relative in _ALLOWED_DEVICE_NAME_SITES
                ):
                    offenders.append(f"{relative}: {token}")
    assert not offenders, "direct OnStep access outside OnStepAdapter (AGENTS.md):\n" + "\n".join(
        offenders
    )
