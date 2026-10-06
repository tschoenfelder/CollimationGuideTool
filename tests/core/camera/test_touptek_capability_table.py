"""S6.5 (#55 audit C01): the ToupTek adapter's capability table.

One decision per feature, made once per connection: the flag-gated features
from the SDK's model flag word, the rest (temperature sensor, gain/exposure/TEC
target ranges) from one query each at connect. Every call site consults the
table; an SDK answer of E_NOTIMPL is a model property, recorded in the table
the first time and never asked again. Runs the production adapter over the
#51 ToupTek simulator (`astrotool_core.testing.sim_touptek`).

The defect class it pins: unsupported-camera-capability log spam --
531a952 (G3M678M has no TEC; `get_descriptor()`, read on every frame, asked
`get_TecTargetRange()` every time) and 4730c55 (GPCMOS02000KPA has no
temperature sensor; `get_Temperature()` retried on every poll). Both were fixed
for their one call each; the same shape stayed open for every other call
(`get_descriptor()` re-queried the gain and exposure ranges per frame, and a
flag-gated option the SDK still refused warned on every use).
"""

from __future__ import annotations

import ast
import dataclasses
import logging
from pathlib import Path

import pytest
from astrotool_core.camera import touptek_adapter
from astrotool_core.camera.capabilities import CameraCapabilities, ConversionGain
from astrotool_core.camera.touptek_adapter import TouptekCameraAdapter
from astrotool_core.testing import (
    CAMERA_MODELS,
    SimulatedToupcam,
    SimulatedToupcamSdk,
    install_simulated_toupcam,
)
from astrotool_core.testing.sim_touptek import (
    E_FAIL,
    E_NOTIMPL,
    FLAG_CGHDR,
    SDK_CONSTANTS,
    CameraModel,
)
from astrotool_core.timing import FakeClock

#: A synthetic model with the HDR conversion-gain flag (none of the rig's cameras has it).
_SYNTH_CGHDR = dataclasses.replace(
    CAMERA_MODELS["SYNTH-MONO-USB2"],
    name="SYNTH-CGHDR",
    flag=CAMERA_MODELS["SYNTH-MONO-USB2"].flag | FLAG_CGHDR,
)
_MODELS: dict[str, CameraModel] = {**CAMERA_MODELS, _SYNTH_CGHDR.name: _SYNTH_CGHDR}

_OPTION_TEC = f"put_Option(0x{SDK_CONSTANTS['TOUPCAM_OPTION_TEC']:02X})"
_GET_TEC = f"get_Option(0x{SDK_CONSTANTS['TOUPCAM_OPTION_TEC']:02X})"
_GET_TECTARGET = f"get_Option(0x{SDK_CONSTANTS['TOUPCAM_OPTION_TECTARGET']:02X})"
_PUT_TECTARGET = f"put_Option(0x{SDK_CONSTANTS['TOUPCAM_OPTION_TECTARGET']:02X})"
_RANGE_QUERIES = ("get_ExpoAGainRange", "get_ExpTimeRange", "get_TecTargetRange")


def _connected(
    monkeypatch: pytest.MonkeyPatch,
    model: str,
    *,
    fail_calls: dict[str, int] | None = None,
) -> tuple[TouptekCameraAdapter, SimulatedToupcam]:
    sdk = SimulatedToupcamSdk(clock=FakeClock())
    sdk.fail_calls.update(fail_calls or {})
    device = sdk.add_camera(_MODELS[model])
    install_simulated_toupcam(monkeypatch, sdk)
    adapter = TouptekCameraAdapter(camera_id=device.id)
    adapter.connect()
    return adapter, sdk.handles[-1]


def _sdk_warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.levelno >= logging.WARNING and r.name == touptek_adapter.__name__
    ]


def _a_session_of_ui_reads(adapter: TouptekCameraAdapter, frames: int = 25) -> None:
    """What CameraPanel does while streaming: a descriptor per frame
    (`_apply_auto_exposure`) and the cooling/temperature/conversion-gain reads
    on its poll timer."""
    for _ in range(frames):
        adapter.get_descriptor()
        adapter.get_temperature()
        adapter.get_cooling_enabled()
        adapter.get_target_temperature()
        adapter.get_conversion_gain()
        adapter.get_black_level()


# -- the table, per simulated model ------------------------------------------------------


#: model -> (cooling, hcg, hdr, black level, temperature sensor, raw16, TEC target range)
_EXPECTED: dict[str, tuple[bool, bool, bool, bool, bool, bool, tuple[float, float] | None]] = {
    # Main camera: mono, no TEC, but a temperature sensor (C03, 531a952).
    "G3M678M": (False, False, False, True, True, True, None),
    # Guide camera: no TEC, no temperature sensor at all (4730c55), no CG/black level.
    "GPCMOS02000KPA": (False, False, False, False, False, False, None),
    # Cooled mono with conversion gain (30bd33d); simulator TEC range -50..40 C.
    "ATR585M": (True, True, False, True, True, True, (-50.0, 40.0)),
    "SYNTH-COLOR-USB3": (False, False, False, False, True, False, None),
    "SYNTH-MONO-USB2": (False, False, False, False, True, False, None),
    # HDR flag: HCG, LCG and HDR (toupcam.py:55).
    "SYNTH-CGHDR": (False, True, True, False, True, False, None),
}


class TestCapabilityTablePerModel:
    @pytest.mark.parametrize("model", sorted(_EXPECTED))
    def test_the_descriptor_carries_one_decision_per_feature(
        self, monkeypatch: pytest.MonkeyPatch, model: str
    ) -> None:
        adapter, cam = _connected(monkeypatch, model)
        caps = adapter.get_descriptor().capabilities
        cooling, hcg, hdr, black, temperature, raw16, target = _EXPECTED[model]
        assert (
            caps.supports_cooling,
            caps.supports_hcg,
            caps.supports_hdr,
            caps.supports_black_level,
            caps.supports_temperature,
            caps.supports_raw16,
        ) == (cooling, hcg, hdr, black, temperature, raw16)
        assert caps.supports_lcg is True  # LCG is the SDK's default conversion gain
        target_range = (
            None
            if caps.min_target_temp_c is None
            else (caps.min_target_temp_c, caps.max_target_temp_c)
        )
        assert target_range == target
        spec = _MODELS[model]
        assert (caps.min_gain, caps.max_gain) == spec.gain_range[:2]
        assert (caps.min_exposure_ms, caps.max_exposure_ms) == (
            spec.exposure_range_us[0] / 1000.0,
            spec.exposure_range_us[1] / 1000.0,
        )
        # The temperature sensor is decided by one probe at connect; a camera without
        # one answered E_NOTIMPL exactly once, nothing else was ever refused.
        assert cam.notimpl_calls == ([] if temperature else ["get_Temperature"])

    def test_the_capabilities_type_defaults_the_new_decisions_to_unsupported(self) -> None:
        """Other CameraPort implementations (fake, replay) construct CameraCapabilities
        without the S6.5 fields and must keep working."""
        caps = CameraCapabilities(
            min_gain=0,
            max_gain=1,
            min_exposure_ms=1.0,
            max_exposure_ms=2.0,
            supports_cooling=False,
            supports_hcg=False,
            supports_lcg=True,
            supports_hdr=False,
            supports_black_level=False,
            bit_depth=16,
            pixel_size_um=0.0,
            sensor_width_px=1,
            sensor_height_px=1,
        )
        assert (caps.supports_temperature, caps.supports_raw16) == (False, False)

    def test_a_reconnect_decides_again(self, monkeypatch: pytest.MonkeyPatch) -> None:
        adapter, cam = _connected(monkeypatch, "GPCMOS02000KPA")
        adapter.disconnect()
        assert adapter.get_descriptor().capabilities.supports_temperature is False
        adapter.connect()
        assert cam.queries.count("get_Temperature") == 1
        assert touptek_adapter._enum_devices_cache is not None


# -- no repeated SDK calls, no log spam ----------------------------------------------------


class TestNoRepeatedQueriesAndNoSpam:
    @pytest.mark.parametrize("model", sorted(_EXPECTED))
    def test_a_streaming_session_asks_each_range_once_per_connect(
        self, monkeypatch: pytest.MonkeyPatch, model: str
    ) -> None:
        """C01: `get_descriptor()` is read on every delivered frame
        (camera_panel `_apply_auto_exposure`); it re-queried the gain and exposure
        ranges each time."""
        adapter, cam = _connected(monkeypatch, model)
        _a_session_of_ui_reads(adapter)
        counts = {name: cam.queries.count(name) for name in _RANGE_QUERIES}
        has_tec = _EXPECTED[model][0]
        assert counts == {
            "get_ExpoAGainRange": 1,
            "get_ExpTimeRange": 1,
            "get_TecTargetRange": 1 if has_tec else 0,
        }

    @pytest.mark.parametrize("model", sorted(_EXPECTED))
    def test_a_streaming_session_never_repeats_a_refused_call_or_warns(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, model: str
    ) -> None:
        caplog.set_level(logging.WARNING, logger=touptek_adapter.__name__)
        adapter, cam = _connected(monkeypatch, model)
        _a_session_of_ui_reads(adapter)
        adapter.set_cooling_enabled(True)
        adapter.set_target_temperature(-5.0)
        adapter.set_conversion_gain(ConversionGain.HCG)
        adapter.set_black_level(3)
        assert len(cam.notimpl_calls) <= 1  # at most the connect-time temperature probe
        assert _sdk_warnings(caplog) == []

    @pytest.mark.parametrize("query", ["get_ExpoAGainRange", "get_ExpTimeRange"])
    def test_a_persistently_failing_range_query_warns_once_not_per_frame(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, query: str
    ) -> None:
        """The 531a952 spam shape for a non-capability failure: before S6.5 a range
        query that kept failing went through `_try()` on every frame."""
        caplog.set_level(logging.WARNING, logger=touptek_adapter.__name__)
        adapter, cam = _connected(monkeypatch, "G3M678M", fail_calls={query: E_FAIL})
        _a_session_of_ui_reads(adapter)
        assert cam.queries.count(query) == 1
        assert len([w for w in _sdk_warnings(caplog) if "SDK call failed" in w]) == 1
        caps = adapter.get_descriptor().capabilities
        if query == "get_ExpoAGainRange":  # unknown range: the adapter's documented default
            assert (caps.min_gain, caps.max_gain) == (100, 100)
        else:
            assert (caps.min_exposure_ms, caps.max_exposure_ms) == (2000.0, 2000.0)

    def test_a_tec_flag_the_sdk_refuses_is_reported_once_and_then_treated_as_unsupported(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Flag/SDK disagreement: the flag word claims a TEC, the SDK answers
        E_NOTIMPL. Before S6.5 every cooling call site re-asked and warned (the
        531a952 spam, through the flag gate); now the first E_NOTIMPL decides."""
        caplog.set_level(logging.INFO, logger=touptek_adapter.__name__)
        refused = {
            name: E_NOTIMPL for name in (_OPTION_TEC, _GET_TEC, _GET_TECTARGET, _PUT_TECTARGET)
        }
        adapter, cam = _connected(monkeypatch, "ATR585M", fail_calls=refused)
        _a_session_of_ui_reads(adapter)
        adapter.set_cooling_enabled(True)
        adapter.set_target_temperature(-5.0)
        adapter.disconnect()
        # The connect-time TEC-off, and -- safety (review C1) -- one more TEC-off at
        # disconnect: a refused on/off option never silently skips the final TEC-off.
        assert cam.notimpl_calls == [_OPTION_TEC, _OPTION_TEC]
        reports = [r.getMessage() for r in caplog.records if "not implemented" in r.getMessage()]
        assert len(reports) == 1 and "cooling" in reports[0]
        assert _sdk_warnings(caplog) == []

    def test_cooling_refused_by_the_sdk_is_unsupported_in_the_descriptor(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        adapter, _ = _connected(monkeypatch, "ATR585M", fail_calls={_OPTION_TEC: E_NOTIMPL})
        caps = adapter.get_descriptor().capabilities
        assert caps.supports_cooling is False
        assert adapter.get_cooling_enabled() is False
        assert adapter.get_target_temperature() == adapter._target_temperature_c

    def test_a_temperature_sensor_that_disappears_mid_session_is_reported_once(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The probe said yes; a later E_NOTIMPL still decides once (4730c55's rule,
        now the table's)."""
        caplog.set_level(logging.WARNING, logger=touptek_adapter.__name__)
        adapter, cam = _connected(monkeypatch, "G3M678M")
        assert adapter.get_temperature() == 12.5
        cam.fail_calls["get_Temperature"] = E_NOTIMPL
        for _ in range(10):
            assert adapter.get_temperature() is None
        assert cam.notimpl_calls == ["get_Temperature"]
        assert adapter.get_descriptor().capabilities.supports_temperature is False
        assert _sdk_warnings(caplog) == []

    def test_a_transient_temperature_failure_keeps_the_sensor(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Not E_NOTIMPL = not a capability answer: warned, still supported, asked
        again on the next poll (unchanged behaviour)."""
        caplog.set_level(logging.WARNING, logger=touptek_adapter.__name__)
        adapter, cam = _connected(monkeypatch, "G3M678M")
        cam.fail_calls["get_Temperature"] = E_FAIL
        assert adapter.get_temperature() is None
        del cam.fail_calls["get_Temperature"]
        assert adapter.get_temperature() == 12.5
        assert adapter.get_descriptor().capabilities.supports_temperature is True
        assert len(_sdk_warnings(caplog)) == 1


# -- supported features: behaviour unchanged (characterization) ------------------------------


class TestSupportedFeaturesStillReachTheSdk:
    def test_cooled_camera_cooling_target_and_gain_modes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        adapter, cam = _connected(monkeypatch, "ATR585M")
        tec = SDK_CONSTANTS["TOUPCAM_OPTION_TEC"]
        target = SDK_CONSTANTS["TOUPCAM_OPTION_TECTARGET"]
        assert cam.options[tec] == 0  # forced off at connect
        adapter.set_target_temperature(-12.5)
        adapter.set_cooling_enabled(True)
        assert (cam.options[tec], cam.options[target]) == (1, -125)
        assert adapter.get_cooling_enabled() is True
        assert adapter.get_target_temperature() == -12.5
        adapter.set_conversion_gain(ConversionGain.HCG)
        assert adapter.get_conversion_gain() is ConversionGain.HCG
        adapter.set_black_level(7)
        assert adapter.get_black_level() == 7
        assert adapter.get_temperature() == 12.5
        cam.temperature_c10 = -35
        assert adapter.get_temperature() == -3.5  # read live on every poll, never cached
        adapter.disconnect()
        assert cam.options[tec] == 0  # forced off at disconnect

    def test_cooling_option_reads_are_live_not_cached(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        adapter, cam = _connected(monkeypatch, "ATR585M")
        cam.options[SDK_CONSTANTS["TOUPCAM_OPTION_TEC"]] = 1  # e.g. changed by the SDK
        assert adapter.get_cooling_enabled() is True
        assert cam.queries.count(_GET_TEC) == 1


# -- structure: one table, one E_NOTIMPL rule ---------------------------------------------

_SOURCE = Path(touptek_adapter.__file__).read_text(encoding="utf-8")


class TestOneTableOneRule:
    def test_every_feature_flag_in_the_table_is_a_known_sdk_constant(self) -> None:
        names = [n for flags in touptek_adapter._FEATURE_FLAGS.values() for n in flags or ()]
        assert names and set(names) <= set(touptek_adapter._SDK_FALLBACKS)
        assert set(touptek_adapter._OPTION_FEATURE) <= set(touptek_adapter._SDK_FALLBACKS)
        assert set(touptek_adapter._OPTION_FEATURE.values()) <= set(touptek_adapter._FEATURE_FLAGS)

    def test_e_notimpl_is_compared_in_exactly_one_place(self) -> None:
        tree = ast.parse(_SOURCE)
        uses = [
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and node.value == "E_NOTIMPL"
        ]
        assert len(uses) == 2  # the _SDK_FALLBACKS entry and the one helper

    def test_no_hand_written_capability_guards_remain(self) -> None:
        """The pre-S6.5 `_supports_*` properties and the temperature-only cache are
        gone; call sites ask the table (`_supports(feature)`)."""
        for stale in (
            "_temperature_not_implemented",
            "def _supports_cooling",
            "def _supports_hcg",
            "def _supports_black_level",
        ):
            assert stale not in _SOURCE, stale


# -- review fixes: TEC-off safety (C1) and bounded range re-probe (C4) ---------------------


class TestTecOffIsNeverSuppressedByAnotherOptionsRefusal:
    """Review C1 (safety): a refused TEC *target* option must not refuse the cooler's
    on/off. Before the fix TOUPCAM_OPTION_TECTARGET belonged to "cooling", so one
    E_NOTIMPL on it skipped `set_cooling_enabled(False)` and the TEC-off in
    `disconnect()` -- the cooler stayed ON unattended. Plausible on a real model:
    TOUPCAM_FLAG_TEC = cooler present, TOUPCAM_FLAG_TEC_ONOFF = on/off + target."""

    @pytest.mark.parametrize("refused", [_GET_TECTARGET, _PUT_TECTARGET])
    def test_cooler_ends_off_after_a_refused_target_option(
        self, monkeypatch: pytest.MonkeyPatch, refused: str
    ) -> None:
        adapter, cam = _connected(monkeypatch, "ATR585M")
        cam.fail_calls[refused] = E_NOTIMPL  # after connect, as the reviewer's repro
        tec = SDK_CONSTANTS["TOUPCAM_OPTION_TEC"]
        adapter.set_cooling_enabled(True)
        assert cam.options[tec] == 1
        adapter.get_target_temperature()
        adapter.set_target_temperature(-15.0)
        adapter.set_cooling_enabled(False)
        assert cam.options[tec] == 0
        cam.options[tec] = 1  # e.g. switched on again behind our back
        adapter.disconnect()
        assert cam.options[tec] == 0

    def test_a_refused_target_falls_back_to_the_local_intention(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        adapter, cam = _connected(monkeypatch, "ATR585M")
        cam.fail_calls[_GET_TECTARGET] = E_NOTIMPL
        cam.fail_calls[_PUT_TECTARGET] = E_NOTIMPL
        adapter.set_target_temperature(-15.0)
        assert adapter.get_target_temperature() == -15.0
        assert adapter.get_target_temperature() == -15.0
        assert cam.notimpl_calls == [_PUT_TECTARGET]  # decided once, never re-asked
        assert adapter.get_descriptor().capabilities.supports_cooling is True
        assert adapter.get_cooling_enabled() is False

    def test_cooling_off_is_attempted_once_more_after_the_on_off_option_was_refused(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.INFO, logger=touptek_adapter.__name__)
        adapter, cam = _connected(monkeypatch, "ATR585M", fail_calls={_OPTION_TEC: E_NOTIMPL})
        for _ in range(5):
            adapter.set_cooling_enabled(False)
        adapter.disconnect()
        # connect-time TEC-off, one retry for "cooling off", one at disconnect
        assert cam.notimpl_calls == [_OPTION_TEC, _OPTION_TEC, _OPTION_TEC]
        assert len([r for r in caplog.records if "TEC off attempted" in r.getMessage()]) == 2


def _connected_on_clock(
    monkeypatch: pytest.MonkeyPatch, model: str, *, fail_calls: dict[str, int]
) -> tuple[TouptekCameraAdapter, SimulatedToupcam, FakeClock]:
    clock = FakeClock()
    sdk = SimulatedToupcamSdk(clock=clock)
    sdk.fail_calls.update(fail_calls)
    device = sdk.add_camera(_MODELS[model])
    install_simulated_toupcam(monkeypatch, sdk)
    adapter = TouptekCameraAdapter(camera_id=device.id, clock=clock)
    adapter.connect()
    return adapter, sdk.handles[-1], clock


class TestAFailedRangeProbeIsRetriedBoundedly:
    """Review C4: a range query that failed transiently (not E_NOTIMPL) at connect must
    not pin the fallback (exposure (2000, 2000) ms would clamp auto-exposure to 2 s) for
    the whole connection -- before S6.5 the per-frame query self-healed. It is asked
    again at most once per `_range_retry_s` on the adapter's clock, never per frame."""

    @pytest.mark.parametrize("query", ["get_ExpoAGainRange", "get_ExpTimeRange"])
    def test_a_transient_failure_heals_after_the_retry_interval(
        self, monkeypatch: pytest.MonkeyPatch, query: str
    ) -> None:
        adapter, cam, clock = _connected_on_clock(
            monkeypatch, "G3M678M", fail_calls={query: E_FAIL}
        )
        del cam.fail_calls[query]  # the SDK answers again
        interval = adapter._range_retry_s
        for _ in range(10):
            adapter.get_descriptor()
        assert cam.queries.count(query) == 1  # not per frame
        clock.advance(interval)
        caps = adapter.get_descriptor().capabilities
        assert cam.queries.count(query) == 2
        spec = _MODELS["G3M678M"]
        assert (caps.min_gain, caps.max_gain) == spec.gain_range[:2]
        assert caps.max_exposure_ms == spec.exposure_range_us[1] / 1000.0
        clock.advance(10 * interval)
        _a_session_of_ui_reads(adapter)
        assert cam.queries.count(query) == 2  # healed: asked no more

    def test_a_persistent_failure_is_retried_at_most_once_per_interval(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.WARNING, logger=touptek_adapter.__name__)
        adapter, cam, clock = _connected_on_clock(
            monkeypatch, "G3M678M", fail_calls={"get_ExpTimeRange": E_FAIL}
        )
        interval = adapter._range_retry_s
        frames, step = 100, interval / 4
        for _ in range(frames):
            adapter.get_descriptor()
            clock.advance(step)
        asked = cam.queries.count("get_ExpTimeRange")
        assert 2 <= asked <= 1 + frames * step / interval
        assert len([w for w in _sdk_warnings(caplog) if "SDK call failed" in w]) == asked
        assert cam.queries.count("get_ExpoAGainRange") == 1  # the good one is never re-asked

    def test_a_refused_range_is_never_retried(self, monkeypatch: pytest.MonkeyPatch) -> None:
        adapter, cam, clock = _connected_on_clock(
            monkeypatch, "G3M678M", fail_calls={"get_ExpTimeRange": E_NOTIMPL}
        )
        clock.advance(100 * adapter._range_retry_s)
        _a_session_of_ui_reads(adapter)
        assert cam.queries.count("get_ExpTimeRange") == 1
