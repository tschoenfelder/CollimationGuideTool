# Porting notes: smart_telescope -> CollimationGuideTool

Source-path -> target-path mapping, filled in during each stage's inventory
pass (see PLAN.md Stage 0 and per-stage notes). Not filled in yet — this
file is scaffolding for that record, populated as each stage's port work
happens rather than all at once up front.

## Format

```
### <target path>
- Source: <smart_telescope path> (or: new, no analog)
- Change: <rename / strip coupling / generalize naming / port as-is / ...>
- Stage: <N>
```

## Stage 1

### packages/astrotool_core/frames/frame.py — `Frame`
- Source: `smart_telescope/domain/frame.py` (`FitsFrame`) +
  `smart_telescope/domain/collimation/processing/frame.py` (`ProcessedFrame`)
- Change: merged the two source types into one `Frame` (per PLAN.md — a
  single frame representation instead of a raw/processed pair). Added
  `bit_depth` and `timestamp` fields (previously only on `ProcessedFrame`)
  directly onto `Frame`; `from_fits_bytes` gained a `bit_depth` kwarg since
  it can no longer be inferred from a paired `ProcessedFrame`.
- Stage: 1

### packages/astrotool_core/frames/analysis_plane.py — `AnalysisPlane`, `build_analysis_plane`
- Source: `smart_telescope/domain/collimation/processing/frame.py`
  (`ProcessedFrame`, `normalize_frame`)
- Change: renamed to reflect its new home in the shared core (not
  collimation-specific); takes a `Frame` instead of a `FitsFrame`; gained
  an optional `plane` override so callers can hand it an already-demosaiced
  channel instead of `frame.pixels` directly.
- Stage: 1

### packages/astrotool_core/frames/pixel_format.py — `BayerPattern`, `demosaic`, `mosaic_from_rgb`, `is_bayer`
- Source: new, no analog. Confirmed absent from both smart_telescope and
  smarttscope-live-analysis during Stage 0 inventory; both only ever
  handle already-mono planes.
- Change: n/a (new code, TDD-first per CONTRIBUTING.md). `mosaic_from_rgb`
  was added alongside `demosaic` (not originally itemized in PLAN.md) as
  the shared inverse operation `testing/frame_factory.py` needs to build
  synthetic Bayer fixtures without duplicating the pattern-layout table.
- Stage: 1

### packages/astrotool_core/target/point_source.py — `PointSource`
- Source: adapts `smarttscope_live_analysis.analysis.DetectedSource`
- Change: own dataclass so callers never depend on the library's shape
  directly, per CONTRIBUTING.md's public-interface rule.
- Stage: 1

### packages/astrotool_core/target/detector.py — `DetectionResult`, `detect_sources`
- Source: wraps `smarttscope_live_analysis.analysis.analyze_frame`
- Change: single-frame only — adapts `DetectedSource` -> `PointSource` and
  `StarCountResult` -> `DetectionResult`. Multi-frame temporal linking
  (`smarttscope_live_analysis.temporal.track_sources`) and lock-state
  management are deliberately deferred to `target/roi_tracker.py` (Stage 3),
  not duplicated here.
- Stage: 1

### packages/astrotool_core/testing/frame_factory.py — synthetic frame builders
- Source: new, no analog (smart_telescope's fixtures are FITS files on
  disk, not procedural generators)
- Change: n/a. `star_field_image`/`single_star_image` (Gaussian stars),
  `with_hot_pixels`, `bayer_star_field_image` (built on
  `pixel_format.mosaic_from_rgb`), and `make_frame` (wraps a synthetic
  array into a `Frame`).
- Stage: 1
