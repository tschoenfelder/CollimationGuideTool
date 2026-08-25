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

## Stage 3

### packages/astrotool_core/session/session_context.py — `SessionContext`
- Source: `smart_telescope/services/section_logger.py` (`SectionLogger`)
- Change: dropped the hardcoded 12-entry `LOG_SECTIONS` tuple (smart_telescope-
  specific names like "goto", "click_to_center") — sections are created
  lazily on first `get_logger()` call instead, since astrotool_core has no
  opinion on what sections either app needs.
- Stage: 3

### packages/astrotool_core/session/event_log.py — `EventLogger`, `EventRecord`
- Source: `smart_telescope/services/service_call_logger.py`
  (`ServiceCallLogger`), `smart_telescope/domain/service_call_log.py`
  (`ServiceCallRecord`)
- Change: renamed to drop the "service call" framing (this module logs any
  operation, not specifically a "service" invocation); `.call()` ->
  `.event()`.
- Stage: 3

### packages/astrotool_core/session/frame_recorder.py — `save_frame`, `make_filename`
- Source: `smart_telescope/services/diagnostic_frame_store.py`
  (`DiagnosticFrameStore.save_frame`, `_make_filename`)
- Change: dropped RA/Dec/tracking/optical-train-id headers and the
  retention-cleanup machinery — MountStatus (Stage 2's trimmed MountPort)
  no longer carries a sky position, so those headers were never available
  to populate; retention cleanup is an app/deployment-level policy this
  shared recorder doesn't need an opinion on.
- Stage: 3

### packages/astrotool_core/testing/replay_dataset.py — `load_frames`, `load_expected`, `discover_fits_paths`
- Source: `smart_telescope/adapters/replay/camera.py`
  (`ReplayCamera.from_directory`'s FITS-discovery logic)
- Change: separated the pure loading/discovery logic from any CameraPort —
  `camera/replay_camera.py` wraps this as the actual adapter.
- Stage: 3

### packages/astrotool_core/camera/replay_camera.py — `ReplayCamera`
- Source: `smart_telescope/adapters/replay/camera.py` (`ReplayCamera` +
  `ReplayCameraAdapter`)
- Change: merged the two source classes (disk-backed, in-memory-array-backed)
  into one class with two classmethod constructors (`from_directory`,
  `from_arrays`), matching this project's one-adapter-file-per-role layout.
- Stage: 3

### packages/astrotool_core/camera/touptek_adapter.py — `TouptekCameraAdapter`
- Source: `smart_telescope/adapters/touptek/managed.py` (`SmartTouptekCamera`)
- Change: trimmed to what a single-camera tool needs (connect/capture/
  exposure/gain/black-level/conversion-gain/temperature/descriptor).
  Dropped entirely: TEC/cooling control, filter-wheel control, the
  multi-"role" camera-selector/conflict-validation machinery
  (`validate_unique_camera_roles`), setup profiles, and capture priming —
  all smart_telescope-specific concerns with their own real complexity
  that would need a separate characterization pass, out of scope for
  collimation/guiding. `_detect_pixel_shift` and the `EnumV2()`-once-per-
  process guard are ported byte-for-byte and characterization-tested
  (`tests/core/camera/test_touptek_adapter_characterization.py`) before
  being touched, per CONTRIBUTING.md.
- Stage: 3

### packages/astrotool_core/mount/indi_adapter.py — `IndiMountAdapter`
- Source: wraps `onstep_adapter.client.OnStepClient` (new shim, following
  the same discipline as smart_telescope's `adapters/onstep/mount.py`
  shim, but not a port of it — that file's ~550 lines are almost entirely
  goto/park/align/safety-preflight machinery this project's MountPort
  doesn't expose at all, per the Stage 2 trim).
- Change: n/a (new). Maps `MountAxis`/`AxisDirection` to the upstream
  `guide(direction: "n"|"s"|"e"|"w", duration_ms)` pulse primitive.
- Stage: 3

### packages/astrotool_core/acquisition/stream_controller.py — `StreamController`, `FrameMailbox`
- Source: `smart_telescope/services/managed_camera.py` (`ManagedCamera`,
  `FrameMailbox`)
- Change: renamed `ManagedCamera` -> `StreamController`; dropped the
  `role` concept (smart_telescope's main/guide/oag multi-camera setup —
  neither app here needs more than one camera role) in favor of a plain
  `name` used only for thread naming.
- Stage: 3

### packages/astrotool_core/acquisition/single_capture.py, acquisition_state.py
- Source: new, no analog (smart_telescope calls `CameraPort.capture()`
  directly at each call site with ad hoc error handling; there was no
  shared single-shot wrapper to port).
- Stage: 3

### packages/astrotool_core/target/roi_tracker.py — `RoiTracker`, `TrackingState`
- Source: new, no analog (TDD). See the module's own docstring: built on
  direct nearest-neighbor-to-last-known-position matching rather than
  `smarttscope_live_analysis.temporal.track_sources`/
  `classify_temporal_tracks` as PLAN.md originally sketched — those solve
  batch multi-frame linking + persistent/transient classification across
  a whole star field (for exposure/gain recommendations), a different
  problem than real-time single-target reacquisition. The library
  dependency is still valuable for `target/detector.py`'s underlying
  `analyze_frame()` star detection; this deviation is about the
  frame-to-frame *linking* step only.
- Verified: `tests/core/target/test_roi_tracker.py` reproduces the
  architecture doc's own example transition sequence
  `[LOCKED, LOCKED, LOST, SEARCHING, REACQUIRED]`, and
  `tests/integration/test_roi_tracker_replay.py` reproduces it again
  end-to-end through a real `ReplayCamera` + `detect_sources()` pipeline
  against `datasets/collimation/mono_adjustment_shift/` (Stage 3's "done
  when" gate).
- Stage: 3

### packages/astrotool_core/target/roi_selector.py — `select_target`
- Source: new, no analog. Single-frame heuristic (brightest non-donut
  source, preferring `detector.py`'s "normal_star" classification) rather
  than a literal multi-frame "persistent" check — a persistence check
  needs a rolling history that doesn't exist before any lock target has
  ever been acquired.
- Stage: 3

## Stage 4

### packages/astrotool_core/mount/axis_calibration.py — `calibrate_axis`, `calibrate_axes`, `AxisResponse`, `CalibrationMatrix`
- Source: new, no analog (smart_telescope never automated guide
  calibration; it's done by eye/external tooling there).
- Change: n/a. Deliberately has zero dependency on `astrotool_core.camera`
  or `astrotool_core.target` — how "the current position" is measured is
  injected as a `measure: Callable[[], tuple[float, float]]` callback, so
  the module is testable with nothing more than a `MountPort` and a plain
  function. In practice (see the golden-master test) the caller wires
  `measure` to a camera capture + `detect_sources` + `RoiTracker`, but
  `axis_calibration.py` itself never imports either.
- Verified: `tests/integration/test_axis_calibration_replay.py` replays
  `datasets/guiding/axis1_response/` and `axis2_response/` through
  `calibrate_axes` (measured via a `RoiTracker`, re-acquired fresh at each
  "before" step — a deliberate large commanded pulse isn't something a
  live-guiding lock tolerance should be expected to track through in one
  `update()` call) and reproduces the expected px/ms response matrix
  within tolerance (Stage 4's "done when" gate).
- Stage: 4

## Stage 5

Scope decision (asked and confirmed before starting): only the rough
(donut-based) collimation pathway + focus search are ported this stage.
The Tri-Bahtinov mask fine-collimation pathway — `fine_collimation_advisor.py`,
`spike_smoother.py`, `contradiction_detector.py`, mask-sector mapping, and
the FSM states `INSTALL_TRIBAHTINOV`/`MAP_MASK_SECTORS`/`FINE_FOCUS`/
`MEASURE_SPIKES`/`GUIDE_FINE_COLLIMATION`/`MASKLESS_VALIDATION` — is
deferred to a later stage. `spike_smoother.py` and `contradiction_detector.py`
were never read; `domain/bahtinov.py`, `spike_detection.py`, and
`spike_decomposition.py` (the underlying spike-analysis math the deferred
advisor sits on top of) also were not ported this stage, since they exist
solely in service of that deferred pathway.

### apps/collimation_tool/domain/collimation_measurement.py — `Point2D`, `CircleEllipseFit`, `ReferenceCenterCalibration`, stretch/geometry-fit utilities, `DonutMeasurement`, `DonutAnalyzer`
- Source: `domain/collimation/models.py` (geometry primitives),
  `domain/collimation/processing/{stretch,geometry_fits,donut_detection}.py`
- Change: near-verbatim port (all 3 source files were already pure NumPy +
  dataclasses, zero smart_telescope-specific coupling). Only mechanical
  change: takes `astrotool_core.frames.AnalysisPlane` (`.mono`/`.width`/
  `.height`) wherever the source took its own `ProcessedFrame`.
- Stage: 5

### apps/collimation_tool/domain/symmetry_analysis.py — `ObstructionResult`, `detect_obstruction`
- Source: `domain/collimation/processing/obstruction_detection.py`
- Change: same `AnalysisPlane`-for-`ProcessedFrame` swap; otherwise verbatim.
- Stage: 5

### apps/collimation_tool/domain/focus_metric.py — `FocusQuality`, `classify_focus_quality`, `mean_fwhm_px`
- Source: new, no direct 1:1 port. Reuses the excellent/good/poor
  threshold logic from `services/collimation/fwhm_focus.py`'s
  `FWHMFocusController._quality()`, but built on
  `astrotool_core.target.PointSource.fwhm_x`/`fwhm_y` (from
  `smarttscope-live-analysis`'s own moment-based FWHM estimator) instead
  of porting `processing/star_detection.py`'s separate radial-profile
  FWHM estimator (`_estimate_fwhm`) — that file is redundant with
  `astrotool_core.target.detect_sources`/`select_target` entirely and was
  not ported (confirmed during Stage 5 research: it duplicates exactly
  what those already do, just with a different heuristic set).
- Stage: 5

### apps/collimation_tool/domain/collimation_state.py — enums, `CollimationRecommendation`, `ScrewCalibration`, `CollimationAdvisor`, `ScrewResponseLearner`, `CollimationState` FSM
- Source: `domain/collimation/models.py` (enums, `CollimationRecommendation`,
  `ScrewCalibration`), `services/collimation/collimation_advisor.py`
  (`CollimationAdvisor`), `services/collimation/screw_mapper.py`
  (`ScrewResponseLearner`), a trimmed subset of `services/collimation/
  state_machine.py`'s 20-state FSM.
- Change: `CollimationAdvisor`/`ScrewResponseLearner` ported verbatim
  (pure decision logic, zero hardware coupling — placed here rather than
  a 6th domain file, since PLAN.md's domain file list was fixed at 5).
  The FSM is trimmed to 13 states covering only rough-collimation + focus
  (dropped: `SELECT_STAR`/`SLEW_TO_STAR` — the new MountPort has no goto
  — and the whole Tri-Bahtinov branch, per this stage's scope decision).
  `TurnDirection`/`AdjustmentSize`/`CollimationState` use `enum.StrEnum`
  (Python 3.11+) instead of the source's `class X(str, Enum)` spelling.
- Stage: 5

### apps/collimation_tool/application/focus_controller.py — `FocusSearcher`, `FocusSearchResult`
- Source: `services/collimation/{focus_search,fwhm_focus}.py`
- Change: **consolidated two near-duplicate hill-climbers into one.**
  `focus_search.py`'s probe step tested only one direction and assumed
  the untested direction was "good" when the first move didn't improve —
  a shortcut that existed specifically to avoid wasting a measurement
  when a soft focuser limit blocked one direction.
  `astrotool_core.focus.FocuserPort` has no soft-limit-detection surface
  at all (`move()` returns `None`; nothing reports a clamped/rejected
  relative move), so that shortcut has nothing to guard against here.
  Ported `fwhm_focus.py`'s cleaner, symmetric-probe algorithm (tests both
  directions, plus a backlash-elimination final-approach-direction
  correction) once, and use it for both the rough (post-defocus) and
  final-refocus roles the two source files split apart.
- Stage: 5

### apps/collimation_tool/application/recenter_policy.py — `CollimationRecenterPolicy`, `RecenterConfig`, `MountCorrectionResult`
- Source: `services/collimation/mount_centering.py::PulseCenterer` +
  `domain/collimation/config.py::MountCenteringConfig`
- Change: **redesigned, not a literal port** — the old implementation
  derived pulse duration from a theoretical sidereal-rate constant
  (`pixel_scale_arcsec` + a guide-rate fraction) and called the old
  MountPort's `guide(direction: str, duration_ms)` using an assumed
  image-orientation sign convention (`dx>0 -> "w"`, `dy>0 -> "n"`). The
  new MountPort only has `pulse_axis(axis, direction, duration_ms)`;
  rather than reintroduce sidereal-rate arithmetic or an assumed
  orientation, this policy uses Stage 4's empirically measured
  `CalibrationMatrix` directly: `duration_ms = |offset_component_px| /
  measured_px_per_ms`, and picks whichever `AxisDirection`'s calibrated
  response actually opposes the measured error by its measured sign.
  Tolerance/settle/divergence-guard fields (`fine_tolerance_px`,
  `rough_tolerance_px`, `max_pulse_ms`, `settle_ms`, `max_iterations`,
  `max_diverge_count`) port near-verbatim from `MountCenteringConfig`,
  including the exact 10%-growth divergence-detection rule and its
  decay-not-reset counter. One deliberate behavior change: a rejected
  pulse now aborts immediately (`"pulse_rejected"`) instead of being
  silently ignored — the original never checked `guide()`'s returned bool.
- Stage: 5

### apps/collimation_tool/application/collimation_controller.py — `adjust_exposure`, `run_auto_exposure`, `CollimationController`
- Source: `services/collimation/assistant.py::CollimationAssistant`
  (`_handle_auto_exposure`'s formula; `_handle_measure_donut`'s
  measure-then-advise decision shape)
- Change: **not a background-thread session runner.** The source's
  `CollimationAssistant` owns a threaded FSM-driving loop plus session
  report/archive building and cross-app guiding coordination (pausing a
  separate `GuidingService` around mount moves) — all dropped. This
  project has no cross-app guiding coordination to begin with (GuideTool
  is a fully separate app), and report/archive building is a UI/session
  concern for a later stage, not domain/application logic.
  `CollimationController` instead exposes plain synchronous methods
  (`measure_and_advise`, `record_screw_adjustment`) that a UI (Stage 7) or
  test calls explicitly — session-flow orchestration (when to transition
  `CollimationStateMachine`) is left to the caller. `adjust_exposure`'s
  formula (target 80% of full well, 10% tolerance, exposure scaled by
  `target/max(fraction, 0.01)`, clamped to [0.001s, 30s]) is ported
  verbatim from `_handle_auto_exposure`.
- Not ported from `assistant.py` (confirmed during research, genuinely
  can't port, not just deferred): `_handle_slew_to_star`/`_handle_acquire_star`'s
  `mount.goto(ra, dec)`/`mount.enable_tracking()`/RA-Dec polling — the new
  MountPort has no goto/slew/RA-Dec surface at all. Practical implication:
  this project's CollimationTool has no automated "find a star via GoTo"
  phase — the user must already have a star in frame before starting.
- Stage: 5
