# mono_adjustment_shift

Mono camera, star shifts after a simulated collimation adjustment. Source for the roi_tracker LOCKED/LOST/SEARCHING/REACQUIRED golden-master test (Stage 3).

**Status:** populated (Stage 3). Synthetic — generated via
`testing.frame_factory.single_star_image` (240x240, sigma=2.5px,
background=100 ADU), not real captured hardware frames. Six frames in
`frames/`:

| file | star | intent |
|---|---|---|
| `00_acquire.fits` | (100.0, 100.0), peak 2000 | seed via `roi_tracker.acquire()` |
| `01_locked_drift1.fits` | (102.0, 101.0) | small drift, within lock tolerance -> LOCKED |
| `02_locked_drift2.fits` | (105.0, 98.0) | still within tolerance -> LOCKED |
| `03_lost_no_star1.fits` | none (flat background) | star gone -> LOST |
| `04_lost_no_star2.fits` | none (flat background) | still gone -> SEARCHING |
| `05_reacquired.fits` | (140.0, 120.0) | reappears within search radius -> REACQUIRED |

`expected.json` — `lock_states`: the exact per-frame `TrackingState` sequence
`tests/integration/test_roi_tracker_replay.py` asserts against
(`RoiTracker(lock_tolerance_px=8.0, search_radius_px=60.0,
lost_to_searching_frames=1)`), reproducing the architecture doc's own
example: `[LOCKED, LOCKED, LOST, SEARCHING, REACQUIRED]`.
