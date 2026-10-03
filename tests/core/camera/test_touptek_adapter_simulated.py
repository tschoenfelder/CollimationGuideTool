"""Issue #51: the production `TouptekCameraAdapter` over the simulated ToupTek
SDK (`astrotool_core.testing.sim_touptek`): per-model capability matrix,
E_NOTIMPL for unsupported calls, exposure/gain metadata, frame timing on fake
time, SDK capture events. The real connect/configure/capture/pull path runs;
only the vendor module is simulated."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import numpy as np
import pytest
from astropy.io import fits
from astrotool_core.camera import touptek_adapter
from astrotool_core.camera.capabilities import ConversionGain
from astrotool_core.camera.touptek_adapter import TouptekCameraAdapter
from astrotool_core.testing import (
    CAMERA_MODELS,
    SimulatedToupcam,
    SimulatedToupcamSdk,
    install_simulated_toupcam,
)
from astrotool_core.testing.sim_touptek import SDK_CONSTANTS, Scene
from astrotool_core.timing import FakeClock

_VENDORED_SDK = Path(__file__).resolve().parents[3] / "resources" / "touptek" / "toupcam.py"


def _connected(
    monkeypatch: pytest.MonkeyPatch, model: str, *, scene: Scene | None = None
) -> tuple[TouptekCameraAdapter, SimulatedToupcam, FakeClock]:
    clock = FakeClock()
    sdk = SimulatedToupcamSdk(clock=clock)
    device = sdk.add_camera(model, scene=scene)
    install_simulated_toupcam(monkeypatch, sdk)
    adapter = TouptekCameraAdapter(camera_id=device.id)
    adapter.connect()
    return adapter, sdk.handles[-1], clock


class TestSimulatorMatchesTheVendoredSdk:
    def test_every_copied_constant_equals_resources_touptek_toupcam_py(self) -> None:
        source = _VENDORED_SDK.read_text(encoding="utf-8")
        for name, value in SDK_CONSTANTS.items():
            match = re.search(rf"^\s*{name}\s*=\s*(0x[0-9A-Fa-f]+|\d+)", source, re.MULTILINE)
            assert match is not None, f"{name} not found in {_VENDORED_SDK}"
            assert int(match.group(1), 0) == value, name

    def test_the_adapter_configures_with_the_sdks_real_option_ids(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The adapter looks option ids up by name on the SDK module first, so
        on the real module its (partly wrong) fallback ids are never used."""
        _, cam, _ = _connected(monkeypatch, "G3M678M")
        for name in ("TOUPCAM_OPTION_FLUSH", "TOUPCAM_OPTION_TRIGGER", "TOUPCAM_OPTION_RGB"):
            assert SDK_CONSTANTS[name] in cam.options, name


_ADAPTER_SOURCE = Path(touptek_adapter.__file__)

#: Adapter helpers that take an SDK constant's name (first argument after
#: `self` / the module) and nothing else that could be a fallback.
_NAME_TAKING_HELPERS = {"_has_flag": 1, "_get_option": 1, "_put_option": 2, "_sdk_constant": 2}


def _vendored_value(source: str, name: str) -> int | None:
    match = re.search(rf"^\s*{name}\s*=\s*(0x[0-9A-Fa-f]+|\d+)", source, re.MULTILINE)
    return None if match is None else int(match.group(1), 0)


class TestAdapterConstantsComeFromTheVendoredSdk:
    """S6.5a: the SDK is the single source of every flag/option/event/error
    value. The adapter looks flags and options up by name on the SDK module;
    its only literals are `_SDK_FALLBACKS`, keyed by SDK name, so a call site
    names a constant and cannot pair it with a wrong fallback (a copied
    `_FLAG_MONO = 0x40` was really TOUPCAM_FLAG_USB30)."""

    def test_every_fallback_equals_the_vendored_sdk_value(self) -> None:
        source = _VENDORED_SDK.read_text(encoding="utf-8")
        assert {"TOUPCAM_FLAG_MONO", "TOUPCAM_OPTION_RGB", "E_NOTIMPL"} <= set(
            touptek_adapter._SDK_FALLBACKS
        )
        mismatches = []
        for name, value in sorted(touptek_adapter._SDK_FALLBACKS.items()):
            sdk_value = _vendored_value(source, name)
            if sdk_value != value:
                mismatches.append(f"{name}: adapter 0x{value:X}, vendored SDK {sdk_value}")
        assert mismatches == []

    def test_no_module_level_int_constant_outside_the_sdk_table(self) -> None:
        """Any module-level int literal (e.g. `_TEC_ONOFF = 0x40`) would be a
        second, unchecked copy of SDK knowledge."""
        tree = ast.parse(_ADAPTER_SOURCE.read_text(encoding="utf-8"))
        loose = [
            ast.unparse(node)
            for node in tree.body
            if isinstance(node, (ast.Assign, ast.AnnAssign))
            and node.value is not None
            and any(
                isinstance(c, ast.Constant) and type(c.value) is int for c in ast.walk(node.value)
            )
            and not (
                isinstance(node, ast.AnnAssign)
                and isinstance(node.target, ast.Name)
                and node.target.id == "_SDK_FALLBACKS"
            )
        ]
        assert loose == []

    def test_every_call_site_names_a_known_constant_and_passes_no_fallback(self) -> None:
        tree = ast.parse(_ADAPTER_SOURCE.read_text(encoding="utf-8"))
        problems = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            helper = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if helper not in _NAME_TAKING_HELPERS:
                continue
            arity = _NAME_TAKING_HELPERS[helper]
            args = node.args
            name_arg = args[0] if helper != "_sdk_constant" else args[1]
            literal = name_arg.value if isinstance(name_arg, ast.Constant) else None
            where = f"line {node.lineno}: {ast.unparse(node)}"
            if len(args) != arity:
                problems.append(f"{where}: expected {arity} args")
            elif helper == "_sdk_constant" and isinstance(name_arg, ast.Name):
                continue  # the helpers' own pass-through of `name`
            elif literal not in touptek_adapter._SDK_FALLBACKS:
                problems.append(f"{where}: name not in _SDK_FALLBACKS")
        assert problems == []

    def test_mono_follows_the_sdk_modules_flag_not_the_fallback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Single source: with a (deliberately wrong) fallback the SDK module's
        TOUPCAM_FLAG_MONO still decides."""
        monkeypatch.setitem(
            touptek_adapter._SDK_FALLBACKS,
            "TOUPCAM_FLAG_MONO",
            SDK_CONSTANTS["TOUPCAM_FLAG_USB30"],
        )
        colour, _, _ = _connected(monkeypatch, "SYNTH-COLOR-USB3")
        mono, _, _ = _connected(monkeypatch, "SYNTH-MONO-USB2")
        assert (colour.is_color_sensor(), mono.is_color_sensor()) == (True, False)


class TestMonoOrColourFollowsTheSdkFlag:
    """`is_color_sensor()` must follow the SDK's TOUPCAM_FLAG_MONO (0x10)."""

    @pytest.mark.parametrize(
        ("model", "mono"),
        [
            ("G3M678M", True),
            ("GPCMOS02000KPA", False),
            ("ATR585M", True),
            # S6.5a: the adapter's old `_FLAG_MONO` (0x40) was the SDK's FLAG_USB30,
            # so mono/colour followed the USB3 bit. These two models separate them.
            ("SYNTH-COLOR-USB3", False),
            ("SYNTH-MONO-USB2", True),
        ],
    )
    def test_is_color_sensor_matches_the_model(
        self, monkeypatch: pytest.MonkeyPatch, model: str, mono: bool
    ) -> None:
        adapter, _, _ = _connected(monkeypatch, model)
        assert CAMERA_MODELS[model].mono is mono
        assert adapter.is_color_sensor() is not mono


class TestCapabilityMatrix:
    @pytest.mark.parametrize(
        ("model", "cooling", "hcg", "black_level", "mono", "bit_depth"),
        [
            ("G3M678M", False, False, True, True, 16),
            ("GPCMOS02000KPA", False, False, False, False, 12),
            ("ATR585M", True, True, True, True, 16),
        ],
    )
    def test_descriptor_reflects_the_model(
        self,
        monkeypatch: pytest.MonkeyPatch,
        model: str,
        cooling: bool,
        hcg: bool,
        black_level: bool,
        mono: bool,
        bit_depth: int,
    ) -> None:
        adapter, cam, _ = _connected(monkeypatch, model)
        caps = adapter.get_descriptor().capabilities
        assert (caps.supports_cooling, caps.supports_hcg, caps.supports_black_level) == (
            cooling,
            hcg,
            black_level,
        )
        assert CAMERA_MODELS[model].mono is mono  # SDK truth; adapter side: TestMonoOrColour...
        assert caps.bit_depth == bit_depth
        assert (caps.sensor_width_px, caps.sensor_height_px) == (
            CAMERA_MODELS[model].width,
            CAMERA_MODELS[model].height,
        )
        assert (caps.min_gain, caps.max_gain) == CAMERA_MODELS[model].gain_range[:2]
        assert cam.notimpl_calls == []

    def test_a_cooled_camera_reports_its_target_range_and_temperature(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        adapter, _, _ = _connected(monkeypatch, "ATR585M")
        caps = adapter.get_descriptor().capabilities
        assert (caps.min_target_temp_c, caps.max_target_temp_c) == (-50.0, 40.0)
        assert adapter.get_temperature() == 12.5
        adapter.set_conversion_gain(ConversionGain.HCG)
        assert adapter.get_conversion_gain() is ConversionGain.HCG


class TestUnsupportedCapabilityIsNeverCalled:
    """Defect class "unsupported camera capability": field reports 2026-09-29
    (531a952 -- G3M678M has no TEC, but get_descriptor() called
    get_TecTargetRange() on every frame, failing each time) and 4730c55
    (GPCMOS02000KPA has no temperature sensor, get_Temperature() was retried
    on every poll). The simulator raises E_NOTIMPL exactly as the SDK does,
    so any doomed call shows up in `notimpl_calls`."""

    def test_no_tec_model_survives_a_session_of_per_frame_descriptor_reads(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        adapter, cam, _ = _connected(monkeypatch, "G3M678M")
        for _ in range(20):  # CameraPanel reads the descriptor on every frame
            adapter.get_descriptor()
            adapter.get_cooling_enabled()
            adapter.get_target_temperature()
            adapter.get_conversion_gain()
        adapter.set_cooling_enabled(True)
        adapter.set_target_temperature(-5.0)
        adapter.set_conversion_gain(ConversionGain.HCG)
        assert cam.notimpl_calls == []

    def test_missing_temperature_sensor_is_probed_once_per_connect(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        adapter, cam, _ = _connected(monkeypatch, "GPCMOS02000KPA")
        for _ in range(10):  # the panel polls temperature on a timer
            assert adapter.get_temperature() is None
        assert cam.notimpl_calls == ["get_Temperature"]


class TestExposureAndFrames:
    def test_a_long_exposure_takes_fake_time_and_carries_its_metadata(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        adapter, cam, clock = _connected(monkeypatch, "G3M678M")
        adapter.set_exposure_ms(30_000.0)
        adapter.set_gain(800)
        before = clock.monotonic()
        frame = adapter.capture(30.0)
        assert clock.monotonic() - before == pytest.approx(30.0)
        assert frame.exposure_seconds == pytest.approx(30.0)
        header = frame.header
        assert isinstance(header, fits.Header)
        assert header["EXPTIME"] == pytest.approx(30.0)
        assert header["GAIN"] == 800
        record = cam.exposures[-1]
        assert record.exposure_s == pytest.approx(30.0)
        assert record.gain == 800
        assert record.start == pytest.approx(before)

    def test_the_vendor_settle_wait_runs_on_the_clock(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, _, clock = _connected(monkeypatch, "G3M678M")
        assert clock.sleeps == [0.2]  # _prepare_capture_mode's 0.2 s, recorded not slept

    def test_scene_pixels_arrive_in_native_adc_range(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def scene(start: float, end: float) -> np.ndarray:
            image = np.full((64, 96), 100.0)
            image[30:34, 40:44] = 3000.0
            return image

        adapter, _, _ = _connected(monkeypatch, "GPCMOS02000KPA", scene=scene)
        frame = adapter.capture(0.5)
        assert frame.pixels.shape == (64, 96)
        assert float(frame.pixels.max()) == 3000.0
        assert frame.bit_depth == 12

    def test_exposure_is_clamped_by_the_camera_not_trusted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        adapter, _, _ = _connected(monkeypatch, "G3M678M")
        adapter.set_exposure_ms(0.001)
        assert adapter.get_exposure_ms() == pytest.approx(0.1)  # model minimum: 100 us

    @pytest.mark.parametrize(
        ("outcome", "message"),
        [
            ("trigger_fail", "Camera trigger failed"),
            ("disconnected", "Camera disconnected during capture"),
            ("error", "Camera reported error during capture"),
        ],
    )
    def test_sdk_capture_events_surface_as_errors(
        self, monkeypatch: pytest.MonkeyPatch, outcome: str, message: str
    ) -> None:
        adapter, cam, _ = _connected(monkeypatch, "G3M678M")
        cam.trigger_outcome = outcome
        with pytest.raises(RuntimeError, match=message):
            adapter.capture(0.01)


class TestIdentity:
    def test_two_models_are_selected_by_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sdk = SimulatedToupcamSdk(clock=FakeClock())
        main = sdk.add_camera("G3M678M")
        guide = sdk.add_camera("GPCMOS02000KPA")
        install_simulated_toupcam(monkeypatch, sdk)
        adapter = TouptekCameraAdapter(camera_id=guide.id)
        adapter.connect()
        assert adapter.get_descriptor().logical_name == "GPCMOS02000KPA"
        assert adapter.device_id == guide.id != main.id
        assert sdk.enum_calls == 1
