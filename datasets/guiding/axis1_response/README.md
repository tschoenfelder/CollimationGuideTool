# axis1_response

Pulse-and-measure response frames for mount axis 1, for mount/axis_calibration.py (Stage 4).

**Status:** populated (Stage 4). Synthetic — generated via
`testing.frame_factory.single_star_image` (240x240, sigma=2.5px,
background=100 ADU), not real captured hardware frames. Four frames in
`frames/`, forming two before/after pairs (a 500ms pulse each):

| file | star | intent |
|---|---|---|
| `00_before_pos.fits` | (100.0, 100.0) | before the AXIS1 POSITIVE pulse |
| `01_after_pos.fits` | (120.0, 100.0) | after — +20px in x |
| `02_before_neg.fits` | (100.0, 100.0) | before the AXIS1 NEGATIVE pulse |
| `03_after_neg.fits` | (80.0, 100.0) | after — -20px in x |

`expected.json` — `pulse_ms` (500) and `responses` keyed by
`"{AXIS}_{DIRECTION}"`, each giving `dx_px`/`dy_px`/`px_per_ms`. Read by
`tests/integration/test_axis_calibration_replay.py`, which drives
`mount.axis_calibration.calibrate_axes` through a `FakeMountAdapter` +
`RoiTracker`-backed `measure` callback and asserts the resulting
`CalibrationMatrix` matches within tolerance.
