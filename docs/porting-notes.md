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

## Stage 2

### packages/astrotool_core/camera/capabilities.py — `CameraCapabilities`, `ConversionGain`, `CameraDescriptor`
- Source: `smart_telescope/domain/camera_capabilities.py`
- Change: `CameraCapabilities`/`ConversionGain` ported as-is. `CameraDescriptor`
  is new — bundles identity (serial number, logical name) with
  capabilities behind one accessor, replacing three separate CameraPort
  getters, per the small-public-interface rule and the doc's own example
  import list (`CameraDescriptor, CameraCapabilities, CameraPort, Frame`).
- Stage: 2

### packages/astrotool_core/camera/port.py — `CameraPort`, `CaptureAbortedError`
- Source: `smart_telescope/ports/camera.py`
- Change: `connect()` returns `None` and raises on failure instead of
  returning `bool` — matches MountPort's literal `connect() -> None`
  Protocol from the architecture doc, so all three port types fail the
  same way. `get_bit_depth()`/`get_serial_number()`/`get_logical_name()`
  folded into `get_descriptor()`.
- Stage: 2

### packages/astrotool_core/camera/fake_camera.py — `FakeCamera`
- Source: new, no direct analog — the default hardware-free adapter both
  apps wire to for Stage 7's "demoable on Windows" requirement. Distinct
  from `testing/fake_touptek.py`: this one is minimal (solid star field,
  no configurable failure modes) since it's runtime/dev config, not a
  test double.
- Stage: 2

### packages/astrotool_core/testing/fake_touptek.py — `FakeTouptekCamera`
- Source: `smart_telescope/adapters/mock/camera.py` (`MockCamera`)
- Change: ported near-verbatim (configurable fail_connect/fail_on_capture/
  bright-dim frames/abortable capture delay); `connect()` raises instead
  of returning `bool`, `capture()` returns `Frame` instead of `FitsFrame`.
- Stage: 2

### packages/astrotool_core/mount/port.py — `MountPort`, `MountAxis`, `AxisDirection`, `MountCapabilities`, `MountStatus`, `CommandResult`
- Source: the literal `MountPort` Protocol from
  collimation-guidetool-architektur.md ("Gemeinsamer INDI-Zugriff,
  getrennte Steuerungslogik"), not smart_telescope's much larger
  `ports/mount.py` (no goto/park/align/sync — only a bounded axis pulse).
- Change: n/a (doc gives the Protocol verbatim). `MountAxis` named
  AXIS1/AXIS2 to match `datasets/guiding/axis1_response`,
  `axis2_response` (Stage 4).
- Stage: 2

### packages/astrotool_core/mount/no_mount.py — `NoMountAdapter`
- Source: new (named explicitly in the architecture doc alongside
  FakeMountAdapter/IndiMountAdapter)
- Change: n/a. Always reports disconnected/not pulse-capable; never
  accepts a pulse.
- Stage: 2

### packages/astrotool_core/testing/fake_mount.py — `FakeMountAdapter`
- Source: new (named explicitly in the architecture doc); loosely
  informed by `smart_telescope/adapters/mock/mount.py` (`MockMount`) for
  the connect-failure/state-tracking shape, but the pulse-based surface
  itself has no analog there (MockMount only has `guide()`/`move()`
  against the old, much larger MountPort).
- Change: n/a. Records every accepted pulse in `pulse_log` for
  calibration/guide-controller tests to assert against.
- Stage: 2

### packages/astrotool_core/focus/port.py — `FocuserPort`, `FocuserStatus`, `FocuserMoveResult`
- Source: `smart_telescope/ports/focuser.py`
- Change: `connect()` returns `None` and raises on failure (consistency
  with camera/mount). `FocuserMoveResult.onstep_reply` dropped — leaked
  an OnStep-specific detail into a hardware-neutral port; the OnStep
  adapter (Stage 3) can still log it internally.
- Stage: 2

### packages/astrotool_core/focus/no_focuser.py — `NoFocuser`
- Source: n/a, new (mirrors `NoMountAdapter`'s role for the focus subsystem)
- Stage: 2

### packages/astrotool_core/focus/fake_focuser.py — `FakeFocuser`
- Source: `smart_telescope/adapters/mock/focuser.py` (`MockFocuser`)
- Change: ported near-verbatim; `connect()` raises instead of returning
  `bool`.
- Stage: 2
