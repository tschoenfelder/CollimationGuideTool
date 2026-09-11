"""Real-frame corpus benchmark/comparison report for the translation
estimator (issue #28's required reporting; issue #32's shared grid).

Measurement-first, like `scripts/quality_report.py`: never enforces a
threshold or changes production code -- only measures and reports, so
issue #28's own "change ONLY the estimator, rerun, compare" optimization
workflow has something objective to compare a change against.

Usage:
    python scripts/translation_estimator_benchmark.py

Discovers every camera dataset under `local_test_data/28_corpus/` by
filename prefix -- not hardcoded to Main/Guide (issue #28's own discovery
AC) -- and runs:

- the stationary-pairwise sweep (`stationary/<camera>_NN.fits`, issue #28
  Test Group A: every unique pair, `dx≈0, dy≈0` expected);
- the known-shift grid sweep, against both a textured base
  (`known_shift/<camera>_base.fits`, issue #28 Test Group B) and a sparse
  star-content base (`star/<camera>_star_base.fits`, issue #32 Test A),
  applying `astrotool_core.testing.shift_grid.KNOWN_SHIFT_GRID` via
  `numpy.roll` so the expected displacement is defined by the transform
  itself, never by the estimator (issue #28's explicit requirement).

Every case is classified `correct_match` / `low_confidence_rejection` /
`aliased` / `wrong_displacement` (see `_classify`'s own docstring for the
`aliased` category -- a shift beyond `max_unaliased_shift_px` reading back
as its own exact circular alias is expected behavior, not a defect, per
issue #28's own "may expose a valid operating limit rather than a defect"
edge consideration; it is reported separately from a genuinely wrong
answer, never folded into "correct").

Writes `docs/quality/translation_estimator_benchmark.json` (every case) and
`.md` (the aggregate summary), and prints the same summary to stdout. Skips
cleanly (prints a note, exits 0, writes nothing) if
`local_test_data/28_corpus/` isn't present locally.
"""

from __future__ import annotations

import itertools
import json
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from statistics import mean, median

import numpy as np
from astropy.io import fits
from astrotool_core.target.translation_offset import (
    TranslationOffset,
    measure_translation_offset_with_tier,
)
from astrotool_core.testing.shift_grid import KNOWN_SHIFT_GRID, ShiftCase

REPO_ROOT = Path(__file__).resolve().parent.parent
CORPUS_DIR = REPO_ROOT / "local_test_data" / "28_corpus"
OUT_DIR = REPO_ROOT / "docs" / "quality"

_STATIONARY_RE = re.compile(r"^(?P<camera>[a-z0-9]+)_(?P<index>\d{2})\.fits$")
_KNOWN_SHIFT_BASE_RE = re.compile(r"^(?P<camera>[a-z0-9]+)_base\.fits$")
_STAR_BASE_RE = re.compile(r"^(?P<camera>[a-z0-9]+)_star_base\.fits$")

#: Tolerance for "correct" (exact numpy.roll shifts recover exactly; a
#: small margin covers the stationary-pair case, where the true shift is
#: zero by construction but real sensor noise moves the measured peak by a
#: pixel or so -- see datasets/regressions/28/expected.json's own
#: currently-committed ±1.5px, the interim value pending a full 10-frame
#: empirical-spread derivation, issue #28's own requirement).
_KNOWN_SHIFT_TOLERANCE_PX = 1.0
_STATIONARY_TOLERANCE_PX = 1.5

Outcome = str  # "correct_match" | "low_confidence_rejection" | "aliased" | "wrong_displacement"


@dataclass
class CaseResult:
    camera: str
    category: str  # "stationary" | "known_shift" | "star"
    dataset: str
    applied_dx: float
    applied_dy: float
    measured_dx: float | None
    measured_dy: float | None
    score: float | None
    outcome: Outcome
    tier: str
    runtime_ms: float


def _load(path: Path) -> np.ndarray:
    return np.asarray(fits.getdata(path), dtype=np.float32)


def _expected_wrapped(applied: int, dimension: int) -> float:
    """What `measure_translation_offset()` reports for an `applied` shift
    along one axis of a frame `dimension` pixels wide/tall, once the shift
    exceeds `max_unaliased_shift_px` -- see that function's own docstring
    (`astrotool_core.target.translation_offset`) for the real evidence this
    formula reproduces exactly."""
    wrapped = applied % dimension
    return float(wrapped if wrapped <= dimension // 2 else wrapped - dimension)


def _classify(
    offset: TranslationOffset | None,
    applied_dx: int,
    applied_dy: int,
    shape: tuple[int, int],
    tolerance_px: float,
) -> Outcome:
    """`"correct_match"` (within tolerance of the applied shift),
    `"low_confidence_rejection"` (estimator returned `None`), `"aliased"`
    (within tolerance of the *exact circular alias* of a shift beyond
    `max_unaliased_shift_px` -- an inherent property of any content this
    module measures at a shift this large relative to the frame, per issue
    #28's own "may expose a valid operating limit rather than a defect";
    reported separately, never folded into `"correct_match"`), or
    `"wrong_displacement"` (neither -- a genuine defect, at any content
    class or magnitude, per issue #28's explicit taxonomy)."""
    if offset is None:
        return "low_confidence_rejection"
    if abs(offset.dx_px - applied_dx) <= tolerance_px and abs(
        offset.dy_px - applied_dy
    ) <= tolerance_px:
        return "correct_match"
    expected_dx = _expected_wrapped(applied_dx, shape[1])
    expected_dy = _expected_wrapped(applied_dy, shape[0])
    if abs(offset.dx_px - expected_dx) <= tolerance_px and abs(
        offset.dy_px - expected_dy
    ) <= tolerance_px:
        return "aliased"
    return "wrong_displacement"


def _measure_case(
    before: np.ndarray,
    after: np.ndarray,
    applied_dx: int,
    applied_dy: int,
    *,
    camera: str,
    category: str,
    dataset: str,
    tolerance_px: float,
) -> CaseResult:
    started = time.perf_counter()
    offset, tier = measure_translation_offset_with_tier(before, after)
    runtime_ms = (time.perf_counter() - started) * 1000.0
    outcome = _classify(offset, applied_dx, applied_dy, before.shape, tolerance_px)
    return CaseResult(
        camera=camera,
        category=category,
        dataset=dataset,
        applied_dx=float(applied_dx),
        applied_dy=float(applied_dy),
        measured_dx=offset.dx_px if offset is not None else None,
        measured_dy=offset.dy_px if offset is not None else None,
        score=offset.score if offset is not None else None,
        outcome=outcome,
        tier=tier,
        runtime_ms=round(runtime_ms, 2),
    )


def discover_stationary_datasets(corpus_dir: Path) -> dict[str, list[Path]]:
    """`{camera: [frame paths...]}` for every `stationary/<camera>_NN.fits`
    found -- any filename prefix is a "camera", not hardcoded to Main/Guide
    (issue #28's own discovery AC)."""
    stationary_dir = corpus_dir / "stationary"
    by_camera: dict[str, list[Path]] = {}
    if not stationary_dir.is_dir():
        return by_camera
    for path in sorted(stationary_dir.glob("*.fits")):
        match = _STATIONARY_RE.match(path.name)
        if match:
            by_camera.setdefault(match.group("camera"), []).append(path)
    return by_camera


def discover_known_shift_bases(corpus_dir: Path) -> dict[str, Path]:
    """`{camera: base frame path}` for every `known_shift/<camera>_base.fits`."""
    shift_dir = corpus_dir / "known_shift"
    bases: dict[str, Path] = {}
    if shift_dir.is_dir():
        for path in sorted(shift_dir.glob("*_base.fits")):
            match = _KNOWN_SHIFT_BASE_RE.match(path.name)
            if match:
                bases[match.group("camera")] = path
    return bases


def discover_star_bases(corpus_dir: Path) -> dict[str, Path]:
    """`{camera: base frame path}` for every `star/<camera>_star_base.fits`."""
    star_dir = corpus_dir / "star"
    bases: dict[str, Path] = {}
    if star_dir.is_dir():
        for path in sorted(star_dir.glob("*_star_base.fits")):
            match = _STAR_BASE_RE.match(path.name)
            if match:
                bases[match.group("camera")] = path
    return bases


def run_stationary_sweep(corpus_dir: Path) -> list[CaseResult]:
    """Issue #28 Test Group A: every unique pair within each camera's
    stationary set (45 for a full 10-frame set; fewer datasets report
    fewer pairs rather than failing -- the corpus doesn't yet have 10
    frames for every camera, see local_test_data/28_corpus/README.md's own
    "Known gap" section)."""
    results: list[CaseResult] = []
    for camera, frames in discover_stationary_datasets(corpus_dir).items():
        for path_a, path_b in itertools.combinations(sorted(frames), 2):
            before, after = _load(path_a), _load(path_b)
            results.append(
                _measure_case(
                    before,
                    after,
                    0,
                    0,
                    camera=camera,
                    category="stationary",
                    dataset=f"{path_a.stem}_vs_{path_b.stem}",
                    tolerance_px=_STATIONARY_TOLERANCE_PX,
                )
            )
    return results


def _known_shift_sweep_for(
    bases: dict[str, Path], *, category: str, grid: tuple[ShiftCase, ...]
) -> list[CaseResult]:
    results: list[CaseResult] = []
    for camera, base_path in bases.items():
        base = _load(base_path)
        for case in grid:
            after = (
                base.copy()
                if case.dx == 0 and case.dy == 0
                else np.roll(base, shift=(case.dy, case.dx), axis=(0, 1))
            )
            results.append(
                _measure_case(
                    base,
                    after,
                    case.dx,
                    case.dy,
                    camera=camera,
                    category=category,
                    dataset=case.name,
                    tolerance_px=_KNOWN_SHIFT_TOLERANCE_PX,
                )
            )
    return results


def run_known_shift_sweep(corpus_dir: Path) -> list[CaseResult]:
    """Issue #28 Test Group B (`known_shift/`, textured content) and issue
    #32 Test A (`star/`, sparse/point-source content) -- the same shared
    `KNOWN_SHIFT_GRID`, applied to whichever base frames are present."""
    return _known_shift_sweep_for(
        discover_known_shift_bases(corpus_dir), category="known_shift", grid=KNOWN_SHIFT_GRID
    ) + _known_shift_sweep_for(
        discover_star_bases(corpus_dir), category="star", grid=KNOWN_SHIFT_GRID
    )


def run_benchmark(corpus_dir: Path = CORPUS_DIR) -> list[CaseResult]:
    return run_stationary_sweep(corpus_dir) + run_known_shift_sweep(corpus_dir)


def _outcome_counts(results: list[CaseResult]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for result in results:
        counts[result.outcome] = counts.get(result.outcome, 0) + 1
    return counts


def _error_stats(results: list[CaseResult]) -> dict[str, float] | None:
    """Mean/median/max absolute X/Y error, `correct_match` cases only --
    an `aliased` or rejected case has no meaningful "error" against the
    applied shift by definition."""
    correct = [r for r in results if r.outcome == "correct_match"]
    if not correct:
        return None
    x_errors = [abs((r.measured_dx or 0.0) - r.applied_dx) for r in correct]
    y_errors = [abs((r.measured_dy or 0.0) - r.applied_dy) for r in correct]
    return {
        "mean_abs_x_error_px": round(mean(x_errors), 4),
        "median_abs_x_error_px": round(median(x_errors), 4),
        "max_abs_x_error_px": round(max(x_errors), 4),
        "mean_abs_y_error_px": round(mean(y_errors), 4),
        "median_abs_y_error_px": round(median(y_errors), 4),
        "max_abs_y_error_px": round(max(y_errors), 4),
    }


def _magnitude_bucket(dx: float, dy: float) -> str:
    magnitude = max(abs(dx), abs(dy))
    if magnitude == 0:
        return "zero"
    if magnitude < 64:
        return "small"
    if magnitude <= 512:
        return "medium"
    return "large"


@dataclass
class ScoreDistribution:
    count: int
    mean: float | None
    min: float | None
    max: float | None


@dataclass
class CameraSummary:
    cases_tested: int
    outcome_counts: dict[str, int]
    error_stats: dict[str, float] | None


@dataclass
class RuntimeSummary:
    total_ms: float
    mean_per_case_ms: float | None


@dataclass
class BenchmarkSummary:
    total_cases: int
    outcome_counts: dict[str, int]
    error_stats: dict[str, float] | None
    score_distribution: ScoreDistribution
    outcome_by_magnitude_bucket: dict[str, dict[str, int]]
    per_camera: dict[str, CameraSummary]
    runtime: RuntimeSummary


def build_summary(results: list[CaseResult]) -> BenchmarkSummary:
    scores = [r.score for r in results if r.score is not None]
    runtimes = [r.runtime_ms for r in results]

    by_bucket: dict[str, dict[str, int]] = {}
    for result in results:
        bucket = _magnitude_bucket(result.applied_dx, result.applied_dy)
        by_bucket.setdefault(bucket, {})
        by_bucket[bucket][result.outcome] = by_bucket[bucket].get(result.outcome, 0) + 1

    per_camera: dict[str, CameraSummary] = {}
    for camera in sorted({r.camera for r in results}):
        camera_results = [r for r in results if r.camera == camera]
        per_camera[camera] = CameraSummary(
            cases_tested=len(camera_results),
            outcome_counts=_outcome_counts(camera_results),
            error_stats=_error_stats(camera_results),
        )

    return BenchmarkSummary(
        total_cases=len(results),
        outcome_counts=_outcome_counts(results),
        error_stats=_error_stats(results),
        score_distribution=ScoreDistribution(
            count=len(scores),
            mean=round(mean(scores), 4) if scores else None,
            min=round(min(scores), 4) if scores else None,
            max=round(max(scores), 4) if scores else None,
        ),
        outcome_by_magnitude_bucket=by_bucket,
        per_camera=per_camera,
        runtime=RuntimeSummary(
            total_ms=round(sum(runtimes), 2),
            mean_per_case_ms=round(mean(runtimes), 4) if runtimes else None,
        ),
    )


def _render_markdown(summary: BenchmarkSummary) -> str:
    lines = [
        "# Translation estimator benchmark",
        "",
        "Generated by `scripts/translation_estimator_benchmark.py` (issues #28/#32). "
        "Measurement only -- no threshold is enforced; compare this report before/after "
        "an estimator change (issue #28's own optimization workflow).",
        "",
        f"**{summary.total_cases} cases** across {len(summary.per_camera)} camera dataset(s).",
        "",
        "## Outcome counts",
        "",
        "| outcome | count |",
        "|---|---|",
    ]
    for outcome, count in summary.outcome_counts.items():
        lines.append(f"| `{outcome}` | {count} |")
    lines.append("")
    lines.append(
        "`wrong_displacement` is the one outcome that must never occur, at any content "
        "class or magnitude. `aliased` (a shift beyond the frame's own unaliased range "
        "reading back as its exact circular equivalent) is expected, documented behavior "
        "-- see `max_unaliased_shift_px`'s own docstring -- not a defect."
    )
    lines.append("")
    lines.append("## Outcome by shift magnitude")
    lines.append("")
    outcome_keys = sorted(summary.outcome_counts.keys())
    lines.append("| bucket | " + " | ".join(outcome_keys) + " |")
    lines.append("|---|" + "---|" * len(outcome_keys))
    for bucket in ("zero", "small", "medium", "large"):
        row = summary.outcome_by_magnitude_bucket.get(bucket, {})
        lines.append(f"| {bucket} | " + " | ".join(str(row.get(k, 0)) for k in outcome_keys) + " |")
    lines.append("")
    if summary.error_stats is not None:
        stats = summary.error_stats
        lines.append("## Error stats (correct_match cases only)")
        lines.append("")
        lines.append(
            f"mean abs error: X={stats['mean_abs_x_error_px']}px, "
            f"Y={stats['mean_abs_y_error_px']}px -- "
            f"max: X={stats['max_abs_x_error_px']}px, Y={stats['max_abs_y_error_px']}px"
        )
        lines.append("")
    lines.append("## Per camera")
    lines.append("")
    lines.append("| camera | cases | outcomes |")
    lines.append("|---|---|---|")
    for camera, data in summary.per_camera.items():
        outcomes = ", ".join(f"{k}={v}" for k, v in data.outcome_counts.items())
        lines.append(f"| {camera} | {data.cases_tested} | {outcomes} |")
    lines.append("")
    lines.append("Full per-case data: `docs/quality/translation_estimator_benchmark.json`.")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    if not CORPUS_DIR.is_dir():
        print(
            f"local_test_data/28_corpus/ not present at {CORPUS_DIR} -- nothing to benchmark. "
            "See local_test_data/28_corpus/README.md for how to populate it.",
        )
        return 0

    results = run_benchmark()
    if not results:
        print(f"{CORPUS_DIR} exists but has no discoverable datasets -- nothing to benchmark.")
        return 0

    summary = build_summary(results)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "translation_estimator_benchmark.json").write_text(
        json.dumps(
            {"summary": asdict(summary), "cases": [asdict(r) for r in results]}, indent=2
        )
        + "\n",
        encoding="utf-8",
    )
    markdown = _render_markdown(summary)
    (OUT_DIR / "translation_estimator_benchmark.md").write_text(markdown, encoding="utf-8")
    print(markdown)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
