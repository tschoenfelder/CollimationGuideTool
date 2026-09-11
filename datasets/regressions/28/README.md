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

**Status (2026-09-11): 5 of 6 cases populated and passing** against real data
(`local_test_data/28_corpus/README.md` has the provenance + the independent
verification of every `expect` before it was written). Still open: a genuinely
well-exposed Main stationary pair — see that README's "Known gap" section.

## The four categories (from the issue)

| category | cases here | status | remaining population |
|---|---|---|---|
| **Stationary / pairwise ≈0** | `stationary_{main,guide}_pair_00_01_is_zero` | Guide populated + passing (real `(0,0)`). **Main still open** — the only Main frames pulled so far are too underexposed to even confirm zero (`measure_translation_offset` returns `None`, not `(0,0)`); needs a genuinely well-exposed Main pair, not a re-use of the tracked underexposure incidents. | More pairs welcome: `stationary/main_00.fits …_09.fits`, `guide_02.fits …` per camera, plus more pairwise cases (`00_02`, `03_07`, …). |
| **Known 10…1000 px software shift** | `known_shift_guide_x_120px`, `known_shift_guide_xy_640_360px` | Populated + passing (exact recovery, score ~1.0). | Add shifts across the full range and for Main: `known_shift/guide_base.fits` = one real Guide frame; `guide_roll_x120.fits` = `numpy.roll(base, 120, axis=1)`; `guide_roll_x640_y360.fits` = `numpy.roll(base, (360, 640), axis=(0, 1))`. |
| **Real physical before/after** | `real_physical_guide_axis1_af7d27b7` | Populated + passing — matches `af7d27b7`'s own `incident.json` `calibration.right.axis1` exactly. | Add more real pulse pairs (Main's, other axes, other bundles) to `real_pairs/`. |
| **Sparse star / point source (#32)** | `star_field_guide_pair_through_same_interface` | Populated + passing (`match: true` only — no independent hand-measured centroid shift yet, so `dx_px`/`dy_px` are deliberately not pinned). | Hand-measure the true shift from star centroids in `star/guide_star_{before,after}.fits`, then add `dx_px`/`dy_px` to `expect`. Add a Main star pair once one exists. |

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
