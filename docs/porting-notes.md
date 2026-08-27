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

## Stage 6

### apps/guide_tool/domain/guide_error.py — `GuideError`, `compute_guide_error`
- Source: `domain/guiding.py::GuideMeasurement`
- Change: pared down significantly. Fields that duplicated what
  `RoiTracker.TrackingResult`/`PointSource` already carry (the raw pixel
  centroid measurement, peak/background/noise/saturated/fwhm_px) are
  dropped — this module only computes the *error* relative to an
  established target, on top of the tracker's own position.
  `GuideCentroidEstimator` (`services/guide_measurement.py`'s windowed
  pixel-level centroid/SNR/saturation measurement) is not ported at all:
  confirmed during research to duplicate `astrotool_core.target.
  detect_sources`, the same redundancy already avoided for
  `star_detection.py` in Stage 1/5. `GuideFrame` (frame identity/timing
  metadata) is also not ported — `StreamController.MailboxFrame` already
  covers the same fields (sequence, captured_at_monotonic, dropped_before).
- Stage: 6

### apps/guide_tool/domain/guiding_state.py — `GuideSourceHealth`, `GuideSourceState`, `source_state_from_error`
- Source: `domain/guiding.py` (`GuideSourceHealth`, `GuideSourceState`),
  `services/guide_measurement.py::source_state_from_measurement`
- Change: the multi-camera role-selection concept (`GuideSourceSelector`,
  primary/fallback across smart_telescope's main/guide/oag cameras) is
  dropped entirely — not ported, no trimmed replacement — since this
  project's GuideTool assumes a single guide camera, consistent with
  Stage 3's `StreamController` dropping the same "role" concept. No
  `role` field on `GuideSourceState` as a result. Uses `enum.StrEnum`
  per Stage 5's established convention.
- Stage: 6

### apps/guide_tool/domain/drift_estimator.py — `DriftEstimator`
- Source: new — extracted from the inline `error_history`/`rms_px`
  rolling-window calculation in `services/guiding_service.py::_loop`
  (no closer existing analog found, as PLAN.md asked to check).
- Stage: 6

### apps/guide_tool/domain/correction_model.py — `WouldGuidePulse`, `GuideCorrectionConfig`, `compute_would_pulses`
- Source: `services/guide_measurement.py::MeasureOnlyGuideController`
- Change: redesigned to consume Stage 4's `CalibrationMatrix`
  (`duration_ms = |error_px| * aggressiveness / measured_px_per_ms`)
  instead of a fixed `ms_per_px` rate guess, and to target `MountAxis`/
  `AxisDirection` instead of "ra"/"dec" + "n"/"s"/"e"/"w" strings.
  Direction is picked by the calibration's *measured* sign (same
  `_direction_opposing` reasoning as `collimation_tool.application.
  recenter_policy`) — independently re-implemented here rather than
  imported, since `guide_tool` must never depend on `collimation_tool`.
  `deadband_px`/`max_pulse_ms`/`min_pulse_ms`/`aggressiveness` port
  verbatim; `ra_only` renamed `axis2_enabled` (inverted sense, matches
  the new axis-agnostic naming).
- Stage: 6

### apps/guide_tool/application/correction_policy.py — `GuideCorrectionPolicy`
- Source: new (written fresh, not ported) — a deliberately thin wrapper
  that only calls `MountPort.pulse_axis` for pulses `correction_model.
  compute_would_pulses` already decided on. Kept as a separate class from
  `CollimationRecenterPolicy` per the architecture doc's dependency
  rationale — a change to guiding's correction loop can't leak into
  collimation's recentering, or vice versa.
- Stage: 6

### apps/guide_tool/application/calibration_controller.py — `run_calibration`
- Source: new — wires `astrotool_core.mount.axis_calibration.
  calibrate_axes` to a `measure` callback built from `CameraPort` +
  `detect_sources` + `RoiTracker`, the same composition Stage 4's
  golden-master test (`tests/integration/test_axis_calibration_replay.py`)
  demonstrated, promoted to reusable application-layer orchestration.
- Stage: 6

### apps/guide_tool/application/guide_controller.py — `GuideController`, `GuidingStatus`
- Source: `services/guiding_service.py::GuidingService`
- Change: trimmed to a single guide camera (no `GuideSourceSelector` —
  see `guiding_state.py`'s note above) and built on `astrotool_core.
  acquisition.StreamController` + `astrotool_core.target.{detect_sources,
  select_target, RoiTracker}` instead of `ManagedCamera` +
  `GuideCentroidEstimator`. `pause_pulses`/`resume_pulses`/`rebaseline`
  port near-verbatim (still meaningful single-camera session controls).
  Sends pulses through the new `GuideCorrectionPolicy` instead of calling
  `mount.guide()` directly.
- Verified: `tests/integration/test_guide_lost_star_replay.py` replays
  `datasets/guiding/lost_star/` through `detect_sources` + `RoiTracker` +
  `compute_guide_error` (bypassing the threaded loop for determinism, same
  reasoning as Stage 3's `test_roi_tracker_replay.py`) and reproduces the
  `[LOCKED, LOST, SEARCHING, REACQUIRED]` sequence plus matching guide
  errors within tolerance (Stage 6's "done when" gate — proves the
  RoiTracker core built in Stage 3 for CollimationTool is genuinely
  shared with GuideTool).
- Stage: 6

## Stage 7

No smart_telescope source exists for any file below: its UI is a browser/JS
frontend served by FastAPI, with zero PySide6 (or any desktop-toolkit)
overlap. Everything in `apps/*/ui/` and `apps/*/main.py` is new.

### apps/collimation_tool/ui/live_view.py — `LiveViewLabel`
- Source: new — a `QLabel` that percentile-stretches the incoming mono
  frame to uint8, converts to `QImage`/`QPixmap`, and (when a
  `DonutMeasurement` is available) draws the outer-ring/inner-hole
  circles plus the error vector between their centers via `QPainter`.
- Change: the stretch formula mirrors `collimation_measurement.
  auto_stretch`'s percentiles, but is a separate, display-tuned
  function rather than an import from there — that function is
  analysis-facing (tuned for `DonutAnalyzer`'s detection), this one is
  purely for what looks good on screen, and the two should be free to
  diverge independently.
- Stage: 7

### apps/collimation_tool/ui/main_window.py — `MainWindow`
- Source: new. Owns a `StreamController` directly (unlike GuideTool's
  window — see below) because `CollimationController` is a pure
  per-frame measure/advise API with no run loop of its own; something
  has to drive it each frame, and here that's the UI's `QTimer`.
- Not wired: `CollimationRecenterPolicy` (SCT collimation screws are
  turned by hand — driving the whole scope via the mount is a separate,
  undecided operator workflow) and the Tri-Bahtinov fine-collimation
  pathway (deferred since Stage 5).
- Stage: 7

### apps/collimation_tool/main.py — `main`
- Source: new. Wires a `ReplayCamera` serving a small synthetic donut
  sequence (via `testing.frame_factory.donut_image`) as the default
  camera, not `FakeCamera` — `FakeCamera`'s round single-star field
  can't exercise `DonutAnalyzer` (no ring shape), so PLAN.md's literal
  "fake_camera by default" doesn't produce a working demo here. Frame
  shape/radii/peak intentionally match
  `datasets/acceptance/collimation_cases.json`'s frame config: with a
  240x240 frame the ring occupies a small enough area for
  `DonutAnalyzer.estimate_background`'s sigma-clipping to converge in
  one pass; a first attempt at a larger 480x480/wider-ring demo frame
  made the ring occupy ~30% of the image, which inflated the whole-image
  standard deviation enough that every frame read as `"no_signal"`.
- Stage: 7

### apps/guide_tool/ui/live_view.py — `LiveViewLabel`
- Source: new — same percentile-stretch-to-`QImage` shape as
  CollimationTool's `LiveViewLabel`, drawing a target crosshair, a
  centroid crosshair, and the drift-vector line between them (or a
  "NO LOCK" label when the error is rejected) instead of donut rings.
- Change: the stretch helper is duplicated from CollimationTool's
  rather than shared, per this project's established guide_tool/
  collimation_tool independence rule (see `correction_model.py`'s
  `_direction_opposing` for the precedent).
- Stage: 7

### apps/guide_tool/application/guide_controller.py — `GuidingStatus.latest_pixels`
- Source: new field on an existing (Stage 6) class. `GuideController`
  already owns its `StreamController` privately and drives it via
  `_loop()`; the UI has no other way to reach the live frame for
  display, since `GuidingStatus` previously carried only measurement/
  health fields, not pixels. Populated in `_loop()` from the same
  `mailbox_frame.frame.pixels` `process_frame()` already consumes, and
  held over (like `rms_px`) on ticks with no new frame.
- Stage: 7

### apps/guide_tool/ui/main_window.py — `MainWindow`
- Source: new. Unlike CollimationTool's window, this one does not own a
  `StreamController` — `GuideController` already owns one internally
  (see Stage 6), so the window just calls `start()`/`stop()` and polls
  `status()` on a `QTimer`.
- Stage: 7

### apps/guide_tool/main.py — `main`
- Source: new. Wires a plain `FakeCamera()` with no mount configured —
  unlike CollimationTool, `FakeCamera`'s single round star is exactly
  what `RoiTracker`/`compute_guide_error` need, so PLAN.md's literal
  "fake_camera by default" applies directly here.
- Stage: 7

### tests/conftest.py — `qapp` fixture
- Source: new. Forces `QT_QPA_PLATFORM=offscreen` before any PySide6
  import (module-level, so it applies even to collection-time imports),
  and provides a session-scoped `QApplication` singleton — verified
  working headlessly in this Windows dev environment via
  `PySide6.QtWidgets.QApplication([])` under `QT_QPA_PLATFORM=offscreen`.
- Stage: 7

## Stage 8

### tests/integration/_golden_master.py — `assert_matches_golden`
- Source: new. Factors out the per-key `pytest.approx(..., abs=tol)` loop
  that `test_axis_calibration_replay.py` and `test_guide_lost_star_replay.py`
  were each writing by hand, per PLAN.md's "Regression-protection
  scaffolding" section — same signature as the doc's own sketch, but taking
  an already-loaded `expected` dict rather than a `Path`, since both call
  sites already have a dict in hand (a whole-file `load_expected()`, or one
  sub-dict of it) and a dict-in/dict-out helper composes with either.
  `test_roi_tracker_replay.py` is deliberately NOT converted — it compares a
  list of lock-state names for exact equality, which isn't what a
  numeric-tolerance helper is for.
- Stage: 8

### tests/integration/test_placeholder_datasets_skip_cleanly.py
- Source: new. Wires the four still-empty `datasets/` leaves
  (`collimation/artificial_star`, `collimation/color_bayer`,
  `collimation/mono_centered`, `guiding/steady_drift`) into a real test per
  PLAN.md Stage 8's "wire every datasets/ leaf", rather than leaving the
  auto-skip convention their READMEs describe as prose only. No synthetic
  frames were fabricated for these — that's still deferred (see each
  README). The test skips cleanly while a leaf has no frames/expected.json,
  and deliberately fails if data shows up without a real replay test
  replacing the skip, so the placeholder can't silently rot once populated.
- Stage: 8
