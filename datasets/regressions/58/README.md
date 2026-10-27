# 58 â€” artificial-star autofocus with several / over-exposed stars

Real captured frames from diagnostic `4c5677d0-c97a-4a5a-9a10-86d8f71c95dd`
(git commit `daefd76adedb`, Pi field report 2026-10-25).

> Artificial-star Auto Focus fails at once (`no_usable_evidence` /
> `multiple_candidates` on the first frame, the focuser never moves) with 4
> artificial stars visible on Main.

## Provenance

- Originating issue: #58
- Diagnostic UUID(s): 4c5677d0-c97a-4a5a-9a10-86d8f71c95dd
- Git commit at capture: daefd76adedb
- Optical setup: Main optical train, ATR585M (mono, 16-bit, 3840x2160), 2000 ms,
  gain 460 for the autofocus run (470 at capture), OnStep focuser at 14780.
- **Frames are local-only (not committed â€” large test data stays local).** The
  bundle's three Main frames live in the git-ignored
  `local_test_data/58/frames/frame_0..2.fits`, full resolution, not cropped,
  lossless **uint16** (the bundle wrote float32; every value is an integer up to
  65520 â€” round trip verified bit-exact). To recreate them:

  ```
  python scripts/pull_diagnostic_bundle.py --uuid 4c5677d0-c97a-4a5a-9a10-86d8f71c95dd   # only if not on this PC
  python scripts/slim_regression_frames.py --uuid 4c5677d0 --dataset 58 --frames 0 1 2
  ```

  Without them the real-frame tests in
  `tests/regressions/test_issue_58_artificial_star_acquisition_regression.py` skip
  with that reason (CI); the bundle's Guide-camera frames (3..5) are not used.
- Raw bundle `incident.json` / `application.log`: copied under `provenance/`.
- Scene (by hand): four artificial stars, each a round clipped plateau of
  13400-14100 px (~130 px across) with a halo reaching ~200 px from its centre;
  the two lower-right ones nearly touch (their halos merge). Background median
  17744 ADU, noise sigma ~1300 ADU (MAD), 55087 pixels >= 65000 on frame_0.
- The run was in artificial-star mode while `target_mode = natural_star` (the
  hand-toggled button, fixed by #56 A) -- the frames are still a valid
  several-artificial-stars scene.

## Root cause

- `detect_sources` (shared, unchanged) switches to local-maximum detection when
  more than 25000 pixels are bright, and then splits every clipped plateau and
  its noisy halo into dozens of "stars" at least 15 px apart: 785 sources on
  frame_0 (248 saturated plateau pieces, 537 halo pieces), 769 / 774 on frames 1 / 2.
- With fewer bright pixels `detect_sources` drops any component over 5000 px, so a
  single big clipped or defocused star is not found at all (review F1); its width
  is a moment FWHM in a 37x37 px window that tops out at ~24 px, so a sweep cannot
  see big stars shrink -- the real ones read 23.5-25.1 px at every simulated
  exposure (review F2).
- `StarTargetTracker.acquire` required exactly one source (#33), so even a single
  such star gave `multiple_candidates`, and there was no rule for several stars.
- `AutofocusController`'s exposure step-down only reacted to a `saturated`
  sample after a whole search and stopped after 2 halvings
  (`max_exposure_attempts = 3`); acquisition failed before it could act.

## Fix

- `packages/astrotool_core/focus/star_candidates.py` (new, review F1/F2/F4: the
  artificial-star mode no longer uses `detect_sources`): the frame is binned to
  <= ~600k px (4x4 here) and 3x3 box-smoothed; star pixels are above background +
  5 measured smoothed-noise sigmas; one candidate per blob by topographic
  prominence -- a local maximum starts its own candidate only if the region
  around it that stays above `its level - max(9 smoothed sigma, 5 % of its
  amplitude)` reaches no brighter candidate. Halo and plateau pieces join their
  blob; the two touching stars stay separate (their saddle is 15-20 % of the
  amplitude below the plateaus; every value from 6 to 14 sigmas gives 4 on all
  three frames). Size = half-flux diameter of the blob's full-resolution pixels
  after a local-background subtraction (monotone from 3 to 150 px, clipped tops
  and C8 donuts included -- see the proof manifest). ~0.2 s per full frame.
- `StarTargetTracker`: acquires the candidate nearest the image centre (the
  green cross, #57) and reports the count; two candidates equally near the
  centre (< 2 px apart in distance) are still `multiple_candidates`. `measure()`
  analyses only a crop around the target.
- `AutofocusController`: a clipped target at acquisition -- or a clipped frame
  without a star -- steps the exposure down (halving, the shared F56 step and
  restore flag) and re-acquires after every step, at most 12 times
  (2000 ms -> 0.5 ms) or to the camera's minimum, else
  `saturated_at_min_exposure`; `target_candidates` in the result; the focuser
  panel says "following the star nearest the centre (4 found)".

Measured on the three frames: 785 / 769 / 774 `detect_sources` sources -> 4 / 4 / 4
candidates; the target is the plateau at (1450.6, 1005.5) (frame_0), 474.7 px from
the centre (candidate centre within 3 px of it).
## Fix commit

See `proofs/58-artificial-star-acquisition.toml` (`fix_commits`).

## How the expected values were derived

Each case's `rationale` in `expected.json`: the four clipped plateaus (connected
pixels >= 65000) were flood-filled and their pixel-mean centres taken by a
separate script, not by the grouping code; the target is the plateau nearest the
image centre (1919.5, 1079.5) by plain distance; all four stars are clipped, so
the acquired target must report `saturated`.
