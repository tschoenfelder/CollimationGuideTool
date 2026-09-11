# 28 — real-frame translation-estimator corpus

Issue #28. A gated corpus for **proving one translation estimator offline**
before it is trusted in the calibration path, plus issue #32's requirement that
the *same* production interface handle both textured-terrestrial and
sparse-star frames.

This directory holds the case structure and expectations; the frames
themselves are not committed. Every case input is a
`local_test_data/28_corpus/...` pointer, so on any checkout without that
(git-ignored) tree the whole dataset **skips cleanly** — it graduates case by
case as real frames are dropped in.

**Status (2026-09-11): all 11 of 11 cases populated and passing** against real
data (`local_test_data/28_corpus/README.md` has the provenance + the
independent verification of every `expect` before it was written, plus the
full-grid benchmark findings). Both stationary cases (Main included, real
10-frame sets for both cameras) closed the same day, captured directly by
connecting to the cameras over SSH — see that README's own follow-up section
for the real-drift-on-Main-only finding this surfaced (small, genuine,
outdoor-daytime environmental motion, NOT mount motion — confirmed via a
mount-status check — and NOT visible to Guide's coarser plate scale).

**Issue #32 finding (2026-09-11): no algorithm change was needed.** The
existing, unmodified `measure_translation_offset()` recovers the full shared
28-case shift grid on real sparse/star-content frames exactly as well as on
real textured frames — 0 rejections, 0 wrong displacements, on either
content class. See `local_test_data/28_corpus/README.md`'s "Key finding"
section and `scripts/translation_estimator_benchmark.py`'s output for the
full evidence. Issue #32's remaining acceptance criteria that aren't yet
closed are real-data gaps (a full 10-frame real stationary star set; a real
UUID where star-mode calibration failed despite an operator-visible shift),
not algorithm gaps.

## The four categories (from the issue)

| category | cases here | status | remaining population |
|---|---|---|---|
| **Stationary / pairwise ≈0** | `stationary_{main,guide}_pair_00_01_is_zero` | **Both populated + passing.** Full real 10-frame sets exist for both cameras (`stationary/{main,guide}_00..09.fits`) — the 45-pair sweep runs via `scripts/translation_estimator_benchmark.py`. Guide: perfect `(0,0)` on all 45 pairs. Main: real small motion (median 2px, max 5px) traced to outdoor environmental content change, not mount motion (mount status confirmed untouched) — tolerance derived from this real spread, not invented. | More pairwise cases (`00_02`, `03_07`, …) welcome — the harness already runs the full sweep, just not every pair is individually gated. |
| **Known 10…1000 px software shift** | `known_shift_guide_x_120px`, `known_shift_guide_xy_640_360px`, `known_shift_guide_x_small_pos_10px` (+ 26 more real-frame shifts locally, see below) | Populated + passing (exact recovery, score ~1.0). The full 28-case shared grid (`astrotool_core.testing.shift_grid.KNOWN_SHIFT_GRID`) is generated against the real Guide base frame; a curated few are gated here, the rest verified via `tests/local_data/` + the benchmark script. | Add the same grid for Main once a well-exposed Main base frame exists. |
| **Real physical before/after** | `real_physical_guide_axis1_af7d27b7`, `real_physical_main_axis1_run1`, `real_physical_main_axis2_run3` | Populated + passing — each matches its own bundle's `incident.json` calibration value, cross-checked non-degenerate against the orthogonal axis. Now covers **both cameras** (Main coverage added 2026-09-11 from 3 live pulse-settle-fix verification runs; see `local_test_data/28_corpus/README.md`'s own follow-up section, incl. one run that failed with an intermittent false-zero on both Main axes — real evidence for the still-open mystery in [[project_ba2b3259_pulse_settle_and_false_zero]]). | Add more real pulse pairs (other axes/bundles) to `real_pairs/`. |
| **Sparse star / point source (#32)** | `star_field_guide_pair_through_same_interface`, `star_known_shift_guide_x_small_pos_10px`, `star_known_shift_guide_diag_unequal_pp_reads_as_its_exact_alias` | Populated + passing. The real motion pair still only pins `match: true` (no independent hand-measured centroid shift yet); the shared-grid cases pin exact `dx_px`/`dy_px` (or the documented alias) since those are numpy.roll-derived, independent of the estimator. | Hand-measure the real motion pair's true shift from star centroids, add `dx_px`/`dy_px`. Add a Main star pair once one exists. |

## Candidate comparison

Issue #28 includes **evaluating candidate estimators**, not only tuning the
current one. `boundary` here is `measure_translation_offset` (the incumbent).
To score an alternative:

1. Register it in `tests/regressions/_loader.py`'s `BOUNDARIES`
   (e.g. `"phase_correlation_offset"`), returning the same
   `{match, dx_px, dy_px, score}` shape.
2. Either point a **copy** of this dataset (`datasets/regressions/28-<name>/`)
   at it, or add a small comparison driver that runs every registered
   translation boundary over this corpus and reports a per-category scoreboard.
   The corpus (inputs + independently-authored expectations) is the fixed part;
   the estimator under test is the variable.

## #32 — one production interface

The star cases run through the **same `measure_translation_offset`** as the
terrestrial cases: no separate star-mode boundary, and (2026-09-11 finding)
no internal dispatch was needed either — the existing NCC + x8-downsample
tiers already handle sparse/point-source content as well as textured content,
verified against the full shared shift grid on real frames. This dataset (and
the equivalent synthetic coverage in `tests/core/target/
test_translation_offset.py`'s `TestSparseStarContent`) is what proved that,
so a future change here should re-run both before assuming a new strategy is
required.

## Benchmark / comparison reporting

`python scripts/translation_estimator_benchmark.py` discovers every camera
dataset under `local_test_data/28_corpus/` (stationary pairwise sweep + the
shared known-shift grid against both `known_shift/` and `star/`), classifies
every case (`correct_match` / `low_confidence_rejection` / `aliased` — an
expected large-shift circular-wrap, not a defect, see
`max_unaliased_shift_px`'s own docstring / `wrong_displacement`), and writes
`docs/quality/translation_estimator_benchmark.{json,md}` — issue #28's
required machine-readable report, and the tool for its "change ONLY the
estimator, rerun, compare" optimization workflow. Measurement-only, same
philosophy as `scripts/quality_report.py`; enforces no threshold, never gates
a commit on its own.
