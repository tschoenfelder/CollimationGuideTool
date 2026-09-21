"""FakeOnStepSerial -- a serial-line OnStep controller simulator for the REAL OnStepAdapter.

`FakeOnStepClient` fakes OnStepAdapter's own surface (fast unit tests of our shims). This
one sits one level lower: it replaces `serial.Serial`, so the real `OnStepClient` /
`OnStepMount` 0.3.5 code runs unmodified against a scripted LX200/OnStep command set. It
proves the shims' calls (directions, `move_ra_timed` modes, partial motion calibration,
`move_ra`/`move_dec`, unpark/tracking) against the installed adapter release itself, so an
API or safety-policy change in a new release fails a test instead of the rig.

Modelled: identity (`:GVP#`), status flags (`:GU#`: tracking, parked, at-home), position
(`:GR#`/`:GD#`, updated by motion), clock/sidereal time consistent with the configured site,
limits, pier side, tracking on/off, park/unpark, rate selection, timed motion start/stop.
Anything else answers empty (like a controller that does not implement the command).
"""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime

from onstep_adapter.mount import _lst_hours

#: Motion rate (arcsec/s) OnStep uses for each `:R<n>#` preset / the centering rate `:RC#`.
_SIDEREAL = 15.041
_PRESET_X = {0: 0.25, 1: 0.5, 2: 1.0, 3: 2.0, 4: 4.0, 5: 8.0, 6: 20.0, 7: 48.0}


def _hms(hours: float) -> str:
    hours %= 24.0
    h = int(hours)
    m = int((hours - h) * 60)
    s = int(round(((hours - h) * 60 - m) * 60))
    if s == 60:
        s, m = 0, m + 1
    return f"{h:02d}:{m:02d}:{s:02d}#"


def _dms(deg: float) -> str:
    sign = "+" if deg >= 0 else "-"
    deg = abs(deg)
    d = int(deg)
    m = int((deg - d) * 60)
    s = int(round(((deg - d) * 60 - m) * 60))
    if s == 60:
        s, m = 0, m + 1
    return f"{sign}{d:02d}*{m:02d}:{s:02d}#"


class FakeOnStepSerial:
    """Drop-in for `serial.Serial` as `onstep_adapter.mount` uses it."""

    def __init__(
        self,
        *,
        site_lon_deg: float,
        site_lat_deg: float = 50.336,
        parked: bool = True,
        at_home: bool = True,
        tracking: bool = False,
        center_rate_arcsec_per_s: float = 120.0,
        ra_h: float | None = None,
        dec_deg: float = 45.0,
    ) -> None:
        self.is_open = True
        self.timeout = 1.0
        self._lon = site_lon_deg
        self._lat = site_lat_deg
        self._buf = b""
        self._lock = threading.Lock()
        self.parked = parked
        self.at_home = at_home
        self.tracking = tracking
        self.center_rate = center_rate_arcsec_per_s
        # Default: 2 h EAST of the meridian (HA = -2 h), safely inside any sane hour-angle
        # limit whatever time of day the test runs at.
        self.ra_h = (
            (_lst_hours(site_lon_deg, datetime.now(UTC)) + 2.0) % 24.0
            if ra_h is None
            else ra_h
        )
        self.dec_deg = dec_deg
        #: Cumulative arcsec actually moved per axis (RA + = east, Dec + = north).
        self.moved_arcsec = {"ra": 0.0, "dec": 0.0}
        self.rate_arcsec_per_s = center_rate_arcsec_per_s
        self.active_motion: str | None = None
        self.motion_started_at = 0.0
        self.commands: list[str] = []

    # ---- serial.Serial surface -------------------------------------------------------
    def reset_input_buffer(self) -> None:
        self._buf = b""

    def close(self) -> None:
        self.is_open = False

    def read(self, size: int = 1) -> bytes:
        out, self._buf = self._buf[:size], self._buf[size:]
        return out

    def read_until(self, terminator: bytes = b"#", size: int | None = None) -> bytes:
        i = self._buf.find(terminator)
        if i < 0:
            out, self._buf = self._buf, b""
            return out
        out, self._buf = self._buf[: i + 1], self._buf[i + 1 :]
        return out

    def write(self, data: bytes) -> int:
        with self._lock:
            for cmd in self._split(data.decode()):
                self.commands.append(cmd)
                self._buf += self._handle(cmd)
        return len(data)

    # ---- controller ------------------------------------------------------------------
    @staticmethod
    def _split(text: str) -> list[str]:
        return [f"{part}#" for part in text.split("#") if part.startswith(":")]

    def _status(self) -> bytes:
        flags = ""
        if not self.tracking:
            flags += "n"
        flags += "N"  # no goto in progress
        flags += "P" if self.parked else "p"
        if self.at_home:
            flags += "H"
        return (flags + "#").encode()

    def _stop_motion(self) -> None:
        if self.active_motion is None:
            return
        elapsed = max(0.0, time.monotonic() - self.motion_started_at)
        moved = elapsed * self.rate_arcsec_per_s
        axis = "ra" if self.active_motion in {"e", "w"} else "dec"
        sign = 1.0 if self.active_motion in {"e", "n"} else -1.0
        self.moved_arcsec[axis] += sign * moved
        if axis == "ra":
            self.ra_h = (self.ra_h + sign * moved / 3600.0 / 15.0) % 24.0
        else:
            self.dec_deg += sign * moved / 3600.0
        self.active_motion = None
        if self.at_home and moved > 0:
            self.at_home = False

    def _handle(self, cmd: str) -> bytes:  # noqa: C901 -- a flat command table
        now = datetime.now(UTC)
        if cmd == ":GVP#":
            return b"On-Step#"
        if cmd == ":GU#":
            return self._status()
        if cmd == ":GR#":
            return _hms(self.ra_h).encode()
        if cmd == ":GD#":
            return _dms(self.dec_deg).encode()
        if cmd == ":GS#":
            return _hms(_lst_hours(self._lon, now)).encode()
        if cmd == ":GC#":
            local = now.astimezone().replace(tzinfo=None)
            return f"{local:%m/%d/%y}#".encode()
        if cmd == ":GL#":
            local = now.astimezone().replace(tzinfo=None)
            return f"{local:%H:%M:%S}#".encode()
        if cmd in {":GX42#", ":GX43#"}:  # mechanical axis 1/2 position, degrees
            axis = "ra" if cmd == ":GX42#" else "dec"
            return f"{self.moved_arcsec[axis] / 3600.0:.4f}#".encode()
        if cmd in {":Gt#", ":Gg#"}:  # site, arc-minute precision; longitude is WEST-positive
            value = self._lat if cmd == ":Gt#" else -self._lon
            sign = "-" if value < 0 else "+"
            minutes = round(abs(value) * 60.0)
            return f"{sign}{minutes // 60:03d}*{minutes % 60:02d}#".encode()
        if cmd == ":Gh#":
            return b"+00#"
        if cmd == ":Go#":
            return b"90#"
        if cmd == ":Gm#":
            return b"N#"
        if cmd in {":Td#", ":Te#"}:
            self.tracking = cmd == ":Te#"
            return b"1"
        if cmd == ":hR#":
            self.parked = False
            return b"1"
        if cmd == ":hP#":
            self.parked = True
            self._stop_motion()
            return b"1"
        if cmd == ":Q#":
            self._stop_motion()
            return b""
        if cmd in {":RC#", ":RG#"}:
            self.rate_arcsec_per_s = self.center_rate
            return b""
        if len(cmd) == 5 and cmd.startswith(":R") and cmd[2].isdigit():
            preset = int(cmd[2])
            self.rate_arcsec_per_s = _PRESET_X.get(preset, 48.0) * _SIDEREAL
            return b""
        if len(cmd) == 4 and cmd.startswith(":M") and cmd[2] in "ewns":
            self._stop_motion()
            self.active_motion = cmd[2]
            self.motion_started_at = time.monotonic()
            return b""
        if len(cmd) == 4 and cmd.startswith(":Q") and cmd[2] in "ewns":
            self._stop_motion()
            return b""
        return b""
