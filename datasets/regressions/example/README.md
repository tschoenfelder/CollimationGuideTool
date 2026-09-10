# example — regression-dataset template

**Synthetic. No real capture, no user data.** This directory exists so
`datasets/regressions/` always ships a complete, runnable worked example of the
convention (issue #6, AC#8) and so contributors can copy it as the starting
point for a real intake.

## What it exercises

`boundary: "measure_translation_offset"` — the one same-camera translation
estimator (`astrotool_core.target.translation_offset.measure_translation_offset`).
The single case feeds it a frame and a copy of that frame shifted by a known
amount, and asserts the estimator recovers the applied shift.

## Provenance / how the expected values were derived

`frames/base.fits` is deterministic textured noise. `frames/shifted.fits` is
`base` displaced by **(dy = 12, dx = 30) px** with `numpy.roll`. The
`expected.json` `dx_px`/`dy_px` are that applied transform — derived from the
transform itself, **not** read back from `measure_translation_offset()` (issue
#6, AC#3). The `±1.0` tolerance covers only the estimator's whole-pixel
rounding.

Recipe (Python, run from the repo root):

```python
import numpy as np
from astropy.io import fits

rng = np.random.default_rng(6)
base = rng.normal(loc=500.0, scale=80.0, size=(96, 96)).astype(np.float32)
shifted = np.roll(base, shift=(12, 30), axis=(0, 1))  # (dy, dx)

fits.PrimaryHDU(data=base).writeto("frames/base.fits", overwrite=True)
fits.PrimaryHDU(data=shifted).writeto("frames/shifted.fits", overwrite=True)
```

## Using this as a template for a real bug

See `datasets/regressions/README.md` for the full schema and the
`scripts/regression_dataset.py` intake helper. In short: copy this directory to
`datasets/regressions/<issue-id>/`, replace `frames/` with the real capture,
fill in the provenance fields and a `provenance/` copy of the diagnostic
bundle's `incident.json` / `application.log`, and author each case's `expect`
and `rationale` from an independent source — never from the implementation's
own output.
