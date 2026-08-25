from astrotool_core.mount.port import AxisDirection, MountAxis
from astrotool_core.testing.fake_mount import FakeMountAdapter
from guide_tool.application.correction_policy import GuideCorrectionPolicy
from guide_tool.domain.correction_model import WouldGuidePulse


def test_send_issues_a_pulse_on_the_mount() -> None:
    mount = FakeMountAdapter()
    mount.connect()
    policy = GuideCorrectionPolicy(mount)

    pulse = WouldGuidePulse(
        axis=MountAxis.AXIS1, direction=AxisDirection.NEGATIVE, duration_ms=70, reason="axis1_error"
    )
    result = policy.send(pulse)

    assert result.accepted is True
    assert mount.pulse_log == [(MountAxis.AXIS1, AxisDirection.NEGATIVE, 70)]


def test_send_all_issues_every_pulse_in_order() -> None:
    mount = FakeMountAdapter()
    mount.connect()
    policy = GuideCorrectionPolicy(mount)

    pulses = [
        WouldGuidePulse(MountAxis.AXIS1, AxisDirection.POSITIVE, 100, "axis1_error"),
        WouldGuidePulse(MountAxis.AXIS2, AxisDirection.NEGATIVE, 50, "axis2_error"),
    ]
    results = policy.send_all(pulses)

    assert [r.accepted for r in results] == [True, True]
    assert mount.pulse_log == [
        (MountAxis.AXIS1, AxisDirection.POSITIVE, 100),
        (MountAxis.AXIS2, AxisDirection.NEGATIVE, 50),
    ]


def test_send_reflects_rejection_when_mount_not_connected() -> None:
    mount = FakeMountAdapter()  # never connected
    policy = GuideCorrectionPolicy(mount)

    pulse = WouldGuidePulse(MountAxis.AXIS1, AxisDirection.POSITIVE, 100, "axis1_error")
    result = policy.send(pulse)

    assert result.accepted is False
