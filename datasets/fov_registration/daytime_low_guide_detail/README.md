# daytime_low_guide_detail

Real captured frames from a live issue #29 verification session
(2026-09-12): a direct, camera-only Main (ATR585M) + Guide (GPCMOS02000KPA)
capture via `TouptekCameraAdapter`, taken against whatever real daytime
scene the rig happened to be pointed at — no mount movement, per that
session's own authorization scope.

**Status:** populated, but a *negative* result — same class of finding as
[`out_of_focus_daytime`](../out_of_focus_daytime/README.md), reached
independently this time.

`TerrestrialRegistrar.register` reports `INSUFFICIENT_STRUCTURE`. Root
cause confirmed directly (not guessed): the Guide frame's own real content,
once properly demosaiced, has a sharpness ratio (`_sharpness_ratio`, high-
frequency gradient energy relative to overall variance) of ~0.0053 at full
resolution — over 3.5x below the 0.02 default floor. The Main frame's own
overall sharpness (~0.022) sits just above the floor, but the Guide side is
the binding constraint either way (`register()` requires *both* sides to
clear it).

**Confirmed not an exposure/SNR artifact**: three exposure combinations
were tried directly against the real cameras before concluding this —
2ms/2ms (real texture, no saturation), 15ms/15ms (Main near-saturated,
Guide fully saturated at its own ADC ceiling), and 5ms Main / 0.6ms Guide
(the pair pinned here — no saturation on either sensor, best achievable
SNR). The Guide sharpness ratio stayed well below the 0.02 floor across
all three (0.0028, N/A-saturated, 0.0053) — genuinely too little resolvable
high-frequency detail in whatever the Guide's own wide (180mm) field was
framing, not a noise-starved short exposure.

**A real artifact found along the way, worth its own regression note**:
running the *raw, undemosaiced* Guide Bayer plane directly through
`register()` as if it were mono (skipping the required
`astrotool_core.frames.demosaic`/`rgb_to_luma` step every real caller in
this project applies first) produces a spuriously *passing* sharpness
check and reports `AMBIGUOUS_MATCH` (best score ~0.70, rival ~0.70) instead
of the correct `INSUFFICIENT_STRUCTURE` — the raw mosaic's own per-pixel
RGGB color bias reads as high-frequency "detail" to `_sharpness_ratio`
even though none of it is genuine scene structure. Same class of raw-
Bayer detection bias this project has hit before (see the guide camera
green-tint/star-detection incident). `test_matches_expected_result` below
also pins this: registering the raw (non-demosaiced) Guide plane directly
must NOT report a confident match either.

**Does not close issue #29 item 2** ("verify terrestrial registration with
the real Main/Guide setup") on its own: this is a second real *negative*
case, not the genuine positive `OK_OVERLAP` real-data regression that item
was after. Getting a positive real match needs the rig re-pointed at more
texture-rich daytime content than whatever it happened to be aimed at
during this session — that needs mount movement, explicitly out of scope
for this camera-only capture. Left as a documented pending-verification
item (see the issue's own status comment).

`frames/main.fits` / `frames/guide_raw.fits` are downsampled 4x from the
original capture (960x540 / 480x270, matching `out_of_focus_daytime`'s own
fixture size) via genuine per-channel block-averaging (each of the raw
mosaic's 4 Bayer sub-planes independently averaged over 4x4 blocks of its
own samples, then re-interleaved) rather than naive decimation — decimation
was tried first and found to *change* the finding (noise interacting with
strided sampling pushed the Guide's sharpness ratio to 0.023, just over
the floor); block-averaging denoises properly and the real finding survives
confirmed empirically (downsampled: main sharp ~0.0074, guide sharp
~0.0148, both still below 0.02). `guide_raw.fits` is the raw RGGB Bayer
mosaic (not yet demosaiced), matching what the real camera actually
delivers — a test using this fixture must demosaic it first, same as
production.

`expected.json` — `result: null`: `register()` must return a non-`ok`
result for this pair.
