# 35 — Terrestrial autofocus false "success" at the search boundary

Real captured evidence from diagnostic `73a007b6-6c9b-41e2-a3e5-66a21ec71ffd`
(git commit `9f5a5e5e2fec`).

> Terrestrial Auto Focus reported success (best_position=15710) but the
> focuser ended up visibly out of focus.

See also `datasets/regressions/35-terrestrial-metric-inversion/` — a
second, real-rig-sweep-derived regression case for the same issue,
covering a distinct root cause found via a live follow-up investigation
(the terrestrial focus metric's own formula, not just the search
algorithm's boundary handling).

## Provenance

- Originating issue: #35
- Diagnostic UUID(s): 73a007b6-6c9b-41e2-a3e5-66a21ec71ffd
- Git commit at capture: 9f5a5e5e2fec
- Optical setup: Main optical train, ATR585M camera, OnStep focuser
  (LX200 OnStep INDI driver), terrestrial scene
- Raw bundle `incident.json` / `application.log`: copied under `provenance/`.
- `frames/frame_0.fits`..`frame_5.fits`: copied verbatim per the standard
  scaffolding, but **not used by this dataset's own regression check** — they
  are generic ring-buffer captures from the diagnostic bundle (3 Main-camera
  ATR585M frames, 3 Guide-camera GPCMOS02000KPA frames), not tagged to any
  specific autofocus sample position. Confirming this absence is itself part
  of what issue #35 fixes (see "Root cause" below, AC#2 — diagnostics
  previously recorded no camera/optical-train/focuser identity at all).

## Root cause

`incident.json`'s `context.autofocus.samples` records the real
terrestrial-mode focus curve: `(14960,7.66) (15210,13.55) (15460,13.88)
(15710,21.70) (15960,22.44) (15960,22.34) (15710,22.59)` — monotonically
rising from the start position (14960) all the way to the configured
±1000-step search boundary (15960), with real `move_absolute()` calls in
`application.log` confirming the focuser only ever moved in the increasing
direction, never exploring below the start.

`BoundedFocusSearcher`'s `_better()` improvement test requires a 5% margin
over the currently recorded best. The last two samples at/near the boundary
(22.44, then 22.33) both fall under 5% above the prior best (21.70 at 15710),
so `best_position` ends up bookkept one coarse-step *behind* the literal
boundary (15710, not 15960) — the one existing safety check
(`best_position in (allowed_min, allowed_max)`) therefore never fires, and
`SUCCESS` was reported for a curve that never demonstrably turned over.

Fixed in `apps/collimation_tool/application/autofocus_search.py`
(`BoundedFocusSearcher._reached_boundary_while_rising`): checks hill-climb's
own *last* sample directly — at the boundary, and not confirmed to have
declined from the recorded best — independent of the `best_position`
bookkeeping quirk above.

## Fix commit

TODO (fill in once this issue's commit lands)

## How the expected values were derived

`expected.json`'s single case's `expect.status == "best_at_search_limit"`
was derived purely by inspecting the recorded `(position, value)` pairs
above arithmetically (monotonic rise, no confirmed decline anywhere, search
boundary reached) — never by running the fixed implementation. This is a
*decision-algorithm* regression (a recorded scalar curve fed through the
search's own decision logic), not an image-analysis regression, so it does
not use `tests/regressions/_loader.py`'s generic frame-pair `BOUNDARIES`
driver — it is exercised by the bespoke
`tests/regressions/test_issue_35_autofocus_search_regression.py`, which
replays the exact recorded sequence through `BoundedFocusSearcher` via a
scripted fake focuser/measurer.
