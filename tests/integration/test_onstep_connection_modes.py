"""The three OnStep connection modes end to end: the production shims on OnStepAdapter's real
`OnStepIndiClient`, against OnStepAdapter's real OnStepX mock (`onstep_mock`) -- in `real` and
`real-sniffer` mode a second mock stands in for the rig's indiserver, configured through
`[indi] host/port` exactly as on the Pi. Real sockets and real (short) mount motion; no
hardware.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from astrotool_core.config import paths
from astrotool_core.mount import AxisDirection, MountAxis
from astrotool_core.onstep import (
    OnStepBackend,
    OnStepMode,
    OnStepMountParkAdapter,
    OnStepMountPulseAdapter,
    load_onstep_indi_config,
    start_onstep_backend,
)
from onstep_mock import SimConfig, SimulatedOnStepServer


@pytest.fixture
def rig() -> Iterator[SimulatedOnStepServer]:
    """The "real device": an OnStepX mock behind its own INDI port, named in `[indi]`, with the
    rig's measured `[meridian]` limits (install.md; the built-in 0.5/0.75 defaults leave
    OnStepAdapter too little time for its 120 s flip allowance and refuse to connect)."""
    with SimulatedOnStepServer(SimConfig(speed=20)) as server:
        paths.own_config_path().parent.mkdir(parents=True, exist_ok=True)
        paths.own_config_path().write_text(
            f'[indi]\nhost = "{server.host}"\nport = {server.port}\nhome_motion_enabled = true\n'
            "[meridian]\nflip_request_deg = 1.0\ntracking_stop_deg = 1.75\n",
            encoding="utf-8",
        )
        yield server


def _wait_live(backend: OnStepBackend, timeout: float = 5.0) -> None:
    """OnStepAdapter accepts status as live only after an update newer than its first read
    (onstep_mock.testing.wait_for_live_status); the mock republishes every 0.25 s."""
    client = backend.connection.client
    assert client is not None
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = client.mount.get_status()
        if status.status_live and status.coordinates_live:
            return
        time.sleep(0.05)
    raise AssertionError("no live OnStep status")


def _exercise(backend: OnStepBackend) -> None:
    """Connect the shims as the app does, unpark, make one finite axis move, park."""
    park = OnStepMountParkAdapter(backend.connection)
    pulse = OnStepMountPulseAdapter(backend.connection)
    park.connect()
    pulse.connect()
    try:
        _wait_live(backend)
        assert park.status().parked
        park.unpark()
        assert not park.status().parked
        result = pulse.move_angular(MountAxis.AXIS2, AxisDirection.POSITIVE, 3600.0)
        assert result.accepted, result.message
        park.park()
        assert park.status().parked
    finally:
        pulse.disconnect()
        park.disconnect()
        backend.close()


def test_mock_mode_drives_the_built_in_mock_and_never_the_configured_device(
    rig: SimulatedOnStepServer,
) -> None:
    backend = start_onstep_backend(OnStepMode.MOCK, load_onstep_indi_config())
    assert backend.connection.config.port != rig.port

    _exercise(backend)

    assert rig.writes == []  # the configured device was never touched


def test_real_mode_drives_the_configured_device(rig: SimulatedOnStepServer) -> None:
    backend = start_onstep_backend(OnStepMode.REAL, load_onstep_indi_config())
    assert backend.connection.config.port == rig.port

    _exercise(backend)

    written = {w.property for w in rig.writes if w.accepted}
    assert {"TELESCOPE_PARK"} <= written
    assert rig.mount.parked
    rig.assert_no_forbidden()


def test_sniffer_mode_drives_the_device_and_records_both_directions(
    rig: SimulatedOnStepServer, tmp_path: Path
) -> None:
    capture = tmp_path / "capture-{date}.jsonl"
    backend = start_onstep_backend(
        OnStepMode.REAL_SNIFFER, load_onstep_indi_config(), capture_path=capture
    )
    assert backend.connection.config.port != rig.port  # the client talks to the proxy

    _exercise(backend)

    assert rig.mount.parked
    rig.assert_no_forbidden()
    files = sorted(tmp_path.glob("capture-*.jsonl"))
    assert len(files) == 1
    records = [json.loads(line) for line in files[0].read_text(encoding="utf-8").splitlines()]
    sent = {r["property"] for r in records if r["type"] == "send"}
    received = {r["property"] for r in records if r["type"] not in ("send", "session", "capture")
                and "property" in r}
    assert "TELESCOPE_PARK" in sent  # a client write, recorded on its way to the device
    assert received  # and the device's updates on their way back
