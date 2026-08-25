"""GuideCorrectionPolicy — issues guide pulses computed by
`domain.correction_model.compute_would_pulses`.

Written fresh (not a literal port) — separate class from CollimationTool's
`CollimationRecenterPolicy` so a change to the guiding correction loop can
never unintentionally alter collimation's recentering, per the
architecture doc's dependency-direction rationale. Deliberately thin: all
the actual decision logic (deadband, direction, duration) lives in the
pure `correction_model.compute_would_pulses`; this class only sends the
already-computed pulses to the mount.
"""

from __future__ import annotations

from astrotool_core.mount.port import CommandResult, MountPort

from guide_tool.domain.correction_model import WouldGuidePulse


class GuideCorrectionPolicy:
    def __init__(self, mount: MountPort) -> None:
        self._mount = mount

    def send(self, pulse: WouldGuidePulse) -> CommandResult:
        return self._mount.pulse_axis(pulse.axis, pulse.direction, pulse.duration_ms)

    def send_all(self, pulses: list[WouldGuidePulse]) -> list[CommandResult]:
        return [self.send(pulse) for pulse in pulses]
