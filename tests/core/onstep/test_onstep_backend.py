"""The OnStep connection modes (`astrotool_core.onstep.backend`): which mode is chosen, and
where each one points the ONE OnStepAdapter connection. Local services are fakes here; the
real mock and sniffer run in tests/integration/test_onstep_connection_modes.py."""

from __future__ import annotations

from pathlib import Path

import pytest
from astrotool_core.config import paths
from astrotool_core.onstep import (
    DEFAULT_ONSTEP_MODE,
    OnStepMode,
    load_onstep_indi_config,
    load_onstep_mode,
    parse_onstep_mode,
    start_onstep_backend,
)
from astrotool_core.onstep.backend import CAPTURE_FILE_NAME, load_onstep_capture_path
from collimation_tool.main import _parse_args


class _FakeService:
    def __init__(self) -> None:
        self.stops = 0

    def stop(self) -> None:
        self.stops += 1


def _write_config(text: str) -> None:
    paths.own_config_path().parent.mkdir(parents=True, exist_ok=True)
    paths.own_config_path().write_text(text, encoding="utf-8")


class TestChoosingTheMode:
    def test_the_default_is_the_real_device_with_the_sniffer(self) -> None:
        assert DEFAULT_ONSTEP_MODE is OnStepMode.REAL_SNIFFER
        assert load_onstep_mode() is OnStepMode.REAL_SNIFFER  # no config file at all

    @pytest.mark.parametrize(
        ("text", "mode"),
        [
            ("mock", OnStepMode.MOCK),
            ("real", OnStepMode.REAL),
            ("real-sniffer", OnStepMode.REAL_SNIFFER),
            ("real_sniffer", OnStepMode.REAL_SNIFFER),
            (" Sniffer ", OnStepMode.REAL_SNIFFER),
            ("MOCK", OnStepMode.MOCK),
        ],
    )
    def test_spellings(self, text: str, mode: OnStepMode) -> None:
        assert parse_onstep_mode(text) is mode

    def test_an_unknown_mode_is_a_value_error_naming_the_choices(self) -> None:
        with pytest.raises(ValueError, match="mock, real, real-sniffer"):
            parse_onstep_mode("simulator")

    def test_the_config_file_chooses(self) -> None:
        _write_config('[onstep]\nmode = "mock"\n')
        assert load_onstep_mode() is OnStepMode.MOCK

    @pytest.mark.parametrize("bad", ['mode = "fake"', "mode = 3", "[onstep", 'other = "x"'])
    def test_a_bad_config_value_falls_back_to_the_default(self, bad: str) -> None:
        _write_config(f"[onstep]\n{bad}\n")
        assert load_onstep_mode() is DEFAULT_ONSTEP_MODE

    def test_the_command_line_overrides_and_qt_keeps_its_own_arguments(self) -> None:
        args, rest = _parse_args(["collimation-tool", "--onstep", "mock", "-platform", "offscreen"])
        assert args.onstep is OnStepMode.MOCK
        assert rest == ["collimation-tool", "-platform", "offscreen"]

    def test_no_command_line_mode_leaves_the_choice_to_the_config(self) -> None:
        args, rest = _parse_args(["collimation-tool"])
        assert args.onstep is None
        assert rest == ["collimation-tool"]

    def test_an_unknown_command_line_mode_exits_with_usage(self) -> None:
        with pytest.raises(SystemExit):
            _parse_args(["collimation-tool", "--onstep", "simulator"])


class TestCapturePath:
    def test_default_is_a_daily_file_in_the_app_directory(self) -> None:
        assert load_onstep_capture_path() == paths.onstep_capture_dir() / CAPTURE_FILE_NAME
        assert "{date}" in CAPTURE_FILE_NAME

    def test_configured_path_expands_the_home_directory(self) -> None:
        _write_config('[onstep]\ncapture_path = "~/caps/onstep-{date}.jsonl"\n')
        assert load_onstep_capture_path() == Path.home() / "caps" / "onstep-{date}.jsonl"


class TestWhereEachModeConnects:
    def test_real_uses_the_configured_indiserver_and_starts_nothing(self) -> None:
        config = load_onstep_indi_config()

        def never(*_args: object) -> tuple[_FakeService, str, int]:
            raise AssertionError("real mode starts no local service")

        backend = start_onstep_backend(
            OnStepMode.REAL, config, start_mock=never, start_sniffer=never
        )

        assert backend.connection.config == config
        assert backend.upstream == (config.host, config.port)
        assert backend.capture_path is None
        assert backend.description == "OnStep: real device"
        backend.close()

    def test_mock_points_the_connection_at_the_mock_only(self) -> None:
        config = load_onstep_indi_config()
        service = _FakeService()
        seen: list[object] = []

        def start_mock(cfg: object) -> tuple[_FakeService, str, int]:
            seen.append(cfg)
            return service, "127.0.0.1", 40001

        backend = start_onstep_backend(OnStepMode.MOCK, config, start_mock=start_mock)

        assert seen == [config]
        assert (backend.connection.config.host, backend.connection.config.port) == (
            "127.0.0.1",
            40001,
        )
        # Everything but the endpoint is the user's configuration.
        assert backend.connection.config.device == config.device
        assert backend.connection.config.flip_request_deg == config.flip_request_deg
        assert backend.description == "OnStep: Mock"
        backend.close()
        backend.close()
        assert service.stops == 1

    def test_sniffer_sits_between_the_connection_and_the_configured_indiserver(
        self, tmp_path: Path
    ) -> None:
        config = load_onstep_indi_config()
        service = _FakeService()
        seen: list[tuple[object, Path]] = []

        def start_sniffer(cfg: object, target: Path) -> tuple[_FakeService, str, int]:
            seen.append((cfg, target))
            return service, "127.0.0.1", 40002

        target = tmp_path / "cap-{date}.jsonl"
        backend = start_onstep_backend(
            OnStepMode.REAL_SNIFFER, config, capture_path=target, start_sniffer=start_sniffer
        )

        assert seen == [(config, target)]  # the proxy forwards to the configured indiserver
        assert backend.connection.config.port == 40002
        assert backend.upstream == (config.host, config.port)
        assert backend.capture_path == target
        assert "sniffer" in backend.description
        backend.close()
        assert service.stops == 1

    def test_sniffer_records_into_the_configured_capture_path_by_default(self) -> None:
        _write_config('[onstep]\ncapture_path = "/tmp/never-written-{date}.jsonl"\n')
        targets: list[Path] = []

        def start_sniffer(_cfg: object, target: Path) -> tuple[_FakeService, str, int]:
            targets.append(target)
            return _FakeService(), "127.0.0.1", 40003

        start_onstep_backend(
            OnStepMode.REAL_SNIFFER, load_onstep_indi_config(), start_sniffer=start_sniffer
        ).close()
        assert targets == [Path("/tmp/never-written-{date}.jsonl")]
