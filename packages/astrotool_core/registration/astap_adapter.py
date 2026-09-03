"""ASTAP adapter — issue #29 #4: "ASTAP is explicitly allowed as a
locally installed external dependency for astronomical frames... ASTAP
shall remain behind an adapter boundary." This module is that boundary:
the only place that knows how to invoke the real `astap_cli` binary and
parse its output into an `astropy.wcs.WCS`. `star_field_registrar` (the
actual geometry logic) depends only on `AstapSolver`'s small Protocol, so
it's fully testable with a fake solver and synthetic WCS objects, never
touching a real process.

Deployment note: no ASTAP install exists on the Windows machine this
adapter was developed on — per project convention (see e.g. the OnStep/
INDI mount adapters), this ships with the real subprocess path unverified
against the actual binary, targeting the Raspberry Pi install where ASTAP
is expected to actually live. `AstapCliSolver.is_available()` and every
failure path here are written defensively (never assumes a specific exit
code or output-file convention beyond what ASTAP's own documented CLI
promises) so an imperfect assumption about its exact behavior degrades to
a clear `SOLVE_FAILED`/`ASTAP_UNAVAILABLE`, not a crash.

Assumed CLI contract (ASTAP's own documented command-line interface):
``astap_cli -f <fits_path> [-ra <hours> -spd <south-pole-distance deg>]
[-fov <deg>] [-r <search radius deg>] -z <downsample> -update`` — with
``-update``, a successful solve writes WCS keywords (CRVAL1/2, CRPIX1/2,
CD1_1/1_2/2_1/2_2) directly into the same FITS file's primary header;
exit code 0 on success. This adapter treats "does the file's own header
now parse as a real celestial WCS" (`astropy.wcs.WCS(header).has_celestial`)
as the actual success signal rather than trusting exit code alone, since
that's the one thing that can't be misjudged from a wrong assumption
about ASTAP's exact exit-code/logging conventions.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol

from astropy.io import fits
from astropy.wcs import WCS

_DEFAULT_BINARY = "astap_cli"
_DEFAULT_TIMEOUT_S = 60.0


class AstapSolveStatus(Enum):
    SOLVED = "solved"
    ASTAP_UNAVAILABLE = "astap_unavailable"
    SOLVE_FAILED = "solve_failed"
    TIMEOUT = "timeout"


@dataclass(frozen=True)
class AstapSolveResult:
    status: AstapSolveStatus
    wcs: WCS | None = None
    message: str = ""

    @property
    def ok(self) -> bool:
        return self.status is AstapSolveStatus.SOLVED


@dataclass(frozen=True)
class AstapSolveHint:
    """Optional prior to narrow/speed up ASTAP's own blind search — every
    field in degrees, `None` meaning "no hint, blind-solve". A previous
    registration's own boresight is a reasonable source for this (issue
    #29 #3: "a previous registration may be used as a hint, but current
    image/WCS evidence must be able to supersede it" — a hint only ever
    narrows *where ASTAP looks*, never substitutes for actually solving)."""

    ra_deg: float | None = None
    dec_deg: float | None = None
    radius_deg: float | None = None
    fov_deg: float | None = None


class AstapSolver(Protocol):
    def solve(self, fits_path: Path, *, hint: AstapSolveHint | None = None) -> AstapSolveResult: ...


class AstapCliSolver:
    """Real adapter: shells out to a configured `astap_cli` binary. See
    this module's own docstring for the assumed CLI/output contract."""

    def __init__(
        self,
        *,
        binary_path: str = _DEFAULT_BINARY,
        timeout_s: float = _DEFAULT_TIMEOUT_S,
        downsample: int = 2,
        extra_args: Sequence[str] = (),
    ) -> None:
        self._binary_path = binary_path
        self._timeout_s = timeout_s
        self._downsample = downsample
        self._extra_args = tuple(extra_args)

    def is_available(self) -> bool:
        return shutil.which(self._binary_path) is not None

    def _build_args(self, fits_path: Path, hint: AstapSolveHint | None) -> list[str]:
        args = [
            self._binary_path, "-f", str(fits_path), "-z", str(self._downsample), "-update",
        ]
        if hint is not None:
            if hint.ra_deg is not None:
                args += ["-ra", str(hint.ra_deg / 15.0)]  # ASTAP wants RA in hours
            if hint.dec_deg is not None:
                args += ["-spd", str(hint.dec_deg + 90.0)]  # south-pole distance
            if hint.radius_deg is not None:
                args += ["-r", str(hint.radius_deg)]
            if hint.fov_deg is not None:
                args += ["-fov", str(hint.fov_deg)]
        return [*args, *self._extra_args]

    def solve(self, fits_path: Path, *, hint: AstapSolveHint | None = None) -> AstapSolveResult:
        if not self.is_available():
            return AstapSolveResult(
                AstapSolveStatus.ASTAP_UNAVAILABLE,
                message=f"'{self._binary_path}' not found on PATH",
            )
        try:
            completed = subprocess.run(  # noqa: S603 -- fixed binary, no shell, args are ours
                self._build_args(fits_path, hint),
                capture_output=True, text=True, timeout=self._timeout_s, check=False,
            )
        except subprocess.TimeoutExpired:
            return AstapSolveResult(AstapSolveStatus.TIMEOUT, message="astap_cli timed out")
        except OSError as exc:
            return AstapSolveResult(AstapSolveStatus.SOLVE_FAILED, message=str(exc))

        try:
            with fits.open(fits_path) as hdul:
                header = hdul[0].header
                wcs = WCS(header)
        except (OSError, ValueError) as exc:
            return AstapSolveResult(
                AstapSolveStatus.SOLVE_FAILED, message=f"could not read solved FITS: {exc}"
            )

        if not wcs.has_celestial:
            detail = completed.stderr.strip() or completed.stdout.strip() or "no WCS in header"
            return AstapSolveResult(AstapSolveStatus.SOLVE_FAILED, message=detail)
        return AstapSolveResult(AstapSolveStatus.SOLVED, wcs=wcs)
