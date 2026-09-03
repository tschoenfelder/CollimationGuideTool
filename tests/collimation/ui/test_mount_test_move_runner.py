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

    def stop_tracking(self) -> None:
        pass

    def start_tracking(self) -> None:
        pass


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

    def test_park_after_false_leaves_the_mount_unparked(self) -> None:
        mount_park = FakeMountPark(start_parked=True)
        mount = FakeMountAdapter()
        mount.connect()
        runner = MountTestMoveRunner()

        runner.submit(
            mount_park,
            mount,
            MountAxis.AXIS1,
            AxisDirection.POSITIVE,
            500,
            park_after=False,
        )

        assert _wait_for(lambda: not runner.is_busy)
        outcome = runner.take_latest()
        assert outcome is not None
        assert outcome.pulsed is True
        assert mount_park.status().parked is False

    def test_submit_passes_rate_preset_through_to_the_pulse(self) -> None:
        mount_park = FakeMountPark(start_parked=True)
        mount = FakeMountAdapter()
        mount.connect()
        runner = MountTestMoveRunner()

        runner.submit(
            mount_park, mount, MountAxis.AXIS1, AxisDirection.POSITIVE, 500, rate_preset="7"
        )

        assert _wait_for(lambda: not runner.is_busy)
        assert mount.rate_log == ["7"]

    def test_submit_sequence_runs_every_step_back_to_back_after_one_unpark(self) -> None:
        mount_park = FakeMountPark(start_parked=True)
        mount = FakeMountAdapter()
        mount.connect()
        runner = MountTestMoveRunner()

        started = runner.submit_sequence(
            mount_park,
            mount,
            [
                (MountAxis.AXIS1, AxisDirection.POSITIVE, 100),
                (MountAxis.AXIS2, AxisDirection.NEGATIVE, 50),
            ],
            rate_preset="7",
            park_after=False,
        )

        assert started is True
        assert _wait_for(lambda: not runner.is_busy)
        outcome = runner.take_latest()
        assert outcome is not None
        assert outcome.pulsed is True
        assert mount.pulse_log == [
            (MountAxis.AXIS1, AxisDirection.POSITIVE, 100),
            (MountAxis.AXIS2, AxisDirection.NEGATIVE, 50),
        ]
        assert mount.rate_log == ["7", "7"]
        # Only one unpark for the whole sequence, not one per step.
        assert mount_park.unpark_count == 1
        assert mount_park.status().parked is False

    def test_a_second_submit_on_an_already_unparked_mount_does_not_re_unpark(self) -> None:
        """Real live-hardware report: "You seem to enable tracking. That
        should not happen. No wonder, that the frames look blury" --
        traced to `_run()` calling `unpark()` unconditionally on *every*
        submit(), even when the mount was already confirmed unparked from
        an earlier submit() in the same run (Run Calibration's 4+ separate
        steps, each its own submit() call -- unlike submit_sequence's
        single multi-step call, already covered by the test above).
        `IndiMountParkAdapter.unpark()` resends the UNPARK switch command
        every time it runs, which re-triggers OnStep's own ~1.5s delayed
        auto-tracking-on quirk (see that module's own docstring) all over
        again on every single pulse -- landing the driver's own tracking
        override right around when some step's "after" frame gets
        captured. Only the first submit() of a run (mount actually
        parked) needs the full unpark() cycle; a later submit() on an
        already-unparked mount uses the lighter stop_tracking() instead
        (a single TRACK_OFF, no UNPARK resend) -- still corrects any
        tracking that crept back in, without re-arming the driver's own
        UNPARK-linked quirk again."""
        mount_park = FakeMountPark(start_parked=True)
        mount = FakeMountAdapter()
        mount.connect()
        runner = MountTestMoveRunner()

        runner.submit(
            mount_park, mount, MountAxis.AXIS1, AxisDirection.POSITIVE, 100, park_after=False
        )
        assert _wait_for(lambda: not runner.is_busy)
        assert mount_park.unpark_count == 1
        assert mount_park.stop_tracking_count == 0

        runner.submit(
            mount_park, mount, MountAxis.AXIS2, AxisDirection.POSITIVE, 100, park_after=False
        )
        assert _wait_for(lambda: not runner.is_busy)
        outcome = runner.take_latest()
        assert outcome is not None
        assert outcome.pulsed is True
        # Still just the one unpark() from the first submit() -- the
        # second used stop_tracking() instead.
        assert mount_park.unpark_count == 1
        assert mount_park.stop_tracking_count == 1

    def test_a_submit_after_the_mount_was_reparked_still_unparks_again(self) -> None:
        """The parked-check must be live, not "only ever the first submit()
        of this runner's lifetime" -- if something reparks the mount
        between two submit() calls (e.g. `park_after=True`, or a separate
        Mount panel park), the next submit() must still go through the
        real unpark() cycle rather than assuming it's already unparked."""
        mount_park = FakeMountPark(start_parked=True)
        mount = FakeMountAdapter()
        mount.connect()
        runner = MountTestMoveRunner()

        runner.submit(
            mount_park, mount, MountAxis.AXIS1, AxisDirection.POSITIVE, 100, park_after=True
        )
        assert _wait_for(lambda: not runner.is_busy)
        assert mount_park.status().parked is True  # re-parked by park_after=True

        runner.submit(
            mount_park, mount, MountAxis.AXIS2, AxisDirection.POSITIVE, 100, park_after=False
        )
        assert _wait_for(lambda: not runner.is_busy)
        assert mount_park.unpark_count == 2
        assert mount_park.stop_tracking_count == 0

    def test_submit_sequence_with_no_steps_is_a_no_op(self) -> None:
        runner = MountTestMoveRunner()
        started = runner.submit_sequence(
            FakeMountPark(start_parked=True), FakeMountAdapter(), []
        )
        assert started is False
        assert runner.is_busy is False

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

    def test_a_transiently_rejected_pulse_is_retried_until_it_succeeds(self) -> None:
        # Real report 45e5ae86 ("SEV 1"): "Shows unparked but fails for
        # parked" -- confirmed live (this same session) that a pulse can
        # still get rejected for a real, if short, window even after
        # status().parked has already settled to False. A single-shot
        # pulse_axis() call isn't reliable through that window; this
        # confirms the runner retries instead of giving up on the first
        # rejection.
        mount = FakeMountAdapter(reject_first_n_pulses=2)
        mount.connect()
        runner = MountTestMoveRunner()
        import collimation_tool.ui.mount_test_move_runner as runner_module

        original_delay = runner_module._PULSE_REJECTION_RETRY_DELAY_S
        runner_module._PULSE_REJECTION_RETRY_DELAY_S = 0.01  # keep the test fast
        try:
            runner.submit(
                FakeMountPark(start_parked=True),
                mount,
                MountAxis.AXIS1,
                AxisDirection.POSITIVE,
                500,
            )
            assert _wait_for(lambda: not runner.is_busy)
        finally:
            runner_module._PULSE_REJECTION_RETRY_DELAY_S = original_delay
        outcome = runner.take_latest()
        assert outcome is not None
        assert outcome.pulsed is True
        assert outcome.error is None
        # The 3rd attempt (index 2) is the one that actually landed.
        assert mount.pulse_log == [(MountAxis.AXIS1, AxisDirection.POSITIVE, 500)]

    def test_settle_ms_delays_completion_after_a_successful_pulse(self) -> None:
        # Real report: "calibration doesn't wait for mount to be
        # stabilized" -- the caller's "after" capture used to happen the
        # instant a pulse's motion-off confirmed, no allowance for
        # mechanical settle. Confirms the runner actually blocks (stays
        # is_busy) for settle_ms once a pulse succeeds, before reporting
        # done -- a caller polling is_busy to know when to capture waits
        # it out too.
        mount = FakeMountAdapter()
        mount.connect()
        runner = MountTestMoveRunner()

        started_at = time.monotonic()
        runner.submit(
            FakeMountPark(start_parked=True),
            mount,
            MountAxis.AXIS1,
            AxisDirection.POSITIVE,
            10,
            park_after=False,
            settle_ms=100,
        )
        assert _wait_for(lambda: not runner.is_busy, timeout_s=2.0)
        elapsed_s = time.monotonic() - started_at

        outcome = runner.take_latest()
        assert outcome is not None
        assert outcome.pulsed is True
        assert elapsed_s >= 0.1

    def test_settle_ms_is_skipped_when_the_pulse_is_rejected(self) -> None:
        # No valid "after" state to settle into if nothing was actually
        # pulsed -- must not add the delay on a failure path.
        mount = FakeMountAdapter(reject_first_n_pulses=999)  # never accepts
        mount.connect()
        runner = MountTestMoveRunner()
        import collimation_tool.ui.mount_test_move_runner as runner_module

        original_delay = runner_module._PULSE_REJECTION_RETRY_DELAY_S
        runner_module._PULSE_REJECTION_RETRY_DELAY_S = 0.01  # keep the test fast
        started_at = time.monotonic()
        try:
            runner.submit(
                FakeMountPark(start_parked=True),
                mount,
                MountAxis.AXIS1,
                AxisDirection.POSITIVE,
                10,
                park_after=False,
                settle_ms=5000,  # would dominate elapsed_s if not skipped
            )
            assert _wait_for(lambda: not runner.is_busy, timeout_s=2.0)
        finally:
            runner_module._PULSE_REJECTION_RETRY_DELAY_S = original_delay
        elapsed_s = time.monotonic() - started_at

        outcome = runner.take_latest()
        assert outcome is not None
        assert outcome.pulsed is False
        assert elapsed_s < 2.0  # nowhere near the 5s settle -- correctly skipped

    def test_a_pulse_rejected_on_every_attempt_still_gives_up_eventually(self) -> None:
        mount = FakeMountAdapter(reject_first_n_pulses=999)  # never accepts
        mount.connect()
        runner = MountTestMoveRunner()
        import collimation_tool.ui.mount_test_move_runner as runner_module

        original_delay = runner_module._PULSE_REJECTION_RETRY_DELAY_S
        runner_module._PULSE_REJECTION_RETRY_DELAY_S = 0.01
        try:
            runner.submit(
                FakeMountPark(start_parked=True),
                mount,
                MountAxis.AXIS1,
                AxisDirection.POSITIVE,
                500,
            )
            assert _wait_for(lambda: not runner.is_busy)
        finally:
            runner_module._PULSE_REJECTION_RETRY_DELAY_S = original_delay
        outcome = runner.take_latest()
        assert outcome is not None
        assert outcome.pulsed is False
        assert outcome.error is not None
        assert "rejected" in outcome.error
        assert mount.pulse_log == []  # never actually accepted
