# 56 — terrestrial autofocus on a real low-texture scene (daytime)

Real captured frames from diagnostic `402eabde-d730-43b0-823d-a7f47c61f944`
(git commit `daefd76adedb`, Pi field report 2026-10-25). Second bundle of the
same issue: `datasets/regressions/56-dusk/`.

> Auto Focus on a daytime terrestrial scene (antenna mast against bright sky,
> Main = ATR585M) fails. A normal camera's contrast autofocus would focus this scene.

## Provenance

- Originating issue: #56
- Diagnostic UUID(s): 402eabde-d730-43b0-823d-a7f47c61f944
- Git commit at capture: daefd76adedb
- Optical setup: Main optical train, ATR585M (mono, 16-bit, 3840x2160), OnStep
  focuser, terrestrial scene.
- **Frames are local-only (not committed — large test data stays local).** The
  bundle's three Main frames live in the git-ignored
  `local_test_data/56/frames/frame_0..2.fits`, full resolution, not cropped,
  lossless **uint16** (the bundle wrote float32; every value is an integer up to
  65520 — round trip verified bit-exact). To recreate them:

  ```
  python scripts/pull_diagnostic_bundle.py --uuid 402eabde-d730-43b0-823d-a7f47c61f944   # only if not on this PC
  python scripts/slim_regression_frames.py --uuid 402eabde --dataset 56 --frames 0 1 2
  ```

  Without them the real-frame tests in
  `tests/regressions/test_issue_56_terrestrial_autofocus_regression.py` skip with that
  reason (CI); the bundle's Guide-camera frames are not used.
- Raw bundle `incident.json` / `application.log`: copied under `provenance/`.
- Scene (by hand): a mostly featureless bright sky (~37000 ADU) with one mast
  and its cross bars in the centre and some foliage bottom right; slightly
  defocused. Sensor noise sigma = std(frame_1 - frame_0)/sqrt(2) = 845 ADU;
  12598 pixels clipped at 65520 (glints on the mast itself, 18 of 1980 tiles).

## Root cause

Two independent defects (both fixed under #56):

- **A — second owner of the target type.** `incident.json` shows
  `autofocus.mode = artificial_star` while `target_mode = natural_star`:
  FocuserPanel's checkable "Artificial star" button could be toggled by hand.
  Result `no_usable_evidence` / `multiple_candidates` on the first frame. Fixed
  in `apps/collimation_tool/ui/focuser_panel.py` (the button only shows the type).
- **B — noise-dominated terrestrial metric.** `measure_terrestrial_focus`
  rejected 1760 of the 1980 tiles of the real frames as "moving": it compared a
  tile's frame-to-frame change with the tile's own std, which for a featureless
  tile is the noise (mean |diff| ~ 1.13 sigma > 0.5). The score, a median over
  the remaining mostly-noise tiles, was flat and non-monotone under added blur.
  Fixed in `packages/astrotool_core/focus/terrestrial_focus_metric.py`: 4x4
  binning; motion = largest change between any two samples clearly above the
  frame's noise-only change AND above 1.0 x the tile's own std (replaced content,
  not shimmer); per-tile energy = mean of the per-sample energies (order
  independent); score = the strongest 3 % static tiles minus the measured
  sensor-noise energy.

Review fixes (F1-F3, contract amendment 2026-10-25):

- **F1** the score at one position depended on sample order and duplicates
  (27-58 % spread): now every ordering, subset and duplicate of the 3 real
  frames agrees within 4.4 % (the frames' own frame-to-frame variation).
- **F2** `AutofocusController` re-read `get_frame()` for the extra samples (the
  same latest frame at 0.3-2 s exposures): now three distinct fresh frames.
- **F3** clipped tiles carry 72-80 % of this scene's score: the controller now
  steps the exposure down while clipped tiles carry more than half of it and
  reports `highlights_clipped` if that does not help.

Measured with the regression test's own sweep (mean of the 3 real frames,
exact Gaussian blur, fresh noise at the bundle's sigma, seed 56):

| added blur | old score | old moving tiles | new score | new step drop |
|---|---|---|---|---|
| 0 px | 1.71e7 | 1688 | 2.83e8 | |
| 2 px | 2.02e7 | 1740 | 2.73e8 | 3.6 % |
| 4 px | 2.02e7 | 1736 | 2.53e8 | 7.3 % |
| 8 px | 2.01e7 | 1741 | 2.04e8 | 19.5 % |
| 16 px | 1.98e7 | 1748 | 1.28e8 | 37.0 % |
| 32 px | 1.90e7 | 1784 | 5.35e7 | 58.3 % |

New metric, 0 tiles moving at every level; noise-only frames: 0 moving (old: 1980).
The 3 real frames: 54 of 1980 tiles rejected as moving (the foliage bottom right).

## Saturation

The clipped pixels are glints on the mast, i.e. on the only detail; clipped tiles
carry 72-80 % of the score (top-3 % selection). Excluding them (or masking the
clipped pixels) made the blur-sweep curve flat/non-monotone (tile exclusion: the
score *rises* by 13 % and 17 % at 2 and 4 px, because the excluded set changes
with blur) — worse than keeping them. A blur-before-clip simulation (clipped
pixels set to K x 65520, blurred, re-clipped) is monotone for K = 1, but for
K >= 3 the score *rises* with small blur for every metric variant tried: a
highlight far above the clip level grows a halo when defocused. Handled in the
controller (review F3): exposure step-down, then `highlights_clipped`. Driven
through the real controller + searcher, K = 1 and K = 3 land on focus after the
step-down; K = 10 is reported as `highlights_clipped` (see the regression test).

## Fix commit

See `proofs/56-terrestrial-autofocus.toml` (`fix_commits`).

## How the expected values were derived

Each case's `rationale` in `expected.json`: added Gaussian blur is more defocus by
construction (known software transform, AC ">= 5 % per step from 4 px on" from
issue #56); constant + the bundle's own measured noise has no motion by
construction; a tile whose pixels are replaced in later samples changed by
construction; samples from one position measure one focus by construction; the
K-simulation's optimum is set by construction. None of the expectations comes
from the metric's own output.
