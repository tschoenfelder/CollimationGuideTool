# 28 — real-frame translation-estimator corpus

Issue #28. A gated corpus for **proving one translation estimator offline**
before it is trusted in the calibration path, plus issue #32's requirement that
the *same* production interface handle both textured-terrestrial and
sparse-star frames.

This directory is the **scaffold only** — the case structure and expectations
are committed; the frames are not yet. Every case input is a
`local_test_data/28_corpus/...` pointer, so on any checkout without that
(git-ignored) tree the whole dataset **skips cleanly**. It graduates case by
case as real frames are dropped in.

## The four categories (from the issue)

| category | cases here | how to populate `local_test_data/28_corpus/` |
|---|---|---|
| **Stationary / pairwise ≈0** | `stationary_{main,guide}_pair_00_01_is_zero` | 10 consecutive same-camera frames per camera, rig static, mount not commanded. Save as `stationary/main_00.fits …_09.fits`, `stationary/guide_00.fits …`. Add more pairwise cases (`00_02`, `03_07`, …) — all expect `dx=dy=0` within ~1.5 px. |
| **Known 10…1000 px software shift** | `known_shift_guide_x_120px`, `known_shift_guide_xy_640_360px` | `known_shift/guide_base.fits` = one real Guide frame; `guide_roll_x120.fits` = `numpy.roll(base, 120, axis=1)`; `guide_roll_x640_y360.fits` = `numpy.roll(base, (360, 640), axis=(0, 1))`. Add shifts across the full 10–1000 px range and for Main. |
| **Real physical before/after** | `real_physical_guide_axis1_af7d27b7` | Copy the real pulse pair from `local_test_data/af7d27b7_terrestrial_calibration_2026-09-10/frames/guide_axis1_{before,after}.fits` to `real_pairs/`. Expected `(dx, dy)` comes from that bundle's `incident.json`, cross-checked non-degenerate — never from the estimator. |
| **Sparse star / point source (#32)** | `star_field_guide_pair_through_same_interface` | A star-mode before/after pair (e.g. from `08d264f8` / `12bea18a`) to `star/`. Measure the true shift by hand from star centroids, then add `dx_px` / `dy_px` to the case's `expect`. |

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

The star case runs through the **same `measure_translation_offset`** as the
terrestrial cases: no separate star-mode boundary. If that function needs to
dispatch internally (textured NCC vs. point-source matching) that is an
implementation detail behind the one entry point, and this dataset is where it
is proven.
