"""#52 K01-K03 (S6.2): `DeviceConnectionService` and its helpers, directly -- no Qt, no
MainWindow. Every lifecycle phase fails once: the failure is returned (never raised), logged
exactly once in the one format, the device is released, and the next attempt works."""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable

import pytest
from astrotool_core.onstep import OnStepFocuserAdapter
from astrotool_core.session import (
    ConnectionPhase,
    DeviceConnectionService,
    describe_failure,
    stop_each,
    switch_device,
)
from astrotool_core.testing import ConnectFailure, OnStepScenario, make_simulated_onstep_connection
from astrotool_core.timing import FakeClock

_LOGGER = "astrotool_core.session.device_connection"


class _Device:
    """Records every lifecycle call; `fail` maps a call name to the error it raises once."""

    def __init__(self, name: str = "dev", log: list[str] | None = None) -> None:
        self.name = name
        self.log = log if log is not None else []
        self.fail: dict[str, BaseException] = {}
        self.open = False

    def _step(self, call: str) -> None:
        self.log.append(f"{self.name}.{call}")
        error = self.fail.pop(call, None)
        if error is not None:
            raise error

    def connect(self) -> None:
        self._step("connect")
        self.open = True

    def disconnect(self) -> None:
        self.open = False  # released even when the call then reports a failure
        self._step("disconnect")

    def start(self) -> None:
        self._step("start")

    def status(self) -> None:
        self._step("status")

    def stop(self) -> None:
        self._step("stop")


def _service(device: _Device) -> DeviceConnectionService:
    return DeviceConnectionService(
        "Focuser", device, start=device.start, wait=device.status, cancel=device.stop
    )


def _records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == _LOGGER]


class TestOpeningPhases:
    @pytest.mark.parametrize(
        ("call", "phase"),
        [
            ("connect", ConnectionPhase.CONNECT),
            ("start", ConnectionPhase.START),
            ("status", ConnectionPhase.WAIT),
        ],
    )
    def test_a_failure_is_returned_logged_once_released_and_retryable(
        self, caplog: pytest.LogCaptureFixture, call: str, phase: ConnectionPhase
    ) -> None:
        device = _Device()
        service = _service(device)
        device.fail[call] = TimeoutError("no answer")

        with caplog.at_level("WARNING", logger=_LOGGER):
            failure = service.connect()

        assert failure is not None and failure.phase is phase
        assert failure.error_type == "TimeoutError" and failure.message == "no answer"
        assert not service.connected and service.last_connect_error == "no answer"
        assert service.last_failure == failure
        assert device.log[-1] == "dev.disconnect" and not device.open  # nothing half-open
        records = _records(caplog)
        assert [r.getMessage() for r in records] == [
            f"Focuser: {phase.value} failed (TimeoutError): no answer"
        ]
        assert records[0].levelno == logging.WARNING

        assert service.connect() is None  # retry
        assert service.connected and service.last_connect_error is None
        assert device.log[-3:] == ["dev.connect", "dev.start", "dev.status"]

    def test_phases_run_in_order_and_a_success_logs_nothing(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        device = _Device()
        with caplog.at_level("DEBUG", logger=_LOGGER):
            assert _service(device).connect() is None
        assert device.log == ["dev.connect", "dev.start", "dev.status"]
        assert _records(caplog) == []

    def test_connecting_twice_does_not_reopen(self) -> None:
        device = _Device()
        service = _service(device)
        service.connect()
        service.connect()
        assert device.log.count("dev.connect") == 1

    def test_a_failing_cleanup_after_a_failed_connect_is_reported_too(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        device = _Device()
        device.fail = {"connect": ConnectionError("refused"), "disconnect": OSError("gone")}
        with caplog.at_level("WARNING", logger=_LOGGER):
            failure = _service(device).connect()
        assert failure is not None and failure.phase is ConnectionPhase.CONNECT
        assert [r.getMessage() for r in _records(caplog)] == [
            "Focuser: connect failed (ConnectionError): refused",
            "Focuser: disconnect failed (OSError): gone",
        ]

    def test_keyboard_interrupt_is_never_swallowed(self) -> None:
        device = _Device()
        device.fail["connect"] = KeyboardInterrupt()
        with pytest.raises(KeyboardInterrupt):
            _service(device).connect()


class TestClosingPhases:
    def test_a_failing_cancel_never_skips_the_release(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        device = _Device()
        service = _service(device)
        service.connect()
        device.fail["stop"] = RuntimeError("OnStep still reports motion after stop")

        with caplog.at_level("WARNING", logger=_LOGGER):
            failures = service.disconnect(cancel_motion=True)

        assert [f.phase for f in failures] == [ConnectionPhase.CANCEL]
        assert device.log[-2:] == ["dev.stop", "dev.disconnect"]
        assert not service.connected and service.last_failure == failures[0]
        assert len(_records(caplog)) == 1
        assert service.connect() is None  # retryable

    def test_a_failing_release_still_leaves_the_service_disconnected(self) -> None:
        device = _Device()
        service = _service(device)
        service.connect()
        device.fail["disconnect"] = OSError("socket already gone")

        failures = service.disconnect()

        assert [f.phase for f in failures] == [ConnectionPhase.DISCONNECT]
        assert failures[0].status_text == "Disconnect failed — socket already gone"
        assert not service.connected
        assert "dev.stop" not in device.log  # a plain disconnect does not cancel motion
        assert service.connect() is None

    def test_disconnect_when_not_connected_does_nothing(self) -> None:
        device = _Device()
        assert _service(device).disconnect(cancel_motion=True) == []
        assert device.log == []

    def test_diagnostic_context(self) -> None:
        device = _Device()
        service = _service(device)
        device.fail["connect"] = KeyError("UTC")
        service.connect()
        assert service.diagnostic_context() == {
            "connected": False,
            "last_connect_error": "KeyError: 'UTC'",
            "last_connection_failure": {
                "device": "Focuser",
                "phase": "connect",
                "error_type": "KeyError",
                "message": "KeyError: 'UTC'",
                "internal": False,
            },
        }


class TestNormalizedFailure:
    @pytest.mark.parametrize(
        ("error", "expected"),
        [
            (TimeoutError("INDI property did not update"), "INDI property did not update"),
            (KeyError("UTC"), "KeyError: 'UTC'"),
            (RuntimeError(), "RuntimeError"),
            (OSError(-2147024891), "-2147024891"),
        ],
    )
    def test_describe_failure(self, error: BaseException, expected: str) -> None:
        assert describe_failure(error) == expected

    @pytest.mark.parametrize(
        ("call", "prefix"),
        [
            ("connect", "Connect failed — "),
            ("start", "Connect failed (setup) — "),
            ("status", "Connect failed (no status) — "),
        ],
    )
    def test_status_text(self, call: str, prefix: str) -> None:
        device = _Device()
        device.fail[call] = ValueError("bad reply")
        failure = _service(device).connect()
        assert failure is not None and failure.status_text == f"{prefix}bad reply"


class TestSwitchDevice:
    def test_the_previous_device_is_released_before_the_new_one_opens(self) -> None:
        log: list[str] = []
        previous, candidate = _Device("old", log), _Device("new", log)

        result = switch_device("Camera", previous, lambda: candidate)

        assert result.device is candidate and result.failure is None
        assert log == ["old.disconnect", "new.connect"]

    def test_a_failed_switch_releases_the_candidate_and_restores_the_previous(self) -> None:
        log: list[str] = []
        previous, candidate = _Device("old", log), _Device("new", log)
        candidate.fail["connect"] = TimeoutError("enumeration timed out")

        result = switch_device("Camera", previous, lambda: candidate)

        assert result.device is None and result.failure is not None
        assert result.failure.message == "enumeration timed out"
        assert log == ["old.disconnect", "new.connect", "new.disconnect", "old.connect"]
        assert previous.open and result.restore_failure is None

    def test_a_failed_identity_check_is_a_failed_switch(self) -> None:
        log: list[str] = []
        previous, candidate = _Device("old", log), _Device("new", log)

        def verify(_device: _Device) -> None:
            raise ConnectionError("opened 'b' instead of the requested 'a'")

        result = switch_device("Camera", previous, lambda: candidate, verify=verify)

        assert result.device is None and result.failure is not None
        assert result.failure.phase is ConnectionPhase.WAIT
        assert not candidate.open and previous.open

    def test_a_raising_factory_and_a_failed_restore_are_both_reported(self) -> None:
        previous = _Device("old")
        previous.fail["connect"] = ConnectionError("busy")

        def factory() -> _Device:
            raise RuntimeError("SDK not loaded")

        result = switch_device("Camera", previous, factory)

        assert result.device is None
        assert result.failure is not None and result.failure.message == "SDK not loaded"
        assert result.restore_failure is not None and result.restore_failure.message == "busy"

    def test_no_restore_when_not_requested(self) -> None:
        log: list[str] = []
        previous, candidate = _Device("demo", log), _Device("new", log)
        candidate.fail["connect"] = ConnectionError("refused")
        switch_device("Camera", previous, lambda: candidate, restore_previous=False)
        assert "demo.connect" not in log


class TestSwitchRestoreIsAllOrNothing:
    """S6.2 review: the restore after a failed switch re-opens the previous device through the
    same `open_device` path, with its own identity check, and releases a partial restore."""

    def test_a_restore_failing_its_identity_check_is_released_and_reported(self) -> None:
        log: list[str] = []
        previous, candidate = _Device("old", log), _Device("new", log)
        candidate.fail["connect"] = ConnectionError("refused")

        def verify_previous(_device: _Device) -> None:
            raise ConnectionError("opened 'x' instead of the requested 'old'")

        result = switch_device(
            "Camera", previous, lambda: candidate, verify_previous=verify_previous
        )

        assert result.restore_failure is not None
        assert result.restore_failure.phase is ConnectionPhase.WAIT
        assert log[-2:] == ["old.connect", "old.disconnect"]
        assert not previous.open  # nothing half-open: the caller falls back to its default

    def test_a_good_restore_runs_the_previous_identity_check(self) -> None:
        previous, candidate = _Device("old"), _Device("new")
        candidate.fail["connect"] = ConnectionError("refused")
        checked: list[_Device] = []

        result = switch_device(
            "Camera", previous, lambda: candidate, verify_previous=checked.append
        )

        assert result.restore_failure is None and checked == [previous] and previous.open


class TestInternalErrors:
    """S6.2 review: a programming error inside a lifecycle step is contained (retryable) but
    must not look like a hardware failure -- ERROR with its traceback, "Internal error" wording,
    `internal` in the diagnostics, and handed to `sys.excepthook` (the apps' #10 incident
    capture) exactly once, as it was when it escaped the handler."""

    @staticmethod
    def _hook(monkeypatch: pytest.MonkeyPatch) -> list[type[BaseException]]:
        calls: list[type[BaseException]] = []
        monkeypatch.setattr(sys, "excepthook", lambda t, _v, _tb: calls.append(t))
        return calls

    @pytest.mark.parametrize(
        "error",
        [
            AttributeError("'NoneType' object has no attribute 'Open'"),
            TypeError("connect() takes 1 positional argument but 2 were given"),
            NameError("name 'tc' is not defined"),
            UnboundLocalError("cannot access local variable 'cam'"),
            AssertionError("device is None"),
            ModuleNotFoundError("No module named 'toupcam'"),
        ],
        ids=lambda e: type(e).__name__,
    )
    @pytest.mark.parametrize("call", ["connect", "start", "status"])
    def test_an_internal_error_in_an_opening_phase(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        error: Exception,
        call: str,
    ) -> None:
        hook = self._hook(monkeypatch)
        device = _Device()
        service = _service(device)
        device.fail[call] = error

        with caplog.at_level("WARNING", logger=_LOGGER):
            failure = service.connect()

        assert failure is not None and failure.internal
        name = type(error).__name__
        assert failure.message == f"{name}: {error}"
        assert failure.status_text == f"Internal error — {name}: {error} (capture diagnostics)"
        assert failure.as_dict()["internal"] is True
        (record,) = _records(caplog)
        assert record.levelno == logging.ERROR and record.exc_info is not None
        assert record.exc_info[1] is error
        assert hook == [type(error)]
        assert not device.open and not service.connected
        assert service.connect() is None  # still retryable

    @pytest.mark.parametrize(
        "error",
        [
            KeyError("UTC"),
            OSError("[Errno 11001] getaddrinfo failed"),
            ConnectionError("refused"),
            TimeoutError("no answer"),
            RuntimeError("Safe HOME-route flip property is not ready"),
            ValueError("Invalid isoformat string: ''"),
        ],
        ids=lambda e: type(e).__name__,
    )
    def test_a_device_failure_is_not_internal_and_never_reaches_the_hook(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, error: Exception
    ) -> None:
        hook = self._hook(monkeypatch)
        device = _Device()
        device.fail["connect"] = error

        with caplog.at_level("WARNING", logger=_LOGGER):
            failure = _service(device).connect()

        assert failure is not None and not failure.internal
        assert failure.status_text.startswith("Connect failed — ")
        (record,) = _records(caplog)
        assert record.levelno == logging.WARNING and record.exc_info is not None
        assert hook == []

    def test_an_internal_error_on_release_and_at_shutdown_reaches_the_hook(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        hook = self._hook(monkeypatch)
        device = _Device()
        service = _service(device)
        service.connect()
        device.fail["stop"] = AttributeError("'NoneType' object has no attribute 'stop'")

        failures = service.disconnect(cancel_motion=True)

        assert [f.internal for f in failures] == [True]
        assert "dev.disconnect" in device.log  # the release still ran

        def broken() -> None:
            raise TypeError("stop() missing 1 required positional argument")

        assert [f.internal for f in stop_each([("panel", broken)])] == [True]
        assert hook == [AttributeError, TypeError]

    def test_a_failing_hook_does_not_break_containment(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        def bad_hook(*_args: object) -> None:
            raise RuntimeError("hook broken")

        monkeypatch.setattr(sys, "excepthook", bad_hook)
        device = _Device()
        device.fail["connect"] = AttributeError("x")

        with caplog.at_level("ERROR", logger=_LOGGER):
            failure = _service(device).connect()  # must not raise

        assert failure is not None and failure.internal and not device.open
        assert any("incident hook failed" in r.getMessage() for r in _records(caplog))


class TestStopEach:
    def test_one_raising_stop_does_not_skip_the_others(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        calls: list[str] = []

        def ok(name: str) -> Callable[[], None]:
            return lambda: calls.append(name)

        def boom() -> None:
            calls.append("mount")
            raise RuntimeError("tracking off not confirmed")

        with caplog.at_level("WARNING", logger=_LOGGER):
            failures = stop_each(
                [("camera", ok("camera")), ("mount", boom), ("wheel", ok("wheel"))]
            )

        assert calls == ["camera", "mount", "wheel"]
        assert [(f.device, f.phase) for f in failures] == [("mount", ConnectionPhase.SHUTDOWN)]
        assert len(_records(caplog)) == 1


class TestOverTheSimulatedOnStepConnection:
    """The production `OnStepConnection` + focuser shim on the #51 simulator (fake time)."""

    @pytest.mark.parametrize(
        "failure",
        [
            ConnectFailure(KeyError("UTC")),
            ConnectFailure(OSError("[Errno 11001] getaddrinfo failed"), self_closes=False),
            ConnectFailure(TimeoutError("INDI property LX200 OnStep.CONNECTION did not update")),
        ],
        ids=["KeyError", "OSError", "TimeoutError"],
    )
    def test_a_connect_failure_leaves_no_client_and_a_retry_connects(
        self, failure: ConnectFailure
    ) -> None:
        clock = FakeClock()
        connection, made = make_simulated_onstep_connection(
            OnStepScenario(connect_errors=[failure], connect_latency_s=2.0), clock=clock
        )
        focuser = OnStepFocuserAdapter(connection)
        service = DeviceConnectionService("Focuser", focuser, wait=focuser.status)

        result = service.connect()

        assert result is not None and result.message == describe_failure(failure.error)
        assert connection.client is None and made[0].close_calls == 1
        assert clock.monotonic() == 2.0  # the connect latency ran on fake time

        assert service.connect() is None
        assert connection.client is made[1] and focuser.status().available
        service.disconnect()
        assert connection.client is None
