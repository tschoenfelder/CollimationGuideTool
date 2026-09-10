# Real-world bug regression datasets

Issue #6. When a reproducible field defect in image analysis / calibration /
registration is fixed, and a diagnostic bundle captured the actual frames that
exposed it, the frames become a **permanent, gated regression case** here:
`reproduce → failing test → fix → permanent passing test`.

These are additive. The deterministic synthetic scenarios in
`datasets/acceptance/` and the synthetic golden-master leaves in
`datasets/{collimation,guiding}/` remain the baseline and are not replaced.

## Directory layout

```
datasets/regressions/<issue-id>/
  frames/                     # the real FITS frames, committed at full resolution
  provenance/incident.json    # copied verbatim from ~/.CollimationGuideTool/diagnostics/<uuid>/
  provenance/application.log   # copied verbatim
  expected.json               # independently-authored expectations (schema below)
  README.md                   # provenance, root cause, fix commit, how the expectations were derived
```

`<issue-id>` is the **GitHub issue number** the bug is tracked under
(`datasets/regressions/34/`). When there is genuinely no tracking issue, use
`diag-<first-8-of-uuid>` (`datasets/regressions/diag-ef49ecb1/`); either way the
issue number and diagnostic UUID(s) are recorded inside `expected.json` and
`README.md`.

Frames are committed **as captured — no downsampling**. If a set is too large or
too privacy-sensitive to commit, a case input may instead be a pointer of the
form `local_test_data/<name>/frames/<file>.fits` (see "Uncommitted pointer
inputs" below).

## `expected.json` schema

```json
{
  "issue": 34,
  "diagnostic_uuids": ["ef49ecb1-a052-44ea-b45e-01145a9f0c33"],
  "git_commit_at_capture": "185b94d472c6",
  "user_report": "Failed for guide calibration but not for main",
  "optical_setup": "Main ATR585M 0.38\"/px; Guide GPCMOS02000KPA (color) 3.32\"/px",
  "boundary": "measure_translation_offset",
  "cases": [
    {
      "name": "guide_axis1_saturated_returns_no_match",
      "inputs": { "before": "frames/guide_axis1_before.fits", "after": "frames/guide_axis1_after.fits" },
      "expect": { "match": false },
      "tolerances": {},
      "rationale": "12-bit sensor; ~25% of pixels >= 4090, measured by hand (see README). A pair this saturated must be rejected, not scored."
    }
  ]
}
```

| field | meaning |
|---|---|
| `issue` | integer GitHub issue number, or `null` when keyed by `diag-<uuid8>`. **`issue` or a non-empty `diagnostic_uuids` must be present** (AC#2). |
| `diagnostic_uuids` | the originating diagnostic bundle UUID(s). |
| `git_commit_at_capture` | the `git_commit` from the bundle's `incident.json`. |
| `user_report` | the reporter's own words (the bundle's `reason`), verbatim. |
| `optical_setup` | free text, **cameras/optics only** — no location or other personal data. |
| `boundary` | which registered runner in `tests/regressions/_loader.py`'s `BOUNDARIES` to invoke. Downstream issues (#28/#29/#30) add their own. |
| `cases[]` | one observable expectation each: `name` (unique in the dataset), `inputs` (role → path), `expect` (result shape for that boundary), `tolerances` (per-key `± abs` for numeric keys; `{}` = exact/boolean compare), `rationale` (**required** — see below). |

## The independent-expectation rule (AC#3)

Every case's `rationale` must state **how its `expect` values were obtained
independently of the implementation under test** — a hand measurement, a known
software transform applied to the frame, a physical ground truth, or "must
reject, by inspection". Never paste `measure_translation_offset()` / the
calibrator's own output as the expectation. This is a review gate, the same
measurement-first discipline the repo already applies to characterization tests
and the CRAP baseline.

## How the datasets run

`tests/regressions/` is part of `scripts/check.sh --release` and CI:

- `test_regression_datasets.py` — the generic driver: for every dataset whose
  `boundary` is registered, runs each case and asserts `expect` (numeric keys
  via `pytest.approx(abs=tolerance)`, everything else exactly).
- `test_every_regression_dataset_is_wired.py` — the loud guard (AC#5): a
  `datasets/regressions/<id>/` with a malformed `expected.json`, or with a
  `boundary` that has no registered runner **and** no bespoke
  `tests/regressions/test_*.py` naming it, **fails the build**. Dropping data in
  without wiring a test is caught, not silently skipped.

A case can also ship a bespoke `tests/regressions/test_<id>.py` (see
`tests/core/registration/test_terrestrial_registrar_real_data.py` for the shape)
when an assertion can't be expressed declaratively; the guard accepts that too.

### Uncommitted pointer inputs

An input path beginning `local_test_data/` points at the git-ignored raw-bundle
tree. On a checkout without that data the case **skips cleanly** (it still runs
in full wherever the data is present). Prefer committing full-res frames; use a
pointer only when size or privacy genuinely rules that out.

## Intake workflow

```
python scripts/regression_dataset.py --uuid <diagnostic-uuid> --issue 34
```

Resolves the bundle via `astrotool_core.diagnostics.find_bundle`, creates
`datasets/regressions/34/`, copies `frames/` at full resolution and the bundle's
`incident.json` / `application.log` into `provenance/`, and writes `expected.json`
and `README.md` skeletons pre-filled from `incident.json` with `TODO` markers.
Then: point each case's `inputs` at the real pair, set `boundary` (registering a
runner in `_loader.py` if new), author each `expect` + `rationale` from an
independent source, fill the README's `TODO` sections, and run
`pytest tests/regressions`.

## Relationship to the other dataset trees

| tree | what it is |
|---|---|
| `datasets/acceptance/` | synthetic, deterministic below-UI baseline. Unchanged. |
| `datasets/{collimation,guiding}/` | synthetic golden-master replay leaves. Unchanged. |
| `datasets/fov_registration/out_of_focus_daytime/` | the pre-#6 real-world regression fixture (real frames, downsampled 4×, independent `expected.json`). **Grandfathered** — left in place as the worked precedent; new real-world regressions go here under `datasets/regressions/`. |
| `local_test_data/` (git-ignored) | raw pulled diagnostic bundles for offline algorithm tuning (issue #28's corpus). **Not** regression fixtures until they gain an independently-authored `expected.json`; `scripts/regression_dataset.py` promotes one when it graduates. |

## `example/`

`datasets/regressions/example/` is a fully synthetic worked case (a frame and a
`numpy.roll`-shifted copy of it) — no real data. It exists so this convention
always ships a runnable end-to-end example and a copy-me template (AC#8).
