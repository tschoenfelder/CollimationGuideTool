"""Config-source contract (#55 D01-D03, CONTRIBUTING.md "Duplicated knowledge").

The rig's EFW device name, the INDI endpoint (host and port 7624) and the config
file locations each have ONE owner:

- `astrotool_core/config/device_defaults.py` -- EFW device name, INDI host/port;
- `astrotool_core/config/paths.py` -- `~/.CollimationGuideTool`, `~/.SmartTScope`,
  `config.toml`.

This scans every production module and dev script (packages/, apps/, scripts/)
with the AST and fails if one of those literals is re-stated anywhere else -- a
second copy is how the wrong "ToupTek EFW 1" shipped (#47). Strings are searched,
not compared: the port as an int/float literal or as a standalone number inside
any string ("localhost:7624", URLs), either host anywhere in a string, any
"EFW <digit>" and any "ToupTek EFW" prefix (so f-strings and concatenations are
caught), and the config path parts. Docstrings are prose, not knowledge, and
are skipped; comments are not in the AST. Exceptions are pinned below with a
reason, and an exception that no longer matches anything fails too.
"""

from __future__ import annotations

import ast
import inspect
import re
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from astrotool_core.config import (
    camera_settings,
    device_defaults,
    mount_alignment_settings,
    paths,
)
from collimation_tool.domain import target_mode

_REPO = Path(__file__).resolve().parents[2]
_PRODUCTION_ROOTS = (_REPO / "packages", _REPO / "apps", _REPO / "scripts")

_OWNERS = frozenset(
    {
        "packages/astrotool_core/config/device_defaults.py",
        "packages/astrotool_core/config/paths.py",
    }
)

#: (module, literal) -> why it may stay. Keep this short.
_ALLOWED: dict[tuple[str, object], str] = {
    # Empty since S6.6b: D09's two pins (fake_onstep_indi_client.py's host/port) are gone --
    # the test double reads config.device_defaults like every other consumer.
}

_PORT = 7624
_EFW = "EFW <n>"
#: (pattern, the owned literal it re-states) -- searched inside every string.
_STRING_PATTERNS: tuple[tuple[re.Pattern[str], object], ...] = (
    (re.compile(r"(?<![\w.])7624(?!\w)"), _PORT),
    (re.compile(r"\blocalhost\b", re.IGNORECASE), "localhost"),
    (re.compile(r"(?<![\d.])127\.0\.0\.1(?!\d)"), "127.0.0.1"),
    (re.compile(r"\bEFW\s*\d"), _EFW),
    (re.compile(r"ToupTek\s+EFW"), _EFW),
    (re.compile(r"(?<!\w)\.CollimationGuideTool\b"), ".CollimationGuideTool"),
    (re.compile(r"(?<!\w)\.SmartTScope\b"), ".SmartTScope"),
    (re.compile(r"(?<![\w.])config\.toml\b"), "config.toml"),
)


def owned_in(value: object) -> list[object]:
    """Every owned literal the constant `value` re-states (de-duplicated, in order)."""
    if isinstance(value, bool):
        return []
    if isinstance(value, int | float):
        return [_PORT] if value == _PORT else []
    if not isinstance(value, str):
        return []
    hits: list[object] = []
    for pattern, literal in _STRING_PATTERNS:
        if pattern.search(value) and literal not in hits:
            hits.append(literal)
    return hits


def _docstring_ids(tree: ast.AST) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                ids.add(id(body[0].value))
    return ids


def owned_literals(source: str) -> Iterator[tuple[int, object]]:
    """`(line, literal)` for every owned literal in `source` outside docstrings."""
    tree = ast.parse(source)
    docstrings = _docstring_ids(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and id(node) not in docstrings:
            for hit in owned_in(node.value):
                yield node.lineno, hit


def _production_modules() -> Iterator[tuple[str, Path]]:
    for root in _PRODUCTION_ROOTS:
        for path in sorted(root.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            yield path.relative_to(_REPO).as_posix(), path


def _findings() -> list[tuple[str, int, object]]:
    found = []
    for rel, path in _production_modules():
        if rel in _OWNERS:
            continue
        for line, literal in owned_literals(path.read_text(encoding="utf-8")):
            found.append((rel, line, literal))
    return found


def test_no_owned_literal_is_restated_outside_its_owner() -> None:
    offenders = [
        f"{rel}:{line}: {literal!r}"
        for rel, line, literal in _findings()
        if (rel, literal) not in _ALLOWED
    ]
    assert offenders == [], (
        "re-stated device default / config location -- import it from "
        "astrotool_core.config.device_defaults or astrotool_core.config.paths:\n"
        + "\n".join(offenders)
    )


def test_every_exception_is_still_needed() -> None:
    used = {(rel, literal) for rel, _, literal in _findings()}
    stale = [key for key in _ALLOWED if key not in used]
    assert stale == []


def test_the_owners_hold_the_literals() -> None:
    """Guards against the scan passing vacuously (e.g. an owner moved)."""
    for rel in _OWNERS:
        assert (_REPO / rel).is_file(), rel
    assert owned_in(device_defaults.EFW_DEVICE_NAME) == [_EFW]
    assert owned_in(device_defaults.INDI_HOST) == ["127.0.0.1"]
    assert owned_in(device_defaults.INDI_PORT) == [7624]
    assert owned_in(paths.APP_DIR_NAME) == [".CollimationGuideTool"]
    assert owned_in(paths.SMARTTSCOPE_DIR_NAME) == [".SmartTScope"]
    assert owned_in(paths.CONFIG_FILE_NAME) == ["config.toml"]
    assert owned_in(str(paths.DEFAULT_OWN_CONFIG_PATH)) == [".CollimationGuideTool", "config.toml"]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("localhost:7624", [7624, "localhost"]),
        ("http://127.0.0.1:7624", [7624, "127.0.0.1"]),
        ("7624", [7624]),
        (7624.0, [7624]),
        ("--port=7624", [7624]),
        ("EFW 2", [_EFW]),
        ("efw: EFW1", [_EFW]),
        ("ToupTek EFW ", [_EFW]),  # the constant part of f"ToupTek EFW {n}"
        ("LOCALHOST", ["localhost"]),
        ("~/.CollimationGuideTool/diagnostics", [".CollimationGuideTool"]),
        # not owned:
        ("76240", []),
        ("17624", []),
        ("1.7624", []),
        ("127.0.0.10", []),
        ("my_localhost_alias", []),
        ("pyproject.toml", []),
        ("myconfig.toml", []),
        (True, []),
        (7625, []),
    ],
)
def test_the_matcher(value: object, expected: list[object]) -> None:
    assert owned_in(value) == expected


def test_the_scanner_catches_fstrings_and_concatenation() -> None:
    source = '''
N = 2
NAME = f"ToupTek EFW {N}"
SPLIT = "ToupTek " + "EFW 2"
URL = f"http://{HOST}:7624/"
'''
    assert sorted(owned_literals(source), key=str) == sorted(
        [(3, _EFW), (4, _EFW), (5, 7624)], key=str
    )


def test_the_scanner_catches_each_kind_of_restatement() -> None:
    source = '''
"""Docstring mentioning ToupTek EFW 1 and ~/.SmartTScope/config.toml is fine."""
NAME = "ToupTek EFW 1"
PORT = 7624
HOST = "localhost"
LOOPBACK = "127.0.0.1"
PATH = Path.home() / ".CollimationGuideTool" / "config.toml"
REMOTE = "~/.SmartTScope/config.toml"
FLAG = True
OTHER_PORT = 7625


def f():
    """Function docstring: localhost 7624."""
    return "ToupTek EFW 2"
'''
    assert sorted(owned_literals(source), key=str) == sorted(
        [
            (3, _EFW),
            (4, 7624),
            (5, "localhost"),
            (6, "127.0.0.1"),
            (7, ".CollimationGuideTool"),
            (7, "config.toml"),
            (8, ".SmartTScope"),
            (8, "config.toml"),
            (15, _EFW),
        ],
        key=str,
    )


# -- D04: the built-in cooling target (S6.5) ------------------------------------------------
#
# `astrotool_core/config/camera_settings.py` owns DEFAULT_TARGET_TEMPERATURE_C (-10 C).
# The number alone is too common to scan for, so a re-statement is a numeric literal
# equal to the owned value on a source line that mentions a temperature ("temp").

_COOLING_OWNER = "packages/astrotool_core/config/camera_settings.py"

#: module -> why it may still re-state the default. Empty since S6.6b: the three CameraPort
#: test doubles / demo cameras pinned here by S6.5 now read the owner.
_COOLING_ALLOWED: dict[str, str] = {}


def cooling_default_lines(source: str, owned: float) -> list[int]:
    """Lines of `source` with a numeric literal equal to `owned` (sign included) on a
    line that mentions a temperature."""
    lines = source.splitlines()
    hits: list[int] = []
    for node in ast.walk(ast.parse(source)):
        value: object
        if (
            isinstance(node, ast.UnaryOp)
            and isinstance(node.op, ast.USub)
            and isinstance(node.operand, ast.Constant)
        ):
            operand = node.operand.value
            value = -operand if isinstance(operand, int | float) else None
            line = node.lineno
        elif isinstance(node, ast.Constant):
            value, line = node.value, node.lineno
        else:
            continue
        if isinstance(value, bool) or not isinstance(value, int | float) or value != owned:
            continue
        if "temp" in lines[line - 1].lower() and line not in hits:
            hits.append(line)
    return hits


def _cooling_findings() -> dict[str, list[int]]:
    owned = camera_settings.DEFAULT_TARGET_TEMPERATURE_C
    found: dict[str, list[int]] = {}
    for rel, path in _production_modules():
        if rel == _COOLING_OWNER:
            continue
        hits = cooling_default_lines(path.read_text(encoding="utf-8"), owned)
        if hits:
            found[rel] = hits
    return found


def test_the_cooling_default_has_one_owner() -> None:
    offenders = [
        f"{rel}:{line}"
        for rel, lines in _cooling_findings().items()
        if rel not in _COOLING_ALLOWED
        for line in lines
    ]
    assert offenders == [], (
        "re-stated cooling target default -- read "
        "astrotool_core.config.camera_settings.DEFAULT_TARGET_TEMPERATURE_C:\n"
        + "\n".join(offenders)
    )


def test_every_cooling_exception_is_still_needed() -> None:
    assert sorted(set(_COOLING_ALLOWED) - set(_cooling_findings())) == []


def test_the_cooling_owner_holds_the_value() -> None:
    source = (_REPO / _COOLING_OWNER).read_text(encoding="utf-8")
    assert camera_settings.DEFAULT_TARGET_TEMPERATURE_C == -10.0
    assert len(cooling_default_lines(source, -10.0)) == 1


def test_the_cooling_scanner_catches_each_kind_of_restatement() -> None:
    source = '''
"""Docstring: -10 C target temperature."""
class A:
    target_temperature_c: float = -10.0
    def f(self, table):
        self._target_temperature_c = -10
        spin_temp.setValue(-10.0)
        return float(table.get("target_temperature_c", -10.0))
OFFSET = -10.0
TEMP_STEP = 10.0
'''
    assert cooling_default_lines(source, -10.0) == [4, 6, 7, 8]


def test_every_consumer_observes_an_injected_cooling_default(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    qapp: Any,  # noqa: ANN401
) -> None:
    """#55: change the owner, and every consumer follows (read at call time)."""
    from astrotool_core.camera.fake_camera import FakeCamera
    from astrotool_core.camera.touptek_adapter import TouptekCameraAdapter
    from collimation_tool.ui.camera_panel import CameraPanel

    monkeypatch.setattr(camera_settings, "DEFAULT_TARGET_TEMPERATURE_C", -7.5)
    assert TouptekCameraAdapter().get_target_temperature() == -7.5
    assert (
        camera_settings.CameraPanelSettings(
            camera_id=None, exposure_ms=1.0, gain=100, auto_exposure_enabled=False
        ).target_temperature_c
        == -7.5
    )
    path = tmp_path / "config.toml"
    path.write_text("[cameras.main]\nexposure_ms = 1.0\ngain = 100\n", encoding="utf-8")
    assert camera_settings.load_camera_settings(path)["main"].target_temperature_c == -7.5
    panel = CameraPanel(FakeCamera(), title="D04", device_lister=list)
    try:
        assert panel._target_temp_spin.value() == -7.5
    finally:
        panel.deleteLater()


# -- D06/D07/D08: the last single-owner numeric defaults (S6.9) ------------------------------
#
# Like D04, the numbers alone are too common to scan for. A re-statement is a numeric literal
# equal to the owned value on a source line -- or inside a function whose name -- mentions the
# value's subject ("distance"/"terrestrial", "interval", "fraction").

_MOUNT_ALIGN_OWNER = "packages/astrotool_core/config/mount_alignment_settings.py"
_TARGET_MODE_OWNER = "apps/collimation_tool/domain/target_mode.py"

#: item -> (owner module, owned value, subject keywords).
_NUMERIC_DEFAULTS: dict[str, tuple[str, float, tuple[str, ...]]] = {
    "D06 terrestrial seed distance (m)": (
        _TARGET_MODE_OWNER,
        target_mode.TERRESTRIAL_SEED_DISTANCE_M,
        ("distance", "terrestrial"),
    ),
    "D07 stability sample interval (s)": (
        _MOUNT_ALIGN_OWNER,
        mount_alignment_settings.DEFAULT_STABILITY_SAMPLE_INTERVAL_S,
        ("interval",),
    ),
    "D08 calibration target fraction": (
        _MOUNT_ALIGN_OWNER,
        mount_alignment_settings.DEFAULT_CALIBRATION_TARGET_FRACTION,
        ("fraction",),
    ),
}


def numeric_default_lines(source: str, owned: float, keywords: tuple[str, ...]) -> list[int]:
    """Lines of `source` with a numeric literal equal to `owned` (sign included) where the line
    or the enclosing function's name mentions one of `keywords`."""
    lines = source.splitlines()
    hits: list[int] = []

    def visit(node: ast.AST, scope: str) -> None:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            scope = node.name
        value: object = None
        if (
            isinstance(node, ast.UnaryOp)
            and isinstance(node.op, ast.USub)
            and isinstance(node.operand, ast.Constant)
        ):
            operand = node.operand.value
            value = -operand if isinstance(operand, int | float) else None
        elif isinstance(node, ast.Constant):
            value = node.value
        if isinstance(node, ast.UnaryOp | ast.Constant):
            if not isinstance(value, bool) and isinstance(value, int | float) and value == owned:
                line = node.lineno
                context = (lines[line - 1] + " " + scope).lower()
                if any(k in context for k in keywords) and line not in hits:
                    hits.append(line)
            if isinstance(node, ast.UnaryOp) and isinstance(node.operand, ast.Constant):
                return  # a negated literal's operand is not a second literal
        for child in ast.iter_child_nodes(node):
            visit(child, scope)

    visit(ast.parse(source), "")
    return hits


def _numeric_findings(owner: str, owned: float, keywords: tuple[str, ...]) -> list[str]:
    return [
        f"{rel}:{line}"
        for rel, path in _production_modules()
        if rel != owner
        for line in numeric_default_lines(path.read_text(encoding="utf-8"), owned, keywords)
    ]


@pytest.mark.parametrize("item", sorted(_NUMERIC_DEFAULTS))
def test_each_numeric_default_has_one_owner(item: str) -> None:
    owner, owned, keywords = _NUMERIC_DEFAULTS[item]
    assert _numeric_findings(owner, owned, keywords) == [], (
        f"re-stated {item} -- import it from {owner}"
    )


@pytest.mark.parametrize("item", sorted(_NUMERIC_DEFAULTS))
def test_each_numeric_owner_holds_its_value_once(item: str) -> None:
    """Guards against the scan passing vacuously (owner moved, keyword no longer on the line)."""
    owner, owned, keywords = _NUMERIC_DEFAULTS[item]
    source = (_REPO / owner).read_text(encoding="utf-8")
    assert len(numeric_default_lines(source, owned, keywords)) == 1


def test_the_owned_numeric_values_are_unchanged() -> None:
    """S6.9 is behaviour-neutral: the owners hold exactly the values the copies held."""
    assert target_mode.TERRESTRIAL_SEED_DISTANCE_M == 10_000.0
    assert target_mode.ARTIFICIAL_STAR_SEED_DISTANCE_M == 30.0
    assert mount_alignment_settings.DEFAULT_STABILITY_SAMPLE_INTERVAL_S == 0.2
    assert mount_alignment_settings.DEFAULT_STABILITY_SAMPLE_COUNT == 3
    assert mount_alignment_settings.DEFAULT_CALIBRATION_TARGET_FRACTION == 0.25


def test_the_numeric_scanner_catches_each_kind_of_restatement() -> None:
    source = '''
"""Docstring: 0.25 of the frame, a 10 km target, 0.2 s apart."""
TARGET_FRACTION = 0.25
_MIN_SCALE = 0.25
def _sizing_distance_for(mode):
    return 10_000.0 if mode == "terrestrial" else None
def other():
    return 10000
def f(stability_sample_interval_s: float = 0.2, band_low: float = 0.20):
    return -0.25  # fraction, but a different (negative) value
'''
    assert numeric_default_lines(source, 0.25, ("fraction",)) == [3]
    assert numeric_default_lines(source, 10_000.0, ("distance", "terrestrial")) == [6]
    assert numeric_default_lines(source, 0.2, ("interval",)) == [9]


def test_every_consumer_observes_injected_seed_distances(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D06: change the owner, and both sizing consumers follow (read at call time)."""
    from collimation_tool.ui.main_window import MainWindow
    from collimation_tool.ui.mount_test_move_panel import MountTestMovePanel

    monkeypatch.setattr(target_mode, "TERRESTRIAL_SEED_DISTANCE_M", 12_345.0)
    monkeypatch.setattr(target_mode, "ARTIFICIAL_STAR_SEED_DISTANCE_M", 42.0)
    natural: Any = SimpleNamespace(_artificial_star_selected=lambda: False)
    artificial: Any = SimpleNamespace(_artificial_star_selected=lambda: True)
    assert MainWindow._calibration_distance_m(natural, "terrestrial") == 12_345.0
    assert MainWindow._calibration_distance_m(natural, "star") is None
    assert MainWindow._calibration_distance_m(artificial, "terrestrial") == 42.0
    panel: Any = SimpleNamespace(_calibration_distance_m=None)
    assert MountTestMovePanel._sizing_distance_for(panel, "terrestrial") == 12_345.0
    assert MountTestMovePanel._sizing_distance_for(panel, "star") is None


def test_the_stability_and_fraction_defaults_reach_every_default_consumer() -> None:
    """D07/D08: every built-in default is the owner's value (no second literal to drift)."""
    from astrotool_core.acquisition import motion_aware_acquisition as maa
    from astrotool_core.mount.movement_sizing import SizingPolicy

    owner = mount_alignment_settings
    settings = owner.MountAlignmentSettings()
    assert settings.stability_sample_interval_s == owner.DEFAULT_STABILITY_SAMPLE_INTERVAL_S
    assert settings.stability_sample_count == owner.DEFAULT_STABILITY_SAMPLE_COUNT
    assert settings.calibration_target_fraction == owner.DEFAULT_CALIBRATION_TARGET_FRACTION
    for function in (maa.acquire_verified_frame, maa.acquire_verified_frames):
        parameters = inspect.signature(function).parameters
        interval = parameters["stability_sample_interval_s"].default
        assert interval == owner.DEFAULT_STABILITY_SAMPLE_INTERVAL_S
        count = parameters["stability_sample_count"].default
        assert count == owner.DEFAULT_STABILITY_SAMPLE_COUNT
    fraction = SizingPolicy(center_rate_x=8.0).target_fraction
    assert fraction == owner.DEFAULT_CALIBRATION_TARGET_FRACTION
    from collimation_tool.application import star_acquisition

    step_fraction = star_acquisition._STEP_FRAME_FRACTION
    assert step_fraction == owner.DEFAULT_CALIBRATION_TARGET_FRACTION


def test_an_injected_mount_align_configuration_reaches_sizing_and_capture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D07/D08: an alternate `MountAlignmentSettings` is what calibration sizing and the
    stability-verified capture use (production never relies on the function defaults)."""
    from astrotool_core.mount import MountCapabilities, TrackingMode
    from astrotool_core.mount.movement_sizing import CameraGeometry
    from astrotool_core.timing.fake_clock import FakeClock
    from collimation_tool.application import mount_align_capture
    from collimation_tool.application.mount_align_sizing import CalibrationSizing

    alternate = mount_alignment_settings.MountAlignmentSettings(
        calibration_target_fraction=0.4,
        stability_sample_interval_s=0.37,
        stability_sample_count=5,
    )

    class _AngularMount:
        def capabilities(self) -> MountCapabilities:
            return MountCapabilities(
                supports_pulse_guiding=False,
                min_pulse_ms=0,
                max_pulse_ms=0,
                supports_angular_moves=True,
                min_angular_arcsec=30.0,
            )

    sizing = CalibrationSizing(mount=_AngularMount, settings=lambda: alternate)
    sizing.start(
        angular_only=True, cameras=[CameraGeometry("left", 1000, 800, 1.0)], distance_m=None
    )
    assert sizing.policy is not None
    assert sizing.policy.target_fraction == 0.4

    seen: dict[str, Any] = {}

    def recorder(sources: dict[str, Any], **kwargs: Any) -> dict[str, Any]:  # noqa: ANN401
        seen.update(kwargs)
        return {key: SimpleNamespace(frame=None) for key in sources}

    monkeypatch.setattr(mount_align_capture, "acquire_verified_frames", recorder)
    request = mount_align_capture.CaptureRequest(
        mode="star",
        label="nudge_before",
        after_monotonic=None,
        verify_stability=True,
        timeout_s=5.0,
        required_tracking=TrackingMode.OFF,
        movement_context=None,
        left_state=None,
        right_state=None,
    )
    no_wait: Any = None
    mount_align_capture.capture_blocking(
        request,
        tracking_failure=lambda _required, _mode: None,
        get_left_frame=lambda: None,
        get_right_frame=lambda: None,
        wait_for_left_frame=no_wait,
        wait_for_right_frame=no_wait,
        settings=alternate,
        clock=FakeClock(start=100.0),
    )
    assert seen["stability_sample_interval_s"] == 0.37
    assert seen["stability_sample_count"] == 5
