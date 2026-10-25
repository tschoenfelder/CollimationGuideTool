# 56-dusk — terrestrial autofocus does not move the focuser (dusk)

Real captured frames from diagnostic `f0bc314b-a889-4e18-ac9b-74971f42f014`
(git commit `daefd76adedb`). Second bundle of issue #56; the daytime bundle and
the full root-cause write-up are in `datasets/regressions/56/README.md`.

> autofocus with artificial star off doesn't move focus at all

**Stale autofocus record.** The incident's `context.autofocus` is the EARLIER
artificial-star run (`mode = artificial_star`, start 14461, one exposure attempt
of 18.575 ms, `multiple_candidates`) — not the run the user reported. The
terrestrial run was still in progress when the bundle was captured
(auto-exposure paused, Main at 2.0 s); the frames are what that terrestrial run
was measuring, but its own result is not in the bundle.

## Provenance

- Originating issue: #56
- Diagnostic UUID(s): f0bc314b-a889-4e18-ac9b-74971f42f014
- Git commit at capture: daefd76adedb
- Optical setup: Main optical train, ATR585M (mono, 16-bit, 3840x2160), 2.0 s
  exposure at dusk, OnStep focuser, terrestrial scene.
- **Frames are local-only (not committed — large test data stays local).** The
  bundle's three Main frames live in the git-ignored
  `local_test_data/56-dusk/frames/frame_0..2.fits`, full resolution, not cropped,
  lossless **uint16** (the bundle wrote float32; every value is an integer up to
  65520 — round trip verified bit-exact). To recreate them:

  ```
  python scripts/pull_diagnostic_bundle.py --uuid f0bc314b-a889-4e18-ac9b-74971f42f014   # only if not on this PC
  python scripts/slim_regression_frames.py --uuid f0bc314b --dataset 56-dusk --frames 0 1 2
  ```

  Without them the real-frame tests in
  `tests/regressions/test_issue_56_terrestrial_autofocus_regression.py` skip with that
  reason (CI); the bundle's Guide-camera frames are not used.
- Raw bundle `incident.json` / `application.log`: copied under `provenance/`.
- By hand: mean ~24000 ADU, noise sigma = std(frame_1 - frame_0)/sqrt(2) =
  829 ADU, 50342 pixels clipped at 65520 (on the mast, 36 of 1980 tiles).

## Root cause

Defect B of #56 (see `../56/README.md`): the old metric rejected 1839 of 1980
tiles as moving and its score was non-monotone under added blur, so the
search's probe saw no 5 % improvement in either direction and returned to the
start ("success" without a focus change).

| added blur | old score | old moving tiles | new score | new step drop |
|---|---|---|---|---|
| 0 px | 1.67e7 | 1826 | 6.02e8 | |
| 2 px | 1.86e7 | 1865 | 5.66e8 | 6.0 % |
| 4 px | 1.85e7 | 1865 | 4.94e8 | 12.7 % |
| 8 px | 1.83e7 | 1867 | 3.67e8 | 25.8 % |
| 16 px | 1.83e7 | 1873 | 2.23e8 | 39.3 % |
| 32 px | 2.25e7 | 1909 | 9.57e7 | 57.0 % |

New metric: 0 tiles moving at every level, on the 3 real frames and on noise
alone; every ordering/subset/duplicate of the 3 frames within 2.5 %.

Saturation is heavier here: clipped tiles carry 91-93 % of the score. Handled by
the controller's exposure step-down / `highlights_clipped` (review F3) — see
`../56/README.md` "Saturation".

## Fix commit

See `proofs/56-terrestrial-autofocus.toml` (`fix_commits`).

## How the expected values were derived

As for `../56/`: blur = more defocus (known transform), constant + measured
noise = no motion, replaced tile = changed content, one position = one focus,
K-simulation optimum set by construction — never from the metric's output.
