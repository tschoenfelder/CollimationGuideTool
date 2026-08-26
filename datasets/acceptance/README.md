# Synthetic acceptance regression suite

Copy `datasets/acceptance/` and `tests/acceptance/` into the CollimationGuideTool repository.

Run:

```powershell
pytest tests/acceptance -v
```

The suite checks:
- centered, offset, clipped and no-signal donut behaviour;
- monotonic collimation error;
- a full below-UI recenter loop:
  synthetic donut -> DonutAnalyzer -> CollimationRecenterPolicy
  -> stateful SyntheticOnStepMount -> newly rendered donut -> centered result;
- no further pulse after convergence.

The JSON files are the reviewable source of truth. Pixel data is generated deterministically
at test runtime. Later real FITS regression datasets can be added alongside these scenarios.


## Guiding acceptance regression

`test_guiding_regression.py` adds a deterministic closed-loop guide test:

synthetic star -> real GuideController measurement -> real correction calculation
-> stateful SyntheticGuideMount -> newly rendered image -> reduced guide error.

It also checks:
- no pulse inside deadband;
- lost frames never cause mount movement;
- reacquisition after missing frames;
- reversed camera/image orientation is corrected from the measured CalibrationMatrix,
  not from a hard-coded direction convention.

The harness is synchronous on purpose. It tests the guiding business loop without
introducing StreamController timing races; existing repository replay tests use the
same deterministic reasoning.
