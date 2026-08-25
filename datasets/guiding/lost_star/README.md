# lost_star

Guide star lost mid-session and reacquired. Exercises roi_tracker's LOST/SEARCHING/REACQUIRED path from the guide side (Stage 6).

**Status:** populated (Stage 6). Synthetic — generated via
`testing.frame_factory.single_star_image` (240x240, sigma=2.5px,
background=100 ADU), not real captured hardware frames. Five frames in
`frames/`:

| file | star | intent |
|---|---|---|
| `00_acquire.fits` | (100.0, 100.0), peak 2000 | seed via `RoiTracker.acquire()` — target set to this position |
| `01_locked_drift.fits` | (102.0, 101.0) | small drift, within lock tolerance -> LOCKED |
| `02_lost1.fits` | none (flat background) | star gone -> LOST |
| `03_lost2_searching.fits` | none (flat background) | still gone -> SEARCHING |
| `04_reacquired.fits` | (130.0, 110.0) | reappears within search radius -> REACQUIRED |

`expected.json` — `states`: the `TrackingState` sequence for frames 1-4
(`[LOCKED, LOST, SEARCHING, REACQUIRED]`); `errors`: the corresponding
`GuideError.error_x`/`error_y` for accepted frames, `null` for
LOST/SEARCHING (rejected as `"star_lost"`). Read by
`tests/integration/test_guide_lost_star_replay.py`, which drives
`detect_sources` + `RoiTracker` + `compute_guide_error` synchronously
(bypassing `GuideController`'s threaded loop/lossy mailbox, for the same
determinism reason as Stage 3's `test_roi_tracker_replay.py`) and asserts
against both fields within tolerance.
