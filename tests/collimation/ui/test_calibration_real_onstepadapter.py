"""The whole migration, end to end, against the REAL OnStepAdapter 0.3.5 (no hardware).

Mount Align panel -> runner -> `OnStepMountParkAdapter`/`OnStepMountPulseAdapter` -> the real
`OnStepClient`/`OnStepMount` -> `FakeOnStepSerial` (a scripted OnStep controller). Camera frames
are the textured scene shifted by what the simulated mount REALLY moved, so the calibration
result can only come out right if the timed bootstrap really measured the (unknown) centering
rate, that rate was installed into OnStepAdapter at runtime, and the following moves really went
through `move_ra`/`move_dec`.
"""

from __future__ import annotations

import dataclasses
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import numpy as np
import onstep_adapter.mount as mount_module
import pytest
from astrotool_core.acquisition.stable_frame_acquisition import (
    DeliveredFrame,
    FrameAcquisitionResult,
    FrameAcquisitionStatus,
)
from astrotool_core.config import MountAlignmentSettings
from astrotool_core.mount import AxisDirection, MountAxis
from astrotool_core.mount.movement_sizing import CameraGeometry
from astrotool_core.onstep import (
    OnStepConnection,
    OnStepMountParkAdapter,
    OnStepMountPulseAdapter,
    build_onstep_safety_config,
)
from astrotool_core.testing.fake_onstep_serial import FakeOnStepSerial
from collimation_tool.ui.mount_test_move_panel import MountTestMovePanel

_SITE_LAT, _SITE_LON = 50.336, 8.533
#: What the simulated controller REALLY moves at (:RC#) -- not the 8x-sidereal (120.3"/s) seed.
_TRUE_CENTER_RATE = 150.0
_SETTINGS = MountAlignmentSettings(
    settle_ms=0,
    frame_settle_ms=0,
    stability_sample_interval_s=0.0,
    stability_timeout_s=3.0,
)
_LEFT = CameraGeometry("left", 200, 120, 3.0)  # FOV 600" x 360"
_RIGHT = CameraGeometry("right", 300, 100, 12.0)  # FOV 3600" x 1200"
_SCALE = {"left": 3.0, "right": 12.0}


def _texture(seed: int, shape: tuple[int, int]) -> np.ndarray:
    rng = np.random.default_rng(seed)
    base = rng.random(shape).astype(np.float32)
    smooth = sum(np.roll(np.roll(base, i, 0), j, 1) for i in range(4) for j in range(4))
    return np.asarray(smooth / 16.0 * 1000.0 + 100.0, dtype=np.float32)


class _SimSky:
    def __init__(self, sim: FakeOnStepSerial) -> None:
        self.sim = sim
        self.base = {"left": _texture(1, (120, 200)), "right": _texture(2, (100, 300))}

    def frame(self, key: str) -> np.ndarray:
        dx = round(self.sim.moved_arcsec["ra"] / _SCALE[key])
        dy = round(self.sim.moved_arcsec["dec"] / _SCALE[key])
        return np.roll(np.roll(self.base[key], dy, axis=0), dx, axis=1)

    def getter(self, key: str) -> Callable[[], np.ndarray]:
        return lambda: self.frame(key)

    def waiter(self, key: str) -> Callable[[float, float], FrameAcquisitionResult]:
        def wait(_reference: float, _timeout: float) -> FrameAcquisitionResult:
            return FrameAcquisitionResult(
                status=FrameAcquisitionStatus.OK,
                frame=DeliveredFrame(
                    pixels=self.frame(key),
                    captured_at_monotonic=time.monotonic(),
                    exposure_seconds=0.01,
                ),
            )

        return wait


@pytest.fixture
def sim(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeOnStepSerial]:
    simulator = FakeOnStepSerial(
        site_lon_deg=_SITE_LON,
        site_lat_deg=_SITE_LAT,
        center_rate_arcsec_per_s=_TRUE_CENTER_RATE,
    )
    monkeypatch.setattr(mount_module.serial, "Serial", lambda *_a, **_k: simulator)
    yield simulator


def _connection(tmp_path: Path) -> OnStepConnection:
    site = tmp_path / "smarttscope.toml"
    site.write_text(f"[observer]\nlat = {_SITE_LAT}\nlon = {_SITE_LON}\n")
    config = dataclasses.replace(
        build_onstep_safety_config(site, tmp_path / "none.toml"),
        state_file=str(tmp_path / "state.json"),
        mechanical_calibration_file=str(tmp_path / "calibration.json"),
        horizon_path="",
        time_trust_source="ntp",
        home_park_settle_s=0.0,
    )
    return OnStepConnection("FAKE", safety_config=config)


def _run(panel: MountTestMovePanel, *, timeout_s: float = 240.0) -> None:
    panel._run_calibration_button.click()
    deadline = time.monotonic() + timeout_s
    while panel._calibration_queue or panel._pending is not None:
        while panel._runner.is_busy:
            assert time.monotonic() < deadline, "calibration never completed"
            time.sleep(0.005)
        panel._poll()
        assert time.monotonic() < deadline, "calibration never completed"
    while panel._runner.is_busy:  # a trailing return pulse
        time.sleep(0.005)
    panel._poll()


def _calibrate(qapp: object, sim: FakeOnStepSerial, tmp_path: Path, *, via_home: bool) -> None:
    connection = _connection(tmp_path)
    park = OnStepMountParkAdapter(connection)
    pulse = OnStepMountPulseAdapter(connection)
    park.connect()
    pulse.connect()
    sky = _SimSky(sim)
    panel = MountTestMovePanel(
        pulse,
        mount_park=park,
        get_left_frame=sky.getter("left"),
        get_right_frame=sky.getter("right"),
        wait_for_left_frame=sky.waiter("left"),
        wait_for_right_frame=sky.waiter("right"),
        settings=_SETTINGS,
        camera_geometry=lambda: [_LEFT, _RIGHT],
    )
    panel._terrestrial_button.click()  # texture-based (cross-correlation) measurement
    panel._connect_button.setChecked(True)
    if via_home:
        park.unpark()  # the adapter's own route: unpark, tracking off, drive to HOME
    else:
        sim.parked, sim.at_home = False, False  # unparked/jogged by other means
    park.confirm_home()  # the operator's explicit step

    try:
        _run(panel)

        events = panel._sizing_log
        paths = [e["path"] for e in events if e.get("event") == "motion"]
        assert paths, "no motion was logged"
        assert paths[0] == "timed"  # the bootstrap
        if via_home:
            # OnStepAdapter 0.3.5 keeps reporting 'at home' after the unpark-to-home route, so
            # every angular attempt is refused and runs as the equivalent timed move.
            assert "timed_fallback" in paths and "angular" not in paths
        else:
            assert "angular" in paths  # ... then OnStepAdapter's move_ra / move_dec
            assert "timed_fallback" not in paths  # a trusted clock: no workaround needed

        # The rates the real adapter now holds were MEASURED from the images.
        client = connection.client
        assert client is not None
        calibration = client.mount.get_motion_calibration()
        assert calibration is not None
        for rate in (
            calibration.center_ra_east_arcsec_per_s,
            calibration.center_ra_west_arcsec_per_s,
            calibration.center_dec_north_arcsec_per_s,
            calibration.center_dec_south_arcsec_per_s,
        ):
            assert rate is not None
            assert abs(rate - _TRUE_CENTER_RATE) / _TRUE_CENTER_RATE < 0.05, rate

        # The result is a real, measured calibration in the 25% band, and the mount was
        # returned to where it started.
        matrix = panel.calibration_for("left")
        assert matrix is not None
        response = matrix.response_for(MountAxis.AXIS1, AxisDirection.POSITIVE)
        assert 0.20 <= abs(response.dx_px) / 200 <= 0.30
        for axis in ("ra", "dec"):
            assert abs(sim.moved_arcsec[axis]) < 15.0
    finally:
        panel.stop()
        pulse.disconnect()
        park.disconnect()


def test_calibration_through_the_real_adapter_with_angular_moves(
    qapp: object, sim: FakeOnStepSerial, tmp_path: Path
) -> None:
    _calibrate(qapp, sim, tmp_path, via_home=False)


def test_calibration_after_unpark_to_home_completes_via_the_timed_fallback(
    qapp: object, sim: FakeOnStepSerial, tmp_path: Path
) -> None:
    _calibrate(qapp, sim, tmp_path, via_home=True)
