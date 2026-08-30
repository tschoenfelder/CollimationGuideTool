# out_of_focus_daytime

Real captured frames from diagnostic incident
`d2401406-9b4f-4021-935d-5deca3292f5c` (reason: "FOV calibrated wrongly. Add
these frames as local test cases") — a heavily out-of-focus main camera
(ATR585M) pointed at a blurred utility pole/wires, and a guide camera
(GPCMOS02000KPA, color) framed on a landscape/tree line below a bright sky,
during daytime/twilight testing rather than actual night-sky operation. No
resolved stars in either frame.

**Status:** populated. `fov_registration.register_main_frame_in_guide_frame`
reported a "confident" match (score ~0.65) at a location with no genuine
corresponding content — the guide frame's sky region has real standard
deviation (a smooth brightness gradient), enough to pass a plain contrast
check, but almost none of it is genuine high-frequency detail. This dataset
pins the fix (`_sharpness_ratio`/`min_sharpness_ratio`, see that function's
docstring): both frames' high-frequency gradient energy relative to their
own variance must clear a floor before any candidate is scored at all. The
guide frame here scores ~0.002 — over 10x below the 0.02 default floor, and
~75x below any synthetic starfield this project's own test suite uses
(0.16-0.23) — so the fixed behavior is to return `None` (no confident
match) rather than report a specific-but-meaningless location.

`frames/main.fits` / `frames/guide_raw.fits` are downsampled 4x from the
original incident capture (960x540 / 480x270 instead of 3840x2160 /
1920x1080) to keep this fixture a reasonable size — confirmed empirically
that the sharpness-ratio failure mode survives this downsampling
(guide luma sharpness ratio ~0.0024 either way, vs. the fix's 0.02
threshold). `guide_raw.fits` is the raw RGGB Bayer mosaic (not yet
demosaiced), matching what the real camera actually delivers — a test
using this fixture must demosaic it first, the same as production does
(see `astrotool_core.frames.demosaic`/`rgb_to_luma`).

`expected.json` — `result: null`: the fixed algorithm must return `None`
for this pair (see `tests/collimation/ui/test_fov_registration_real_data.py`).
