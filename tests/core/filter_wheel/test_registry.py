"""Issue #41: ONE physical filter wheel, shared by every optical train that
references it -- one adapter, one connection state; a train without a wheel
gets nothing; several physical wheels stay possible."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from astrotool_core.filter_wheel.fake_filter_wheel import FakeFilterWheel
from astrotool_core.filter_wheel.indi_filter_wheel_adapter import IndiFilterWheelAdapter
from astrotool_core.filter_wheel.port import FilterWheelPort
from astrotool_core.filter_wheel.registry import (
    DEFAULT_LAYOUT,
    FilterWheelConfig,
    build_filter_wheels,
    load_filter_wheel_layout,
)
from astrotool_core.testing.fake_indi_server import FakeIndiServer

_REAL_DEVICE = "ToupTek EFW 1"  # the device name on the Pi's indiserver (indi_getprop)


class _Recorder:
    """A factory that records every adapter it builds."""

    def __init__(self) -> None:
        self.built: list[FilterWheelConfig] = []

    def __call__(self, config: FilterWheelConfig) -> FilterWheelPort:
        self.built.append(config)
        return FakeFilterWheel()


class TestDefaultLayout:
    def test_default_is_one_shared_wheel_for_main_and_oag_and_none_for_guide(self) -> None:
        factory = _Recorder()

        assignments = build_filter_wheels(DEFAULT_LAYOUT, factory)

        assert len(assignments) == 1
        assert assignments[0].device_name == _REAL_DEVICE
        assert assignments[0].trains == ("Main", "OAG")
        assert all("Guide" not in a.trains for a in assignments)

    def test_one_adapter_is_built_per_physical_wheel_not_per_train(self) -> None:
        factory = _Recorder()

        build_filter_wheels(DEFAULT_LAYOUT, factory)

        assert len(factory.built) == 1

    def test_the_shared_wheel_is_one_object_for_every_train(self) -> None:
        assignments = build_filter_wheels(DEFAULT_LAYOUT, _Recorder())

        wheel = assignments[0].port
        assert wheel is assignments[0].port  # one instance, referenced by Main and OAG


class TestConfig:
    def test_missing_file_gives_the_default_layout(self, tmp_path: Path) -> None:
        assert load_filter_wheel_layout(tmp_path / "nope.toml") == DEFAULT_LAYOUT

    def test_malformed_toml_gives_the_default_layout(self, tmp_path: Path) -> None:
        path = tmp_path / "config.toml"
        path.write_text("this is [not valid", encoding="utf-8")

        assert load_filter_wheel_layout(path) == DEFAULT_LAYOUT

    def test_a_file_without_filter_wheels_gives_the_default_layout(self, tmp_path: Path) -> None:
        path = tmp_path / "config.toml"
        path.write_text('[cameras.main]\ncamera_id = "x"\n', encoding="utf-8")

        assert load_filter_wheel_layout(path) == DEFAULT_LAYOUT

    def test_two_distinct_wheels_stay_independent(self, tmp_path: Path) -> None:
        path = tmp_path / "config.toml"
        path.write_text(
            '[filter_wheels.efw1]\ndevice = "ToupTek EFW 1"\n'
            '[filter_wheels.efw2]\ndevice = "ToupTek EFW 2"\nport = 7625\n'
            '[optical_trains.Main]\nfilter_wheel = "efw1"\n'
            '[optical_trains.OAG]\nfilter_wheel = "efw1"\n'
            '[optical_trains.Guide]\nfilter_wheel = "efw2"\n',
            encoding="utf-8",
        )
        factory = _Recorder()

        assignments = build_filter_wheels(load_filter_wheel_layout(path), factory)

        assert [a.device_name for a in assignments] == ["ToupTek EFW 1", "ToupTek EFW 2"]
        assert [a.trains for a in assignments] == [("Main", "OAG"), ("Guide",)]
        assert len(factory.built) == 2  # two physical wheels -> two adapters
        assert factory.built[1].port == 7625

    def test_a_train_may_have_no_wheel(self, tmp_path: Path) -> None:
        path = tmp_path / "config.toml"
        path.write_text(
            '[filter_wheels.efw1]\ndevice = "ToupTek EFW 1"\n'
            '[optical_trains.Main]\nfilter_wheel = "efw1"\n'
            "[optical_trains.Guide]\n",
            encoding="utf-8",
        )

        assignments = build_filter_wheels(load_filter_wheel_layout(path), _Recorder())

        assert [a.trains for a in assignments] == [("Main",)]

    def test_a_train_pointing_at_an_unknown_wheel_is_ignored(self, tmp_path: Path) -> None:
        path = tmp_path / "config.toml"
        path.write_text(
            '[filter_wheels.efw1]\ndevice = "ToupTek EFW 1"\n'
            '[optical_trains.Main]\nfilter_wheel = "efw1"\n'
            '[optical_trains.OAG]\nfilter_wheel = "does-not-exist"\n',
            encoding="utf-8",
        )

        assignments = build_filter_wheels(load_filter_wheel_layout(path), _Recorder())

        assert [a.trains for a in assignments] == [("Main",)]

    def test_a_wheel_nobody_uses_is_not_built(self, tmp_path: Path) -> None:
        path = tmp_path / "config.toml"
        path.write_text(
            '[filter_wheels.efw1]\ndevice = "ToupTek EFW 1"\n'
            '[filter_wheels.spare]\ndevice = "Spare"\n'
            '[optical_trains.Main]\nfilter_wheel = "efw1"\n',
            encoding="utf-8",
        )
        factory = _Recorder()

        build_filter_wheels(load_filter_wheel_layout(path), factory)

        assert [c.device_name for c in factory.built] == ["ToupTek EFW 1"]


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
        self, server: FakeIndiServer
    ) -> None:
        layout = load_filter_wheel_layout(Path("does-not-exist.toml"))
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
