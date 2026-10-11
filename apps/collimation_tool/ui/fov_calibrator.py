"""FovCalibrator — runs TerrestrialRegistrar.register() on a background
thread, off the UI thread.

Even at full resolution the default search parameters take on the order
of two real minutes on real camera-sized frames (see
`astrotool_core.registration.terrestrial_registrar`'s module docstring —
measured on a real rig, and reported by real-world use as "very slow"),
long enough to freeze the window's event loop if run inline from a button
click — exactly the UI-responsiveness class of bug this project already
had to fix once for the per-frame analysis pipeline (see `FrameAnalyzer`,
which this mirrors: same submit()/take_latest()/is_busy shape, "run at
most one at a time" semantics, and daemon background thread). This is
also where `_DEFAULT_SEARCH_DOWNSAMPLE` lives: the registrar's own
default (1, exact) is right for tests that check precise pixel positions,
but production calibration should search at reduced resolution for speed
— see that constant's docstring.

Also forwards the registrar's progress_callback so a caller can show
something better than a static "working" message for those two minutes
— see `latest_progress` and the real bug this was added for: a user
report that "Calibration started but working without any status on
progress" was, on inspection, expected behavior (the search was
genuinely still running) that looked indistinguishable from a hang.

Issue #29 moved the actual matching algorithm into
`astrotool_core.registration.terrestrial_registrar` (reusable domain
logic, zero Qt dependency) — this module is unchanged in *shape*, only in
which function it now drives and the `OpticalPrior`-based (rather than a
bare `approx_scale` float) calling convention that came with it.

S6.3 (#52 L02): the lifecycle of all three entry points (one calibration at a time,
guaranteed terminal state) is `OperationLifecycle`'s -- an exception in any of them used to
leave `is_busy` True for good ("Calibrate FOV" disabled until restart). A crashed run now
ends as a not-ok result (`NO_VALID_REGISTRATION`, the exception in `diagnostics["error"]`)
of the method that was running; progress is cleared either way.
"""

from __future__ import annotations

import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from astropy.io import fits
from astrotool_core.frames.frame import Frame
from astrotool_core.registration.artificial_star_registrar import ArtificialStarRegistrar
from astrotool_core.registration.astap_adapter import AstapSolver
from astrotool_core.registration.optical_prior import OpticalPrior
from astrotool_core.registration.result import (
    CrossCameraRegistrationResult,
    RegistrationMethod,
    RegistrationStatus,
)
from astrotool_core.registration.star_field_registrar import StarFieldHints, StarFieldRegistrar
from astrotool_core.registration.terrestrial_registrar import TerrestrialRegistrar

from collimation_tool.application.operation_lifecycle import OperationLifecycle

#: Target size (pixels) for the guide frame's larger dimension after
#: downsampling — see _auto_search_downsample and
#: TerrestrialRegistrar.register's "search_downsample" docstring
#: section: FFT cost drops roughly with the square of the downsample
#: factor, so a real ~1920px guide frame landing near this target
#: (factor ~4) cuts the full default search from ~2 minutes towards
#: single-digit seconds. Position accuracy degrades by up to the
#: resulting factor in guide-pixels, which a visual overlay guide doesn't
#: need to be exact to; rotation accuracy is unaffected.
_TARGET_SEARCH_DIMENSION = 480


def _auto_search_downsample(guide_mono: np.ndarray) -> int:
    """Downsample factor bringing guide_mono's larger dimension down to
    roughly _TARGET_SEARCH_DIMENSION pixels. Deliberately never
    downsamples an already-small image (factor 1 minimum) — a fixed
    factor here once made small test/synthetic frames lose so much
    detail that unrelated noise could pass the contrast floor and score
    a false match (a 20x25 template downsampled by a blanket factor of 4
    became 5x6 pixels, with almost nothing left to be selective about).
    """
    largest = int(max(guide_mono.shape))
    return max(1, round(largest / _TARGET_SEARCH_DIMENSION))


@dataclass(frozen=True)
class FovCalibrationOutcome:
    """Wraps a completed calibration's result so `take_latest()` can tell
    "nothing has finished since the last check" (returns `None`) apart
    from "finished, but found no confident match" (returns an outcome
    whose own `result.ok` is `False`)."""

    result: CrossCameraRegistrationResult


def _crashed(method: RegistrationMethod, exc: Exception) -> FovCalibrationOutcome:
    return FovCalibrationOutcome(
        result=CrossCameraRegistrationResult(
            method=method,
            status=RegistrationStatus.NO_VALID_REGISTRATION,
            diagnostics={"error": f"{type(exc).__name__}: {exc}"},
        )
    )


class FovCalibrator:
    def __init__(self) -> None:
        self._lifecycle: OperationLifecycle[FovCalibrationOutcome] = OperationLifecycle(
            "fov-calibrator"
        )
        #: The lifecycle's lock: guards `_latest_outcome` and `_progress` too.
        self._lock = self._lifecycle.lock
        self._latest_outcome: FovCalibrationOutcome | None = None
        #: (completed, total) candidates scored so far — see
        #: latest_progress(). None while idle or before the first
        #: candidate of a run has been scored.
        self._progress: tuple[int, int] | None = None
        self._registrar = TerrestrialRegistrar()

    def submit(
        self,
        main_mono: np.ndarray,
        guide_mono: np.ndarray,
        *,
        prior_a: OpticalPrior,
        prior_b: OpticalPrior,
        search_downsample: int | None = None,
    ) -> bool:
        """Start a calibration in the background. Returns False (a no-op)
        if one is already running — matches FrameAnalyzer's
        drop-rather-than-queue behavior, appropriate here too since this
        is explicitly triggered (see the "Calibrate FOV" button), not a
        per-frame submission that will naturally get a fresher retry.

        ``search_downsample`` defaults to None, meaning "compute from
        guide_mono's own size" (see _auto_search_downsample) rather than
        a fixed factor — pass an explicit value to override.
        """
        downsample = (
            search_downsample
            if search_downsample is not None
            else _auto_search_downsample(guide_mono)
        )
        return self._start(
            RegistrationMethod.TERRESTRIAL,
            lambda: self._run(main_mono, guide_mono, prior_a, prior_b, downsample),
            "fov-calibrator",
        )

    def submit_star_field(
        self,
        main_mono: np.ndarray,
        guide_mono: np.ndarray,
        *,
        prior_a: OpticalPrior,
        prior_b: OpticalPrior,
        solver: AstapSolver,
        hints: StarFieldHints | None = None,
    ) -> bool:
        """Same "run at most one at a time, drop rather than queue"
        contract as submit() -- shares its one lifecycle, since both
        are still just "the one calibration currently running", whichever
        mode it was started in. ASTAP solves files on disk, not in-memory
        arrays (see astap_adapter), so this writes both frames to real
        temp FITS files first (via Frame.to_fits_bytes() -- the existing,
        established way to serialize a bare pixel array to real FITS
        bytes elsewhere in this codebase) and cleans them up once the
        background run finishes, success or failure."""
        return self._start(
            RegistrationMethod.STAR_FIELD,
            lambda: self._run_star_field(main_mono, guide_mono, prior_a, prior_b, solver, hints),
            "fov-calibrator-star-field",
        )

    def _run_star_field(
        self,
        main_mono: np.ndarray,
        guide_mono: np.ndarray,
        prior_a: OpticalPrior,
        prior_b: OpticalPrior,
        solver: AstapSolver,
        hints: StarFieldHints | None,
    ) -> FovCalibrationOutcome:
        registrar = StarFieldRegistrar(solver)
        with tempfile.TemporaryDirectory(prefix="fov_calibrator_") as tmp_dir:
            path_a = Path(tmp_dir) / "main.fits"
            path_b = Path(tmp_dir) / "guide.fits"
            path_a.write_bytes(
                Frame(pixels=main_mono, header=fits.Header(), exposure_seconds=0.0).to_fits_bytes()
            )
            path_b.write_bytes(
                Frame(pixels=guide_mono, header=fits.Header(), exposure_seconds=0.0).to_fits_bytes()
            )
            result = registrar.register(path_a, path_b, prior_a, prior_b, hints=hints)
        return FovCalibrationOutcome(result=result)

    def submit_artificial_star(
        self,
        main_mono: np.ndarray,
        guide_mono: np.ndarray,
        *,
        prior_a: OpticalPrior,
        prior_b: OpticalPrior,
        assumed_rotation_deg: float = 0.0,
    ) -> bool:
        """Same "run at most one at a time, drop rather than queue"
        contract as submit() -- in-memory arrays, no external solver, no
        temp files, same shape as submit()/_run() above (issue #37:
        single-point-source registration is comparatively cheap -- no
        rotation/scale grid search -- so this needs no downsample/
        progress-callback plumbing)."""
        return self._start(
            RegistrationMethod.ARTIFICIAL_STAR,
            lambda: self._run_artificial_star(
                main_mono, guide_mono, prior_a, prior_b, assumed_rotation_deg
            ),
            "fov-calibrator-artificial-star",
        )

    def _run_artificial_star(
        self,
        main_mono: np.ndarray,
        guide_mono: np.ndarray,
        prior_a: OpticalPrior,
        prior_b: OpticalPrior,
        assumed_rotation_deg: float,
    ) -> FovCalibrationOutcome:
        registrar = ArtificialStarRegistrar()
        result = registrar.register(
            main_mono, guide_mono, prior_a, prior_b, assumed_rotation_deg=assumed_rotation_deg
        )
        return FovCalibrationOutcome(result=result)

    def _start(
        self,
        method: RegistrationMethod,
        work: Callable[[], FovCalibrationOutcome],
        thread_name: str,
    ) -> bool:
        """The shared "run at most one at a time, drop rather than queue" start of all three
        entry points (one lifecycle: whichever mode, it is the one calibration running)."""
        return self._lifecycle.start(
            work,
            on_error=lambda exc: _crashed(method, exc),
            publish=self._publish,
            on_begin=self._reset_progress,
            thread_name=thread_name,
        )

    def _reset_progress(self) -> None:
        self._progress = None  # called under self._lock

    def _publish(self, outcome: FovCalibrationOutcome) -> None:
        # Called under self._lock, together with the terminal transition.
        self._latest_outcome = outcome
        self._progress = None  # nothing left to report once finished

    def _on_progress(self, completed: int, total: int) -> None:
        with self._lock:
            self._progress = (completed, total)

    def _run(
        self,
        main_mono: np.ndarray,
        guide_mono: np.ndarray,
        prior_a: OpticalPrior,
        prior_b: OpticalPrior,
        search_downsample: int,
    ) -> FovCalibrationOutcome:
        result = self._registrar.register(
            main_mono, guide_mono, prior_a, prior_b,
            search_downsample=search_downsample, progress_callback=self._on_progress,
        )
        return FovCalibrationOutcome(result=result)

    def take_latest(self) -> FovCalibrationOutcome | None:
        """Return and clear the latest completed outcome, if any —
        `None` means no calibration has finished since the last call."""
        with self._lock:
            outcome, self._latest_outcome = self._latest_outcome, None
            return outcome

    def latest_progress(self) -> tuple[int, int] | None:
        """Current (completed, total) candidate count for an in-flight
        calibration, or None if idle or nothing has been scored yet.

        Unlike take_latest(), this does not clear on read — a caller
        polling every tick while a calibration runs (see MainWindow) is
        meant to see the same value repeatedly until it changes."""
        with self._lock:
            return self._progress

    @property
    def is_busy(self) -> bool:
        return self._lifecycle.is_busy
