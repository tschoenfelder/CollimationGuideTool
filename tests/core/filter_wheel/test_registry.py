"""Issue #41: ONE physical filter wheel, shared by every optical train that
references it -- one adapter, one connection state; a train without a wheel
gets nothing. Issue #47: the wiring itself (which train, right now) comes
from `filter_wheel.config.load_filter_wheel_wiring` (see test_config.py for
that module's own resolution-order tests) -- this file only tests what
`registry.py` builds ON TOP of a resolved wiring."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from astrotool_core.filter_wheel.fake_filter_wheel import FakeFilterWheel
from astrotool_core.filter_wheel.indi_filter_wheel_adapter import IndiFilterWheelAdapter
from astrotool_core.filter_wheel.port import FilterWheelPort
from astrotool_core.filter_wheel.registry import (
    FilterWheelConfig,
    build_filter_wheels,
    load_filter_wheel_layout,
)
from astrotool_core.testing.fake_indi_server import FakeIndiServer

_REAL_DEVICE = "ToupTek EFW 2"  # the device name on the Pi's indiserver (indi_getprop)


class _Recorder:
    """A factory that records every adapter it builds."""

    def __init__(self) -> None:
        self.built: list[FilterWheelConfig] = []

    def __call__(self, config: FilterWheelConfig) -> FilterWheelPort:
        self.built.append(config)
        return FakeFilterWheel()


def _shared(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "smarttscope.toml"
    path.write_text(text, encoding="utf-8")
    return path


class TestBuiltInDefault:
    """Neither config file present -- this rig's known-good state."""

    def test_default_is_one_wheel_wired_to_main_only(self, tmp_path: Path) -> None:
        factory = _Recorder()
        layout = load_filter_wheel_layout(
            smarttscope_path=tmp_path / "a.toml", local_path=tmp_path / "b.toml"
        )

        assignments = build_filter_wheels(layout, factory)

        assert len(assignments) == 1
        assert assignments[0].device_name == _REAL_DEVICE
        assert assignments[0].trains == ("main",)

    def test_one_adapter_is_built_not_per_train(self, tmp_path: Path) -> None:
        factory = _Recorder()
        layout = load_filter_wheel_layout(
            smarttscope_path=tmp_path / "a.toml", local_path=tmp_path / "b.toml"
        )

        build_filter_wheels(layout, factory)

        assert len(factory.built) == 1
        assert factory.built[0].filter_names == {
            1: "L",
            2: "R",
            3: "G",
            4: "B",
            5: "H",
            6: "O",
            7: "S",
        }


class TestSharedConfigDrivesTheLayout:
    def test_the_shared_configs_active_train_is_used(self, tmp_path: Path) -> None:
        shared = _shared(
            tmp_path,
            '[filter_wheel]\nenabled = true\nactive_camera_role = "guide"\n[filters]\nred = 1\n',
        )
        layout = load_filter_wheel_layout(
            smarttscope_path=shared, local_path=tmp_path / "none.toml"
        )
        assignments = build_filter_wheels(layout, _Recorder())
        assert [a.trains for a in assignments] == [("guide",)]

    def test_disabled_produces_an_empty_layout_no_wheel_built(self, tmp_path: Path) -> None:
        shared = _shared(tmp_path, '[filter_wheel]\nenabled = false\nactive_camera_role = "main"\n')
        factory = _Recorder()
        layout = load_filter_wheel_layout(
            smarttscope_path=shared, local_path=tmp_path / "none.toml"
        )

        assignments = build_filter_wheels(layout, factory)

        assert assignments == []
        assert factory.built == []

    def test_local_file_supplies_only_the_indi_identity(self, tmp_path: Path) -> None:
        shared = _shared(tmp_path, '[filter_wheel]\nenabled = true\nactive_camera_role = "main"\n')
        local = tmp_path / "local.toml"
        local.write_text(
            '[filter_wheel]\ndevice = "ToupTek EFW 2"\nhost = "rasppi3"\nport = 7625\n',
            encoding="utf-8",
        )
        layout = load_filter_wheel_layout(smarttscope_path=shared, local_path=local)
        assert layout.wheels[0].host == "rasppi3"
        assert layout.wheels[0].port == 7625


@pytest.fixture
def server() -> Iterator[FakeIndiServer]:
    fake = FakeIndiServer(
        device_name=_REAL_DEVICE,
        filter_slot=3,
        filter_names=("Luminance", "Red", "OIII", "Ha"),
    )
    fake.start()
    try:
        yield fake
    finally:
        fake.stop()


class TestDefaultFactoryTargetsTheRealDevice:
    def test_the_default_factory_builds_an_indi_adapter_for_the_exact_device(
        self, server: FakeIndiServer, tmp_path: Path
    ) -> None:
        layout = load_filter_wheel_layout(
            smarttscope_path=tmp_path / "a.toml", local_path=tmp_path / "b.toml"
        )
        wheels = build_filter_wheels(layout)  # default factory
        assert isinstance(wheels[0].port, IndiFilterWheelAdapter)

        # Pointed at the fake server under the REAL device name, it connects.
        adapter = IndiFilterWheelAdapter(
            server.host, server.port, wheels[0].device_name, connect_timeout_s=2.0
        )
        try:
            adapter.connect()
            status = adapter.status()
            assert status.current_slot == 3 and status.filter_name == "OIII"
        finally:
            adapter.disconnect()

    def test_the_wrong_device_name_fails_explicitly_not_silently(
        self, server: FakeIndiServer
    ) -> None:
        adapter = IndiFilterWheelAdapter(
            server.host, server.port, "Filter Wheel", connect_timeout_s=1.0
        )
        try:
            with pytest.raises(ConnectionError):
                adapter.connect()
        finally:
            adapter.disconnect()

    def test_reconnecting_is_deterministic(self, server: FakeIndiServer) -> None:
        adapter = IndiFilterWheelAdapter(
            server.host, server.port, _REAL_DEVICE, connect_timeout_s=2.0
        )
        try:
            adapter.connect()
            adapter.disconnect()
            adapter.connect()
            assert adapter.status().current_slot == 3
        finally:
            adapter.disconnect()
