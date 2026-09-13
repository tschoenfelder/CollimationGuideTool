# 35-terrestrial-metric-inversion — terrestrial focus metric inverted the true focus signal

Real captured frames from a live focuser-only rig sweep run 2026-09-13
as a direct follow-up to issue #35 (diagnostic
`73a007b6-6c9b-41e2-a3e5-66a21ec71ffd`), requested by the user after a
second real terrestrial autofocus failure (algorithm selected 14460;
the user's own visually-determined best focus is ~14985).

> Auto focus terrestrial fails again — best focus is around 14985, the
> algorithm suggested 14460. Run a live ±500-step sweep around the
> current position and use the frames to optimize the algorithm.

## Provenance

- Originating issue: #35
- Diagnostic UUID(s): 73a007b6-6c9b-41e2-a3e5-66a21ec71ffd
- Git commit at capture: 471d7e6
- Optical setup: Main optical train, ATR585M camera, OnStep focuser
  (LX200 OnStep INDI driver), terrestrial scene
- `provenance/sweep_summary.json`: the full real sweep, all 21
  positions (14485-15485, step 50) with the *pre-fix* production
  `sharpness` value, `confidence`, and an independent `gradient_energy`
  cross-check per position — see `scripts/terrestrial_focus_sweep.py`.
- `frames/sweep_pos_14985.fits` / `frames/sweep_pos_15485.fits`: the
  two real frames this dataset's own case compares (true focus vs. the
  sweep's far edge).

## Root cause

`measure_terrestrial_focus()` scored each tile by Sobel-gradient
("Tenengrad") energy **divided by that tile's own variance** — meant
to cancel exposure/gain differences between samples. Real sweep data
proved this normalization actively **inverts** the true focus signal
for this real scene: the production `sharpness` value was *minimized*
almost exactly at the independently-verified true best focus (14985,
value 10.01 — the lowest of the entire sweep) and rose toward both
edges (up to 22.12 at 15485) — the opposite of what "higher is better"
is supposed to mean. Raw (unnormalized) Sobel-gradient energy, computed
both as a simple whole-frame check and as a proper tile-median
replay of the same real frames, peaked cleanly and unimodally right at
14985 instead.

Since `AutofocusController` already freezes exposure/gain for the
whole search (`set_auto_exposure_paused(True)`), the cross-sample
exposure-invariance the variance normalization was meant to buy was
never actually exercised by this function's one real caller. Fixed by
dropping the variance division entirely — `_tenengrad_energy()` now
scores each tile by raw mean-squared Sobel-gradient magnitude. This
also explains the earlier bundle (`73a007b6-...`, the original #35
report): both real failures are consistent with the search climbing
*away* from true focus on an inverted-for-this-scene metric, not with
random noise.

## Fix commit

Same commit as the rest of issue #35's search/validation fixes (see
`apps/collimation_tool/application/autofocus_search.py` and
`packages/astrotool_core/focus/terrestrial_focus_metric.py`).

## How the expected values were derived

`sharper_ranks_higher: true`'s truth is the user's own independent,
manual determination of 14985 as best focus on the real rig — never
derived from this codebase's own output. It is corroborated (not
derived) by an independently-coded whole-frame gradient-energy
cross-check computed by hand during this investigation (see
`expected.json`'s own `rationale` for the exact numbers), which used
neither tiling nor variance normalization — a genuinely different
technique from the implementation under test.
