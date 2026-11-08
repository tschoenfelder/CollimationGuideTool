"""`finish_motion`: the shims wait for OnStepAdapter's non-blocking `MotionHandle`."""

from __future__ import annotations

import pytest
from astrotool_core.onstep.errors import ONSTEP_OPERATION_ERRORS
from astrotool_core.onstep.motion import MoveStopped, finish_motion
from onstep_adapter.indi_motion import MotionStopped


class _Handle:
    def __init__(self, result: object = None, error: BaseException | None = None) -> None:
        self.result, self.error, self.waits = result, error, 0

    def wait(self) -> object:
        self.waits += 1
        if self.error is not None:
            raise self.error
        return self.result


def test_a_handle_is_waited_for_and_its_result_returned() -> None:
    handle = _Handle(result="parked")
    assert finish_motion(handle) == "parked"
    assert handle.waits == 1


def test_a_direct_result_is_returned_unchanged() -> None:
    result = object()
    assert finish_motion(result) is result


def test_a_stopped_move_is_an_onstep_operation_error() -> None:
    with pytest.raises(MoveStopped, match="stopped") as caught:
        finish_motion(_Handle(error=MotionStopped("park")))
    assert isinstance(caught.value, ONSTEP_OPERATION_ERRORS)
    assert isinstance(caught.value.__cause__, MotionStopped)


def test_other_move_errors_pass_through() -> None:
    with pytest.raises(TimeoutError):
        finish_motion(_Handle(error=TimeoutError("Axis move did not reach its finite target")))
