from __future__ import annotations

from astrotool_core.mount.park_port import MountParkStatus
from astrotool_core.mount.tracking_mode import (
    TrackingMode,
    TrackingVerificationResult,
    TrackingVerificationStatus,
    ensure_tracking_mode,
)
from astrotool_core.testing.fake_mount_park import FakeMountPark
from astrotool_core.timing import FakeClock


class TestEnsureTrackingMode:
    def test_already_on_reports_already_correct(self) -> None:
        mount_park = FakeMountPark(start_parked=False)
        mount_park.start_tracking()
        mount_park.start_tracking_count = 0  # reset -- only the call under test matters

        result = ensure_tracking_mode(mount_park, TrackingMode.ON)

        assert result.status is TrackingVerificationStatus.ALREADY_CORRECT
        assert result.observed_mode is TrackingMode.ON
        assert result.ok
        assert mount_park.start_tracking_count == 0  # no correction issued

    def test_already_off_reports_already_correct(self) -> None:
        mount_park = FakeMountPark(start_parked=False)  # tracking starts False

        result = ensure_tracking_mode(mount_park, TrackingMode.OFF)

        assert result.status is TrackingVerificationStatus.ALREADY_CORRECT
        assert result.observed_mode is TrackingMode.OFF
        assert mount_park.stop_tracking_count == 0

    def test_off_when_on_required_is_repaired(self) -> None:
        mount_park = FakeMountPark(start_parked=False)  # tracking starts False

        result = ensure_tracking_mode(mount_park, TrackingMode.ON)

        assert result.status is TrackingVerificationStatus.REPAIRED
        assert result.observed_mode is TrackingMode.ON
        assert result.ok
        assert mount_park.start_tracking_count == 1

    def test_on_when_off_required_is_repaired(self) -> None:
        mount_park = FakeMountPark(start_parked=False)
        mount_park.start_tracking()

        result = ensure_tracking_mode(mount_park, TrackingMode.OFF)

        assert result.status is TrackingVerificationStatus.REPAIRED
        assert result.observed_mode is TrackingMode.OFF
        assert mount_park.stop_tracking_count == 1

    def test_unavailable_mount_reports_unavailable_without_issuing_commands(self) -> None:
        mount_park = FakeMountPark(available=False)

        result = ensure_tracking_mode(mount_park, TrackingMode.ON)

        assert result.status is TrackingVerificationStatus.UNAVAILABLE
        assert result.observed_mode is None
        assert not result.ok
        assert mount_park.start_tracking_count == 0

    def test_a_mount_that_refuses_the_correction_reports_repair_failed(self) -> None:
        class _StubbornMountPark(FakeMountPark):
            """A real mount can refuse to start tracking while parked --
            simulates that by making start_tracking() a no-op."""

            def start_tracking(self) -> None:
                self.start_tracking_count += 1  # command was sent, just ignored

        mount_park = _StubbornMountPark(start_parked=True)  # tracking False, refuses correction

        result = ensure_tracking_mode(mount_park, TrackingMode.ON)

        assert result.status is TrackingVerificationStatus.REPAIR_FAILED
        assert result.observed_mode is TrackingMode.OFF  # still wrong
        assert not result.ok
        assert mount_park.start_tracking_count == 1  # the attempt was made


class _SlowDriverMountPark(FakeMountPark):
    """Issue #44: a real INDI driver applies start/stop_tracking
    asynchronously -- the new state shows up `delay_s` of (fake) time later."""

    def __init__(self, clock: FakeClock, delay_s: float) -> None:
        super().__init__(start_parked=False)
        self._clock = clock
        self._delay_s = delay_s
        self.status_reads = 0

    def status(self) -> MountParkStatus:
        self.status_reads += 1
        return super().status()

    def start_tracking(self) -> None:
        self.start_tracking_count += 1
        self._clock.call_later(self._delay_s, lambda: setattr(self, "_tracking", True))


class TestSettlePollOnFakeTime:
    """Issue #53: the optional settle poll on a FakeClock (timeout 1.0 s, poll
    every 0.25 s) -- exact deadline boundaries, no real sleeping."""

    @staticmethod
    def _run(
        delay_s: float, *, settle_timeout_s: float = 1.0
    ) -> tuple[TrackingVerificationResult, FakeClock, _SlowDriverMountPark]:
        clock = FakeClock()
        mount_park = _SlowDriverMountPark(clock, delay_s)
        result = ensure_tracking_mode(
            mount_park, TrackingMode.ON,
            settle_timeout_s=settle_timeout_s, poll_interval_s=0.25, clock=clock,
        )
        return result, clock, mount_park

    def test_confirmed_just_before_the_deadline_is_repaired(self) -> None:
        result, clock, _ = self._run(0.999)
        assert result.status is TrackingVerificationStatus.REPAIRED
        assert clock.monotonic() == 1.0

    def test_confirmed_exactly_at_the_deadline_is_repaired(self) -> None:
        result, clock, mount_park = self._run(1.0)
        assert result.status is TrackingVerificationStatus.REPAIRED
        assert clock.sleeps == [0.25] * 4
        # initial status + immediate re-read + one read after each poll
        assert mount_park.status_reads == 1 + 1 + 4

    def test_confirmed_just_after_the_deadline_is_repair_failed(self) -> None:
        result, clock, _ = self._run(1.001)
        assert result.status is TrackingVerificationStatus.REPAIR_FAILED
        assert result.observed_mode is TrackingMode.OFF
        assert clock.monotonic() == 1.0  # gave up at the deadline, no extra wait

    def test_zero_settle_timeout_never_waits(self) -> None:
        result, clock, mount_park = self._run(0.1, settle_timeout_s=0.0)
        assert result.status is TrackingVerificationStatus.REPAIR_FAILED
        assert clock.sleeps == []
        assert mount_park.status_reads == 2  # initial + the one immediate re-read

    def test_the_settle_deadline_starts_after_the_immediate_re_read(self) -> None:
        """Ordering kept from before the clock was injected: a slow status
        round trip (0.5 s here) does not eat into the settle budget."""

        class _SlowStatusMountPark(FakeMountPark):
            def __init__(self, clock: FakeClock) -> None:
                super().__init__(start_parked=False)
                self._clock = clock

            def status(self) -> MountParkStatus:
                self._clock.advance(0.5)
                return super().status()

            def start_tracking(self) -> None:
                self.start_tracking_count += 1  # never confirms

        clock = FakeClock()
        result = ensure_tracking_mode(
            _SlowStatusMountPark(clock), TrackingMode.ON,
            settle_timeout_s=1.0, poll_interval_s=0.25, clock=clock,
        )

        assert result.status is TrackingVerificationStatus.REPAIR_FAILED
        # re-read ends at t=1.0 -> deadline 2.0 -> polls at 1.0 and 1.75.
        # (A deadline taken before the re-read, at 0.5, would allow only one.)
        assert clock.sleeps == [0.25, 0.25]
