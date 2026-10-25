"""Issue #56 -- terrestrial autofocus on a real low-texture scene, on the REAL frames of
diagnostics 402eabde-d730-43b0-823d-a7f47c61f944 (`datasets/regressions/56/`, daytime: an
antenna mast against a bright sky) and f0bc314b-a889-4e18-ac9b-74971f42f014
(`datasets/regressions/56-dusk/`, the same mast at dusk, 2 s exposure).

LOCAL-ONLY: the real frames are not committed (large test data stays local); they live in
the git-ignored `local_test_data/56*/frames/` and every real-frame test here skips, with the
reason, when they are absent (CI). The logic is covered without them by the synthetic,
mutation-proven unit tests in tests/core/focus/test_terrestrial_focus_metric.py and
tests/collimation/application/test_autofocus_controller_terrestrial.py.

Before the fix `measure_terrestrial_focus` rejected ~90 % of the tiles as "moving" on noise
alone and the score was flat/non-monotone under added defocus, so the search never saw a
5 % improvement and returned to the start without focusing.

Review F1/F3 (contract amendment): the score at one position must not depend on which of
the real samples, in which order, with which duplicates were measured; and clipped glints
far above full scale (blur-before-clip simulation of the real scene, driven through the real
AutofocusController + BoundedFocusSearcher) must be stepped down by exposure or reported.

Bespoke (not the generic `_loader.BOUNDARIES` driver): each case needs a blur sweep built
from the frames. The expectations are independent of the implementation -- added Gaussian
blur IS more defocus (a known software transform); a frame made of the bundle's own measured
noise around a constant has, by construction, no moving content; a tile whose pixels are
replaced in later samples has, by construction, changed (see each dataset's expected.json).
"""

from __future__ import annotations

import itertools
from pathlib import Path

import numpy as np
import pytest
from _loader import REGRESSIONS_DIR, REPO_ROOT, RegressionCase, load_manifest
from astropy.io import fits
from astrotool_core.acquisition.stable_frame_acquisition import (
    DeliveredFrame,
    FrameAcquisitionResult,
    FrameAcquisitionStatus,
)
from astrotool_core.focus.fake_focuser import FakeFocuser
from astrotool_core.focus.terrestrial_focus_metric import measure_terrestrial_focus
from astrotool_core.timing import FakeClock
from collimation_tool.application.autofocus_controller import (
    HIGHLIGHTS_CLIPPED,
    AutofocusController,
    AutofocusMode,
    AutofocusResult,
    ExposureControl,
)
from collimation_tool.application.autofocus_search import AutofocusStatus

_DATASETS = ("56", "56-dusk")
#: The production call: AutofocusController's default terrestrial tile size and sample count.
_TILE_SIZE_PX = 64
_SAMPLES = 3


def _main_frames(dataset: str) -> list[np.ndarray]:
    """The bundle's three Main (ATR585M) frames, lossless uint16, from the git-ignored
    `local_test_data/<dataset>/frames/` (large test data is never committed -- same
    convention as datasets/regressions/28/). Absent -> the test skips, saying how to get
    them; CI has no frames, the synthetic unit tests cover the logic there."""
    frames_dir = REPO_ROOT / "local_test_data" / dataset / "frames"
    paths = [frames_dir / f"frame_{index}.fits" for index in range(3)]
    missing = [path.name for path in paths if not path.is_file()]
    if missing:
        pytest.skip(
            f"local-only real frames absent: {frames_dir} lacks {missing} -- see "
            f"datasets/regressions/{dataset}/README.md (pull_diagnostic_bundle.py + "
            "slim_regression_frames.py)"
        )
    return [np.asarray(fits.getdata(path), dtype=np.float64) for path in paths]


def _case(dataset: str, name: str) -> RegressionCase:
    manifest = load_manifest(REGRESSIONS_DIR / dataset)
    return next(case for case in manifest.cases if case.name == name)


def _noise_sigma(frames: list[np.ndarray]) -> float:
    """Per-pixel sensor noise, measured from the difference of two real samples."""
    return float(np.std(frames[1] - frames[0]) / np.sqrt(2.0))


def _blur_sweep(
    frames: list[np.ndarray], sigmas: list[float], rng: np.random.Generator
) -> dict[float, list[np.ndarray]]:
    """`_SAMPLES` noisy samples per added-blur level, all at the real frames' noise level.

    The mean of the real frames carries noise/sqrt(3); blurring removes it, so each blurred
    sample gets the full measured noise again and the unblurred one tops it up to that level.
    """
    clean = np.mean(frames, axis=0)
    noise = _noise_sigma(frames)
    fy = np.fft.fftfreq(clean.shape[0])[:, None]
    fx = np.fft.rfftfreq(clean.shape[1])[None, :]
    spectrum = np.fft.rfft2(clean)
    sweep: dict[float, list[np.ndarray]] = {}
    for sigma in sigmas:
        if sigma == 0.0:
            base, extra = clean, noise * np.sqrt(1.0 - 1.0 / len(frames))
        else:
            kernel = np.exp(-2.0 * (np.pi * sigma) ** 2 * (fx**2 + fy**2))
            base, extra = np.fft.irfft2(spectrum * kernel, s=clean.shape), noise
        sweep[sigma] = [base + rng.normal(0.0, extra, base.shape) for _ in range(_SAMPLES)]
    return sweep


@pytest.mark.parametrize("dataset", _DATASETS)
def test_added_defocus_lowers_the_score_monotonically_and_by_at_least_5_percent(
    dataset: str,
) -> None:
    case = _case(dataset, "added_defocus_lowers_the_score")
    sigmas = [float(value) for value in case.expect["blur_sigmas_px"]]
    from_sigma = float(case.expect["min_step_drop_from_sigma_px"])
    min_drop = float(case.expect["min_step_drop"])

    sweep = _blur_sweep(_main_frames(dataset), sigmas, np.random.default_rng(56))
    scores: list[float] = []
    for sigma in sigmas:
        measurement = measure_terrestrial_focus(sweep[sigma], tile_size_px=_TILE_SIZE_PX)
        assert measurement.sharpness is not None, f"{dataset}: no score at {sigma} px"
        scores.append(measurement.sharpness)

    table = dict(zip(sigmas, scores, strict=True))
    for (s_sharp, sharper), (s_blur, blurrier) in zip(
        table.items(), list(table.items())[1:], strict=False
    ):
        assert blurrier < sharper, f"{dataset}: not monotone at {s_sharp}->{s_blur} px: {table}"
        if s_blur >= from_sigma:
            assert blurrier <= (1.0 - min_drop) * sharper, (
                f"{dataset}: {s_sharp}->{s_blur} px dropped only "
                f"{1.0 - blurrier / sharper:.1%}: {table}"
            )


@pytest.mark.parametrize("dataset", _DATASETS)
def test_the_bundles_own_noise_alone_marks_no_tile_as_moving(dataset: str) -> None:
    case = _case(dataset, "noise_alone_marks_no_tile_as_moving")
    frames = _main_frames(dataset)
    noise = _noise_sigma(frames)
    level = float(np.median(frames[0]))
    rng = np.random.default_rng(57)
    samples = [level + rng.normal(0.0, noise, frames[0].shape) for _ in range(_SAMPLES)]

    measurement = measure_terrestrial_focus(samples, tile_size_px=_TILE_SIZE_PX)

    assert measurement.rejected_moving_tile_count == case.expect["rejected_moving_tile_count"]


@pytest.mark.parametrize("dataset", _DATASETS)
def test_a_really_changing_tile_in_the_real_frames_is_still_rejected(dataset: str) -> None:
    case = _case(dataset, "replaced_tile_is_rejected_as_moving")
    frames = [frame.copy() for frame in _main_frames(dataset)]
    baseline = measure_terrestrial_focus(frames, tile_size_px=_TILE_SIZE_PX)
    # Replace one sky tile's content in the later samples (foliage blowing into view).
    rows, cols = slice(128, 192), slice(128, 192)
    rng = np.random.default_rng(58)
    for frame in frames[1:]:
        frame[rows, cols] = rng.uniform(5000.0, 60000.0, (64, 64))

    measurement = measure_terrestrial_focus(frames, tile_size_px=_TILE_SIZE_PX)

    assert (
        measurement.rejected_moving_tile_count - baseline.rejected_moving_tile_count
        == case.expect["additional_rejected_moving_tile_count"]
    )


def test_both_datasets_name_their_bundle() -> None:
    for dataset, uuid in zip(
        _DATASETS,
        ("402eabde-d730-43b0-823d-a7f47c61f944", "f0bc314b-a889-4e18-ac9b-74971f42f014"),
        strict=True,
    ):
        manifest = load_manifest(Path(REGRESSIONS_DIR / dataset))
        assert manifest.issue == 56
        assert uuid in manifest.diagnostic_uuids


# --- review F1: one focuser position, one score -------------------------------------------


def _sample_sets() -> list[tuple[int, ...]]:
    """Every ordering and subset of the three real samples, plus duplicated re-reads."""
    sets: list[tuple[int, ...]] = []
    for size in (1, 2, 3):
        sets.extend(itertools.permutations(range(3), size))
    sets.extend([(0, 0, 0), (0, 0, 1), (1, 2, 2), (2, 1, 2, 0)])
    return sets


@pytest.mark.parametrize("dataset", _DATASETS)
def test_the_score_at_one_position_does_not_depend_on_the_sample_set(dataset: str) -> None:
    case = _case(dataset, "score_independent_of_the_sample_set")
    frames = _main_frames(dataset)

    scores = {}
    for indices in _sample_sets():
        measurement = measure_terrestrial_focus(
            [frames[index] for index in indices], tile_size_px=_TILE_SIZE_PX
        )
        assert measurement.sharpness is not None
        scores[indices] = measurement.sharpness

    spread = max(scores.values()) / min(scores.values()) - 1.0
    assert spread < case.expect["max_relative_spread"], (dataset, spread, scores)


# --- review F3: clipped highlights through the real controller + searcher ------------------

_FULL_SCALE = 65520.0
_FOCUS = 2500  # inside FakeFocuser's 0..5000
_STEPS_PER_BLUR_PX = 50.0
#: The mast and its glints (rows/cols of the 3840x2160 frame); FFTs of this crop keep a run fast.
_CROP = (slice(512, 1792), slice(768, 2816))


class _ClippedHighlightRig:
    """The real scene with its clipped glints set to K x full scale, defocused (Gaussian,
    sigma = |position - focus| / 50 px) BEFORE clipping, scaled by the exposure, noise added,
    clipped at full scale -- what a camera does to a highlight brighter than it can record."""

    def __init__(self, dataset: str, k: float, start: int) -> None:
        frames = _main_frames(dataset)
        clean = np.mean(frames, axis=0)[_CROP]
        truth = np.where(clean >= 0.999 * _FULL_SCALE, k * _FULL_SCALE, clean)
        self._shape = truth.shape
        self._spectrum = np.fft.rfft2(truth)
        self._f2 = (
            np.fft.fftfreq(truth.shape[0])[:, None] ** 2
            + np.fft.rfftfreq(truth.shape[1])[None, :] ** 2
        )
        self._noise = _noise_sigma(frames)
        self._cache: dict[tuple[int, float], np.ndarray] = {}
        self._rng = np.random.default_rng(1)
        self.focuser = FakeFocuser()
        self.focuser.move_absolute(start)
        self.exposure_ms, self.gain = 100.0, 100

    def _clean(self, position: int) -> np.ndarray:
        key = (position, self.exposure_ms)
        if key not in self._cache:
            sigma = abs(position - _FOCUS) / _STEPS_PER_BLUR_PX
            kernel = np.exp(-2.0 * (np.pi * sigma) ** 2 * self._f2)
            blurred = np.fft.irfft2(self._spectrum * kernel, s=self._shape)
            self._cache[key] = blurred * (self.exposure_ms / 100.0)
        return self._cache[key]

    def wait_for_frame(self, _reference: float, _timeout_s: float) -> FrameAcquisitionResult:
        noisy = self._clean(self.focuser.get_position()) + self._rng.normal(
            0.0, self._noise, self._shape
        )
        return FrameAcquisitionResult(
            status=FrameAcquisitionStatus.OK,
            frame=DeliveredFrame(
                pixels=np.minimum(noisy, _FULL_SCALE),
                captured_at_monotonic=0.0,
                exposure_seconds=0.3,
            ),
        )

    def exposure_control(self) -> ExposureControl:
        def apply(exposure_ms: float, gain: int) -> None:
            self.exposure_ms, self.gain = exposure_ms, gain

        return ExposureControl(get=lambda: (self.exposure_ms, self.gain), set=apply)

    def run(self, *, step_down: bool) -> AutofocusResult:
        controller = AutofocusController(
            self.focuser,
            get_frame=lambda: None,
            wait_for_frame=self.wait_for_frame,
            set_auto_exposure_paused=lambda _paused: None,
            exposure_control=self.exposure_control() if step_down else None,
            clock=FakeClock(),
        )
        return controller.run(AutofocusMode.TERRESTRIAL)


@pytest.mark.parametrize("dataset", _DATASETS)
@pytest.mark.parametrize("start_offset", [-500, 500])
@pytest.mark.parametrize("k", [1.0, 3.0])
def test_glints_up_to_3x_full_scale_are_stepped_down_and_focus_is_found(
    dataset: str, start_offset: int, k: float
) -> None:
    case = _case(dataset, "clipped_highlights_step_down")
    rig = _ClippedHighlightRig(dataset, k, _FOCUS + start_offset)

    result = rig.run(step_down=True)

    assert result.status is AutofocusStatus.SUCCESS, result
    assert result.best_position is not None
    assert abs(result.best_position - _FOCUS) <= case.expect["max_steps_off_focus"]
    assert len(result.exposure_attempts) >= 2  # it really stepped the exposure down
    assert rig.exposure_ms == 100.0  # and restored it afterwards


@pytest.mark.parametrize("dataset", _DATASETS)
def test_glints_still_clipping_after_the_step_down_are_reported_never_success(
    dataset: str,
) -> None:
    rig = _ClippedHighlightRig(dataset, 10.0, _FOCUS + 500)

    result = rig.run(step_down=True)

    assert result.status is not AutofocusStatus.SUCCESS
    assert result.failure_reason == HIGHLIGHTS_CLIPPED
    assert rig.exposure_ms == 100.0


@pytest.mark.parametrize("dataset", _DATASETS)
def test_without_exposure_control_a_clipping_dominated_run_is_never_success(
    dataset: str,
) -> None:
    rig = _ClippedHighlightRig(dataset, 3.0, _FOCUS + 500)

    result = rig.run(step_down=False)

    assert result.status is not AutofocusStatus.SUCCESS
    assert result.failure_reason == HIGHLIGHTS_CLIPPED
