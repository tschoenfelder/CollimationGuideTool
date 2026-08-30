import time
from collections.abc import Callable

from astrotool_core.mount.park_port import MountParkPort, MountParkStatus
from astrotool_core.mount.port import AxisDirection, MountAxis
from astrotool_core.testing.fake_mount import FakeMountAdapter
from astrotool_core.testing.fake_mount_park import FakeMountPark
from collimation_tool.ui.mount_test_move_runner import MountTestMoveRunner


def _wait_for(predicate: Callable[[], bool], *, timeout_s: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class _NeverUnparks(MountParkPort):
    """Simulates OnStep's real refusal-to-move-while-parked quirk taken
    to its extreme: unpark() is accepted at the INDI level but never
    actually settles — pins MountTestMoveRunner's own timeout/abort path,
    since a hung real mount must not hang this runner forever."""

    def connect(self) -> None:
        pass

    def disconnect(self) -> None:
        pass

    @property
    def is_available(self) -> bool:
        return True

    def status(self) -> MountParkStatus:
        return MountParkStatus(available=True, parked=True, tracking=False)

    def park(self) -> None:
        pass

    def unpark(self) -> None:
        pass  # status() always reports parked regardless


class TestMountTestMoveRunner:
    def test_submit_unparks_pulses_and_reparks(self) -> None:
        mount_park = FakeMountPark(start_parked=True)
        mount = FakeMountAdapter()
        mount.connect()
        runner = MountTestMoveRunner()

        started = runner.submit(mount_park, mount, MountAxis.AXIS1, AxisDirection.POSITIVE, 500)

        assert started is True
        assert _wait_for(lambda: not runner.is_busy)
        outcome = runner.take_latest()
        assert outcome is not None
        assert outcome.pulsed is True
        assert outcome.error is None
        assert mount.pulse_log == [(MountAxis.AXIS1, AxisDirection.POSITIVE, 500)]
        # Ends parked again, same as it started — see module docstring.
        assert mount_park.status().parked is True

    def test_take_latest_returns_none_when_nothing_has_completed_yet(self) -> None:
        assert MountTestMoveRunner().take_latest() is None

    def test_take_latest_clears_the_outcome_so_it_is_returned_only_once(self) -> None:
        mount = FakeMountAdapter()
        mount.connect()
        runner = MountTestMoveRunner()
        runner.submit(
            FakeMountPark(start_parked=True), mount, MountAxis.AXIS1, AxisDirection.POSITIVE, 500
        )
        assert _wait_for(lambda: not runner.is_busy)
        assert runner.take_latest() is not None
        assert runner.take_latest() is None

    def test_a_submit_while_busy_is_a_no_op(self) -> None:
        runner = MountTestMoveRunner()
        runner._busy = True  # noqa: SLF001 -- simulate an in-flight test move
        mount = FakeMountAdapter()
        mount.connect()
        started = runner.submit(
            FakeMountPark(start_parked=True), mount, MountAxis.AXIS1, AxisDirection.POSITIVE, 500
        )
        assert started is False

    def test_rejected_pulse_reports_an_error_but_still_reparks(self) -> None:
        mount = FakeMountAdapter()  # never connected -> pulse_axis always rejects
        mount_park = FakeMountPark(start_parked=True)
        runner = MountTestMoveRunner()
        runner.submit(mount_park, mount, MountAxis.AXIS1, AxisDirection.POSITIVE, 500)
        assert _wait_for(lambda: not runner.is_busy)
        outcome = runner.take_latest()
        assert outcome is not None
        assert outcome.pulsed is False
        assert outcome.error is not None
        # A pulse rejection still must not strand the mount unparked.
        assert mount_park.status().parked is True

    def test_a_mount_that_never_unparks_times_out_instead_of_hanging(self) -> None:
        mount = FakeMountAdapter()
        mount.connect()
        runner = MountTestMoveRunner()
        # Keep the test itself fast — the real timeout constants are
        # seconds long, which would make this test unnecessarily slow.
        import collimation_tool.ui.mount_test_move_runner as runner_module

        original_timeout = runner_module._UNPARK_TIMEOUT_S
        runner_module._UNPARK_TIMEOUT_S = 0.1
        try:
            runner.submit(_NeverUnparks(), mount, MountAxis.AXIS1, AxisDirection.POSITIVE, 500)
            assert _wait_for(lambda: not runner.is_busy, timeout_s=2.0)
        finally:
            runner_module._UNPARK_TIMEOUT_S = original_timeout
        outcome = runner.take_latest()
        assert outcome is not None
        assert outcome.pulsed is False
        assert outcome.error is not None
        assert "unpark" in outcome.error
        assert mount.pulse_log == []  # never reached the pulse
