"""S6.3 (#52 K02, adapter side): one owner of "which exceptions an OnStep operation may
raise" (`ONSTEP_OPERATION_ERRORS`), used by the focuser and the pulse shims.

Before: `mount_pulse_adapter.py` restated `(ConnectionError, RuntimeError, TimeoutError,
ValueError)` three times (missing OnStepAdapter's KeyError and a raw OSError), and
`OnStepFocuserAdapter` converted only ValueError -- an INDI TimeoutError from a focuser move
escaped into the autofocus worker (which then stayed busy forever, L02). Ports return
results, not raw exceptions. Everything runs on the OnStep simulator (#51) on fake time.
"""

from __future__ import annotations

import pytest
from astrotool_core.mount.port import AxisDirection, MountAxis
from astrotool_core.onstep import OnStepFocuserAdapter, OnStepMountPulseAdapter
from astrotool_core.onstep.mount_pulse_adapter import ONSTEP_OPERATION_ERRORS
from astrotool_core.testing import OnStepScenario, make_simulated_onstep_connection
from astrotool_core.timing import FakeClock

#: The shapes OnStepAdapter 0.5.0 / the INDI layer really raise (see the S6.2 connect tests).
_DEVICE_ERRORS = [
    TimeoutError("INDI property ABS_FOCUS_POSITION did not update"),
    ConnectionError("OnStep INDI device is not connected"),
    RuntimeError("Focuser move refused: focuser_position_stale"),
    ValueError("Focuser target exceeds confirmed travel limits"),
    KeyError("UTC"),
    OSError("[Errno 104] Connection reset by peer"),
]


def _ids(error: BaseException) -> str:
    return type(error).__name__


def test_the_one_error_set_covers_every_device_failure_shape() -> None:
    for error in _DEVICE_ERRORS:
        assert isinstance(error, ONSTEP_OPERATION_ERRORS)
    # Programming errors are not device failures: they still surface (S6.2 INTERNAL_ERRORS).
    assert not isinstance(TypeError("x"), ONSTEP_OPERATION_ERRORS)
    assert not isinstance(AttributeError("x"), ONSTEP_OPERATION_ERRORS)


def _focuser() -> tuple[OnStepFocuserAdapter, object]:
    connection, made = make_simulated_onstep_connection(OnStepScenario(), clock=FakeClock())
    focuser = OnStepFocuserAdapter(connection)
    focuser.connect()
    return focuser, made[0].focuser


class TestFocuserAdapter:
    @pytest.mark.parametrize("error", _DEVICE_ERRORS, ids=_ids)
    def test_a_raising_move_absolute_is_a_rejected_move(
        self, monkeypatch: pytest.MonkeyPatch, error: Exception, caplog: pytest.LogCaptureFixture
    ) -> None:
        focuser, sim = _focuser()

        def boom(*_args: object, **_kwargs: object) -> object:
            raise error

        monkeypatch.setattr(sim, "move_absolute", boom)
        result = focuser.move_absolute(6000)
        assert result.accepted is False
        assert result.target_position == 6000 and result.start_position == 5000
        assert any("rejected" in r.getMessage() for r in caplog.records)
        assert focuser.status().moving is False  # the adapter's own moving flag was reset

    @pytest.mark.parametrize("error", _DEVICE_ERRORS, ids=_ids)
    def test_a_raising_relative_move_and_stop_do_not_raise(
        self, monkeypatch: pytest.MonkeyPatch, error: Exception
    ) -> None:
        focuser, sim = _focuser()

        def boom(*_args: object, **_kwargs: object) -> object:
            raise error

        monkeypatch.setattr(sim, "move_absolute", boom)
        monkeypatch.setattr(sim, "stop", boom)
        focuser.move(100)
        focuser.stop()

    @pytest.mark.parametrize("error", _DEVICE_ERRORS, ids=_ids)
    def test_a_raising_status_read_reports_unavailable(
        self, monkeypatch: pytest.MonkeyPatch, error: Exception
    ) -> None:
        focuser, sim = _focuser()

        def boom(*_args: object, **_kwargs: object) -> object:
            raise error

        monkeypatch.setattr(sim, "get_status", boom)
        status = focuser.status()
        assert status.available is False and status.moving is False
        assert focuser.is_moving() is False

    def test_a_programming_error_still_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        focuser, sim = _focuser()

        def boom(*_args: object, **_kwargs: object) -> object:
            raise TypeError("bug")

        monkeypatch.setattr(sim, "move_absolute", boom)
        with pytest.raises(TypeError):
            focuser.move_absolute(6000)


class TestPulseAdapter:
    @pytest.mark.parametrize(
        "error", [KeyError("UTC"), OSError("[Errno 104] Connection reset by peer")], ids=_ids
    )
    def test_an_angular_move_raising_outside_the_old_tuple_is_a_refusal(
        self, error: Exception
    ) -> None:
        clock = FakeClock()
        connection, _made = make_simulated_onstep_connection(
            OnStepScenario(parked=False, axis_move_errors=[error]), clock=clock
        )
        mount = OnStepMountPulseAdapter(connection)
        mount.connect()
        result = mount.move_angular(MountAxis.AXIS1, AxisDirection.POSITIVE, 120.0)
        assert result.accepted is False
        assert str(error.args[0]) in result.message
