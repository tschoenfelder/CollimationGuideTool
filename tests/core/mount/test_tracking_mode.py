from __future__ import annotations

from astrotool_core.mount.tracking_mode import (
    TrackingMode,
    TrackingVerificationStatus,
    ensure_tracking_mode,
)
from astrotool_core.testing.fake_mount_park import FakeMountPark


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
