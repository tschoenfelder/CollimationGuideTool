# Duplication audit — duplicated knowledge and UI-owned policy (#55, #52 inventory)

Task S5 in `docs/restructuring-tasks.md`. Analysis only: no production or test
code was changed. Baseline commit: `615f44f` (2026-09-30).

**Method.** Every candidate below was read in context, not just matched by text. The
question for each was: *is this the same authoritative fact or decision in more than
one place?* Similar-looking code that encodes different facts is listed as an
intentional difference or a false positive. Evidence comes from static reading plus
`git log`/`git show` on the recent fix commits (`c7c4bb8`, `3fc09ae`, `7bb7b68`,
`ca5bb92`, `531a952`, `4730c55`, `9cea2e9`, `6b0a215`, `304178e`). **Nothing here was
checked on real hardware.** Wherever a finding depends on runtime behaviour that only the
rig can confirm, it says so.

**Classification legend**

- **must centralize**: the same authoritative fact or decision lives in more than one
  place. The item names one owner and the S6 step that removes the copies.
- **intentional**: similar code that encodes a genuinely different fact (different
  device, different domain rule).
- **superficial**: code that looks alike but holds no shared authoritative knowledge.
  Not worth an abstraction.
- **open**: needs a user decision before it can be classified.

**Live contradictions** (copies that disagree today, so a bug is likely) are marked
**LIVE**. They are listed here and **not fixed**. Under #52's rules, each one first needs
a separate failing regression before any code changes.

---

## 1. Summary table

Line numbers are at `615f44f`. "Tests" lists the tests that cover the knowledge today.
"none" means no test pins the duplicated decision.

### Category 1: device names, slot names/counts, limits, built-in defaults

| ID | Duplicated knowledge | Locations | Class. | Proposed single owner | Existing tests | S6 step |
|----|----------------------|-----------|--------|-----------------------|----------------|---------|
| D01 | EFW INDI device name fallback | `packages/astrotool_core/filter_wheel/config.py:59` (`_BUILT_IN_DEVICE_NAME = "ToupTek EFW 2"`); `filter_wheel/registry.py:90` (second literal `or "ToupTek EFW 2"`); `filter_wheel/indi_filter_wheel_adapter.py:45` (`_DEFAULT_DEVICE_NAME = "Filter Wheel"`); `testing/fake_indi_server.py:5,33` (simulator default **"ToupTek EFW 1"**, the known-wrong value); about 15 hard-coded test literals | must centralize, **LIVE (simulator)** | config object: `astrotool_core/filter_wheel/config.py` (export one public constant; registry and adapter take it and never re-state it) | `tests/core/filter_wheel/test_config.py`, `test_registry.py`, `tests/collimation/ui/test_shared_filter_wheel.py`, `test_main_window_layout.py`; `tests/core/indi/test_indi_client.py:13` still uses "EFW 1" | S6.1 |
| D02 | INDI server endpoint (host/port) | port 7624: `indi/client.py:35`, `filter_wheel/indi_filter_wheel_adapter.py:46`, `filter_wheel/registry.py:37`, `onstep/settings.py:101`; host: `registry.py:36` / `indi_filter_wheel_adapter.py:63` ("localhost") vs `onstep/settings.py:100` ("127.0.0.1") | must centralize | config object: new `astrotool_core/config/indi_endpoint.py` (or export from `indi/client.py`), read by both `onstep/settings.py` and `filter_wheel/config.py` | `tests/core/onstep/test_onstep_adapters.py:481,536`; `tests/core/filter_wheel/test_registry.py` | S6.1 |
| D03 | Config file locations | `~/.CollimationGuideTool/config.toml`: `config/camera_settings.py:34`, `config/mount_alignment_settings.py:28`, `onstep/settings.py:21`, `filter_wheel/config.py:38`. `~/.SmartTScope/config.toml`: `optics/smarttscope_config.py:27`, `filter_wheel/config.py:37`, `onstep/settings.py:22`. Diagnostics dir: `diagnostics/service.py:56` | must centralize | config object: new `astrotool_core/config/paths.py` | `tests/conftest.py:39-71` has to monkeypatch two separate names (`DEFAULT_CONFIG_PATH`, `load_pixel_scale_arcsec`) one by one. This is evidence of the duplication, not a guard | S6.1 |
| D04 | Camera cooling defaults | target −10 °C: `ui/camera_panel.py:289`, `config/camera_settings.py:56` and `:91`, `camera/touptek_adapter.py:313`. Range fallback ±40 °C: `ui/camera_panel.py:288,373-374` | must centralize | config object: `astrotool_core/config/camera_settings.py` (default target); `CameraCapabilities` (range fallback) | `tests/core/config/test_camera_settings.py`, `tests/core/camera/test_touptek_adapter_identity.py` | S6.1 |
| D05 | Optical-train identifiers ("main"/"Main", "guide"/"Guide", "OAG", "left"/"right") | `filter_wheel/config.py:60`; `ui/main_window.py:207` (`_REAL_TRAINS`), `:632-646` (case-insensitive matching); `ui/mount_test_move_panel.py:507` (`_CAMERA_LABELS`); `ui/focuser_panel.py:124` (default "Main"); `main_window.py:615-616` ("left"/"right" keys) | must centralize (low risk; *uncertain*: SmartTScope's `active_camera_role` vocabulary is external) | domain: new `astrotool_core/optics/train.py` (a `TrainId` enum plus parsing of config roles) | `tests/collimation/ui/test_shared_filter_wheel.py:222-239` | S6.1 (new sub-step) |
| D06 | Target-distance seeds for sizing (stars = ∞, terrestrial 10 km, artificial star 30 m) | `ui/main_window.py:624-630`; `ui/mount_test_move_panel.py:2257` (second 10 km fallback) | must centralize | domain: `collimation_tool/domain/target_mode.py` (distance per target type) | `tests/collimation/ui/test_calibration_sizing.py` | S6.4 |
| D07 | Stability-sampling defaults (3 samples, 0.2 s) | `config/mount_alignment_settings.py:124-125`; `acquisition/motion_aware_acquisition.py:135-136` and `:240-241` (function defaults) | must centralize (low) | config object: `MountAlignmentSettings`; the acquisition functions should require the values explicitly | `tests/core/acquisition/test_motion_aware_acquisition.py`, `tests/core/config/test_mount_alignment_settings.py` | S6.7 |
| D08 | Calibration target fraction 25 % | `config/mount_alignment_settings.py:39`; `mount/movement_sizing.py:118` (`SizingPolicy.target_fraction = 0.25`) | must centralize (low) | config object: `MountAlignmentSettings` (`SizingPolicy` without a default) | `tests/core/mount/test_movement_sizing.py`, `test_calibration_sizing.py` | S6.1 |
| D09 | Observer site default 50.336/8.533, port 7624, "LX200 OnStep" | `onstep/settings.py:102-104`; `testing/fake_onstep_indi_client.py:351-354` | superficial (a test double mirroring the config; adopting D02/D03's owner there is optional) | — | `tests/core/onstep/test_onstep_adapters.py` | — |

### Category 2: timeout/retry constants for the same external behaviour

| ID | Duplicated knowledge | Locations | Class. | Proposed single owner | Existing tests | S6 step |
|----|----------------------|-----------|--------|-----------------------|----------------|---------|
| T01 | "Device finished moving" safety net (seen-Busy, then Ok, or timeout) | Focuser: `ui/focuser_panel.py:79` (10 s) **and** `application/autofocus_search.py:168,436-439` (`move_settle_timeout_s=10.0`, polls `is_moving()` only). Filter wheel: `ui/filter_wheel_panel.py:42` (10 s). Park: `ui/mount_park_panel.py:34` (30 s) | must centralize the *mechanism* (values legitimately differ per device); **LIVE** (see L01, T01b in §2) | application service: `collimation_tool/application/operation_lifecycle.py` (`BoundedCompletion` with per-device timeout from config) | `tests/collimation/ui/test_filter_wheel_panel.py:182` (stuck-Busy); nothing for the focuser or park stuck-Busy cases | S6.3 |
| T02 | Park/unpark confirmation | `ui/mount_test_move_runner.py:70-72,123-130` (re-polls `status().parked`, 5 s/5 s); `ui/mount_park_panel.py:34` (30 s); the adapter already confirms: `onstep/mount_park_adapter.py:68,94` (`result.confirmed` / `unparked_confirmed`) | must centralize | adapter: `astrotool_core/onstep/mount_park_adapter.py` (a confirmed result); callers stop re-polling | `tests/collimation/ui/test_mount_test_move_runner.py`, `tests/core/onstep/test_onstep_adapters.py` | S6.8 |
| T03 | Tracking-change settle time | `mount/operating_mode.py:34` (`_DEFAULT_SETTLE_TIMEOUT_S = 3.0`: "OnStep … can take a couple of seconds"); `mount/tracking_mode.py:63` (default `0.0`), used directly by `ui/mount_test_move_panel.py:1217` | must centralize | domain policy: `TrackingEnforcer` in `astrotool_core/mount/operating_mode.py` (sole caller of `ensure_tracking_mode`) | `tests/core/mount/test_operating_mode.py`, `test_tracking_mode.py` | S6.4 |
| T04 | Mechanical settle after a mount move | `config/mount_alignment_settings.py:65` (`settle_ms` 1000) plus `:77` (`frame_settle_ms` 500); `application/recenter_policy.py:40` (`settle_ms` 750) | must centralize | config object: `MountAlignmentSettings`, consumed through the S6.7 service | `tests/collimation/application/test_recenter_policy.py` | S6.7 |
| T05 | Budget for a fresh frame after motion | `ui/mount_test_move_panel.py:313` (2.0 s) plus exposure scaling `:1250-1291` (from diagnostic 9ca6daa3); `application/autofocus_controller.py:112,267` (fixed 5.0 s, no scaling) | must centralize, **LIVE (latent)** | application service: new `collimation_tool/application/fresh_frame.py` (S6.7) | `tests/collimation/ui/test_calibration_frame_validity.py`; nothing for autofocus with long exposures | S6.7 |
| T06 | Pulse-rejection retry budget (6 × 0.3 s plus a 0.3 s pre-delay) | `ui/mount_test_move_runner.py:82-83,133-150,335` (tuned against the removed `IndiMountPulseAdapter`) | must centralize (delete together with P01) | adapter: OnStepAdapter owns motion-gate timing | `tests/collimation/ui/test_mount_test_move_runner.py` | S6.8 |
| T07 | Connect timeouts: OnStep 5.0 s (`onstep/connection.py:42`) vs INDI filter wheel 10.0 s (`indi_filter_wheel_adapter.py:47`) | — | intentional (different devices/drivers) | — | — | — |
| T08 | UI poll intervals 250/200/150/100 ms (`focuser_panel.py:71,80`, `filter_wheel_panel.py:37`, `mount_park_panel.py:30`, `mount_test_move_panel.py:505`, `fine_collimation_panel.py:55`, `main_window.py:197`, `camera_panel.py:117`, `guide_tool/ui/main_window.py:64`) | — | superficial (UI refresh cadence, not device behaviour) | — | — | — |

### Category 3: capability checks and unsupported-feature handling

| ID | Duplicated knowledge | Locations | Class. | Proposed single owner | Existing tests | S6 step |
|----|----------------------|-----------|--------|-----------------------|----------------|---------|
| C01 | ToupTek feature support | Flag-gated properties `camera/touptek_adapter.py:504-514`, each re-checked by hand at `:404,517,524,529,539,565,573,577,585,591,728`; a separate runtime E_NOTIMPL probe only for temperature `:313-320,542-562`; inline `supports_hdr` `:621`; hard-coded `supports_lcg=True` `:620`; catch-all `_try()` `:792-797` logs every failure. `get_descriptor()` (`:598-611`) re-queries gain/exposure ranges on each call and is called **per frame** by `ui/camera_panel.py:431` | must centralize | adapter: one capability table in `astrotool_core/camera/touptek_adapter.py` (or `camera/touptek_capabilities.py`) mapping feature → model flag / probe-once result; descriptor cached per connect | `tests/core/camera/test_touptek_adapter_no_hardware.py`, `test_touptek_adapter_identity.py` (from `531a952`, `4730c55`) | S6.5 |
| C02 | Mount/focuser optional features by duck typing | `ui/mount_park_panel.py:82` (`hasattr confirm_home`), `:163` (`long_running_actions`), `:196` (`home_confirmed`); `ui/mount_test_move_panel.py:2292-2296` (three `hasattr`s = "angular"), `:2984` (`abort`); `ui/mount_test_move_runner.py:166` (`move_angular`); `ui/focuser_panel.py:480` (`blockers`). `MountCapabilities.supports_pulse_guiding` (`mount/port.py:34`, `onstep/mount_pulse_adapter.py:73` = False) **is never consulted by any caller** | must centralize | adapter/port: extend `MountCapabilities` (`astrotool_core/mount/port.py`) and add a `MountParkCapabilities` in `park_port.py`; UI and services ask for capabilities, never `hasattr` | `tests/contracts/test_mount_contract.py`, `test_mount_park_contract.py`, `test_mount_park_confirm_home.py` | S6.5 (extend to mount ports, new sub-step) |
| C03 | Camera temperature polled even without cooling support (`ui/camera_panel.py:296`) | — | intentional (documented in `4730c55`: some cameras report a temperature without a TEC; the adapter caches E_NOTIMPL) | — | `test_touptek_adapter_identity.py` | — |

### Category 4: connect/disconnect exception normalization

| ID | Duplicated knowledge | Locations | Class. | Proposed single owner | Existing tests | S6 step |
|----|----------------------|-----------|--------|-----------------------|----------------|---------|
| K01 | "Connect failed" handling in UI panels | `ui/focuser_panel.py:274-304`, `ui/mount_park_panel.py:106-135`, `ui/mount_test_move_panel.py:1155-1179` (log + `last_connect_error`, `7bb7b68`/`ca5bb92`); `ui/filter_wheel_panel.py:132-164` (**no log, no `last_connect_error`**); `ui/camera_panel.py:455-461` (**`self._camera.connect()` unguarded** in stream start) vs `:479-531` (guarded); `guide_tool/ui/main_window.py:196-203` (**catches only `ConnectionError`, never releases the previous camera**) | must centralize, **LIVE** | application service: new `collimation_tool/application/device_connection.py` (`DeviceConnectionService`). The guide app reuses it or an `astrotool_core` equivalent | `tests/collimation/ui/test_focuser_panel.py`, `test_mount_interface_state.py`, `test_mount_park_confirm_home.py:29,39`; `test_camera_switching.py`; `tests/guide/ui/test_guide_main_window.py:68` (ConnectionError only) | S6.2 |
| K02 | Which exceptions an OnStep operation may raise | `onstep/connection.py:77` and `onstep/mount_pulse_adapter.py:138`: `(ConnectionError, RuntimeError, TimeoutError, ValueError)`; `onstep/focuser_adapter.py:84-95` catches only `ValueError` (`move_absolute`) and `:114` suppresses only `ValueError` (`move`); park adapter `:68,94,110,131` raises `RuntimeError`, everything else propagates | must centralize | adapter: `astrotool_core/onstep` (one `ONSTEP_ERRORS` tuple plus one normalizer; ports return results, not raw exceptions) | `tests/core/onstep/test_onstep_adapters.py` | S6.2 |
| K03 | Disconnect/stop cleanup | Unguarded `disconnect()` at `focuser_panel.py:299`, `filter_wheel_panel.py:153`, `mount_park_panel.py:129`, `mount_test_move_panel.py:1175`; `MountParkPanel.stop()` `:281-285` calls `stop_tracking()` (can raise `RuntimeError`, `mount_park_adapter.py:110-111`) before `disconnect()`; `MountParkPanel._enforce_tracking("connect")` `:123` runs after `_connected=True` with no guard; `MainWindow.closeEvent` `main_window.py:1059-1068` stops panels in sequence without isolation (one raising `stop()` skips the rest) | must centralize | application service: `DeviceConnectionService` (disconnect/stop never raises; each device isolated) | `tests/collimation/ui/test_collimation_main_window.py` (quit cleanup); no test for a raising stop/disconnect | S6.2 |
| K04 | Unhandled-exception diagnostic boundary | `apps/collimation_tool/main.py:115-145` and `apps/guide_tool/main.py:30-53` (near-verbatim copies, "shared only by convention") | must centralize (low) | `astrotool_core/diagnostics` (`install_excepthook(diagnostics)`) | `tests/core/diagnostics/test_diagnostic_service.py` (service only) | new step needed (small) |

### Category 5: operating-mode and tracking decisions (#44/#48)

| ID | Duplicated knowledge | Locations | Class. | Proposed single owner | Existing tests | S6 step |
|----|----------------------|-----------|--------|-----------------------|----------------|---------|
| M01 | Terrestrial vs star environment in Mount Align | Local toggle `ui/mount_test_move_panel.py:496,1010-1024,1181-1187`; `_required_tracking_mode` `:1189-1200` (the global mode wins only when TERRESTRIAL; in ASTRONOMICAL the **panel toggle alone** decides); MainWindow copies the mode into it `ui/main_window.py:697-705` | must centralize (breaks the AGENTS.md "Operating mode" rule) | domain policy: `OperatingMode`/`TrackingEnforcer` (`astrotool_core/mount/operating_mode.py`); Mount Align reads it | `tests/collimation/ui/test_operating_mode_ui.py:36,45,174,187` | S6.4 |
| M02 | Two tracking-verification paths | `ui/mount_test_move_panel.py:1202-1223`: `enforcer.verify(...)` (3 s settle) when the mount is available, else a direct `ensure_tracking_mode(...)` (0 s settle, no trail) | must centralize | domain policy: `TrackingEnforcer` only | `test_operating_mode_ui.py`, `tests/core/mount/test_operating_mode.py` | S6.4 |
| M03 | Tracking state before a mount move | `ui/mount_test_move_runner.py:312-336`: **always** `unpark()` or `stop_tracking()` (OnStep emergency stop, `mount_park_adapter.py:99-111`) before every move, whatever the mode; the panel requires tracking **ON** for star mode (`mount_test_move_panel.py:1200`) and re-verifies/re-enables it after the move (`:1225-1237`) | must centralize, **LIVE** | domain policy: `TrackingEnforcer` decides; the movement service (S6.8) only asks it | `test_mount_test_move_runner.py`, `test_calibration_failure_recovery.py`; no test for star-mode tracking around a move | S6.4 (+S6.8) |
| M04 | Autofocus environment toggle Star / Terrestrial / Artificial (`ui/focuser_panel.py:199-213,380-385`), independent of `OperatingMode`; only the measurement gate reads the global mode | open. The metric choice (FWHM vs Tenengrad) and artificial-star are image-algorithm distinctions that AGENTS.md allows, but "terrestrial scene vs sky" is the operating mode | open (user decision: derive the default from `OperatingMode`, or keep an independent metric choice?) | domain: `collimation_tool/domain/target_mode.py` + `OperatingMode` | `tests/collimation/ui/test_focuser_panel.py`, `test_operating_mode_ui.py:132` | S6.4 (scope question) |
| M05 | "Is the target an artificial star?" decided in three places | `collimation_tool/domain/target_mode.py:19-31` (`CollimationTargetMode`); `ui/main_window.py:431-434` (registration-mode button); `application/autofocus_controller.py:57` (`AutofocusMode.ARTIFICIAL_STAR`, synced one-way at `main_window.py:724-732`); `_calibration_distance_m` `main_window.py:630` reads the **registration button**, not `CollimationTargetMode` | must centralize, **LIVE (low impact)** | domain: `CollimationTargetMode` (`collimation_tool/domain/target_mode.py`) | `test_operating_mode_ui.py:76`; `tests/collimation/ui/test_fine_collimation_panel.py` | S6.4 (new sub-step) |
| M06 | Tracking enforcement on connect/unpark in MountParkPanel (`mount_park_panel.py:123,137-149,231,244`) delegates to `TrackingEnforcer` | — | intentional (correct owner already; the unguarded error path is K03) | — | `test_operating_mode_ui.py:88,99` | — |

### Category 6: Busy/running/cancelled terminal-state cleanup

| ID | Duplicated knowledge | Locations | Class. | Proposed single owner | Existing tests | S6 step |
|----|----------------------|-----------|--------|-----------------------|----------------|---------|
| L01 | One-action-at-a-time state machine (in-flight flag, seen-Busy, issued-at, timeout) | `ui/focuser_panel.py:150-152,306-315,430-446`; `ui/filter_wheel_panel.py:80-83,200-249`; `ui/mount_park_panel.py:56-61,151-252` | must centralize, **LIVE** (MountParkPanel still has the pre-`3fc09ae` ordering; FocuserPanel comment contradicts its code) | application service: `collimation_tool/application/operation_lifecycle.py` | `test_filter_wheel_panel.py:182` only | S6.3 |
| L02 | Background run lifecycle (submit / busy / cancel / take_latest) | `ui/autofocus_runner.py:28-76`, `ui/fine_collimation_runner.py:30-76`, `ui/fov_calibrator.py:95-275` (3 entry points), `ui/mount_test_move_runner.py:185-393`. Only the last one guarantees `busy` is cleared on exception (`:266-282`, `6b0a215`/#49) | must centralize, **LIVE** | application service: `OperationLifecycle` (one worker wrapper with guaranteed terminal outcome) | `tests/collimation/ui/test_autofocus_runner.py`, `test_fine_collimation_runner.py`, `test_fov_calibrator.py` (no exception-path tests); `test_mount_test_move_runner.py` (has one) | S6.3 |
| L03 | Quit semantics per panel: FocuserPanel aborts motion (`:538-545`), MountParkPanel emergency-stops (`:281-285`), MountTestMovePanel does **not** abort its in-flight runner (`:3252-3266`) and relies on MountParkPanel being stopped first in `closeEvent` (`main_window.py:1066-1067`) | — | intentional (per-device abort differs), but the ordering dependency is implicit and should be written into S6.3's contract | — | `test_collimation_main_window.py` | S6.3 (document) |
| L04 | Fail-safe around the poll loop | Only `ui/mount_test_move_panel.py:1610-1624,2998-3007` resets to a retryable state when a poll raises; the Focuser, FilterWheel and MountPark poll handlers have none (*uncertain* whether OnStep `status()` can raise under INDI) | must centralize | application service: `OperationLifecycle` | `test_calibration_failure_recovery.py` (Mount Align only) | S6.3 |
| L05 | Pause/resume auto-exposure and live updates around a measurement | `application/autofocus_controller.py` (`set_auto_exposure_paused` in try/finally, around `:150-200`); `ui/mount_test_move_panel.py:1751-1758` plus defensive resume in `stop()` `:3261-3266`; `ui/main_window.py:296` (focuser jog pauses Main updates) | must centralize | application service: S6.7 fresh-frame service (owns "measurement window" enter/exit) | `test_focuser_panel.py`, `test_calibration_frame_validity.py` | S6.7 |

### Category 7: repeated conversion/geometry formulas

| ID | Duplicated knowledge | Locations | Class. | Proposed single owner | Existing tests | S6 step |
|----|----------------------|-----------|--------|-----------------------|----------------|---------|
| G01 | rate × duration ↔ arcsec | `ui/mount_test_move_panel.py:2086,2098,2148,2314,2324`; `mount/movement_sizing.py:71,190` | must centralize | domain: `astrotool_core/mount/movement_sizing.py` (`arcsec_for(rate, ms)` / `ms_for(rate, arcsec)` plus clamp) | `tests/core/mount/test_movement_sizing.py`, `test_calibration_angular.py` | S6.6 |
| G02 | Duration that produces a wanted pixel shift (px ÷ px-per-ms, clamped) | `ui/mount_test_move_panel.py:2676-2678` (nudge); `application/recenter_policy.py:110-111` (recenter); `mount/axis_calibration.py:266-340` (`solve_screen_move`) | must centralize | domain: `astrotool_core/mount/axis_calibration.py` | `tests/core/mount/test_axis_calibration.py`, `test_recenter_policy.py` | S6.6 |
| G03 | `hypot`/`atan2` distances and angles across domain modules (`collimation_measurement.py`, `symmetry_measurement.py`, `axis_calibration.py`, `registration/*`) | — | superficial (generic maths, different quantities) | — | — | — |

### Category 8: duplicate adapter/fallback movement paths (#31/#46)

| ID | Duplicated knowledge | Locations | Class. | Proposed single owner | Existing tests | S6 step |
|----|----------------------|-----------|--------|-----------------------|----------------|---------|
| P01 | How the app moves the mount: a timed `pulse_axis` path that the production adapter always refuses | Nudges `ui/mount_test_move_panel.py:2745-2749` (`runner.submit`, duration only); screen moves `:2938-2942` (3-tuples); every calibration bootstrap `:1842-1853` (`_step_arcsec` is None until a rate is installed, `:2318-2324`; a rate is installed only by `_learn_rate` after a **successful timed** move, `:2116-2158`); guide reacquisition `application/recenter_policy.py:113`. Production `onstep/mount_pulse_adapter.py:143-151`: `pulse_axis` **always** returns `accepted=False` | must centralize, **LIVE (highest)** | application service: new `collimation_tool/application/mount_motion.py` over the angular `MountPort` API (adapter: OnStepAdapter's `move_*_axis_deg`) | `tests/collimation/ui/test_calibration_angular.py:131` asserts "the very first move is a timed bootstrap", using a fake that *accepts* pulses; the end-to-end real-adapter test `test_calibration_real_onstepadapter.py` was **deleted** in `304178e` | S6.8 |
| P02 | Timed fallback when an angular move is refused | `ui/mount_test_move_runner.py:91-96,156-178` (`axis_motion_refused_at_home`, `raspberry_time_plausible_not_trusted` → `pulse_axis`) | must centralize (delete), **LIVE** | as P01 | `test_calibration_angular.py:229` ("timed_fallback" in paths, fake mount) | S6.8 |
| P03 | Three independent movement executors | `ui/mount_test_move_runner.py` (unpark + tracking-off + retries + settle); `application/recenter_policy.py:59-137` (direct pulse, own settle, no unpark/tracking check); `astrotool_core/mount/axis_calibration.py:80,107` (`calibrate_axes`, direct pulse; used by `guide_tool/application/calibration_controller.py`) | must centralize (the guide-app path is dormant because guide `main.py` wires no mount, so it is lower priority) | application service: `mount_motion.py` (S6.8) | `test_mount_test_move_runner.py`, `test_recenter_policy.py`, `tests/core/mount/test_axis_calibration.py`, `tests/guide/application/test_calibration_controller.py` | S6.8 |
| P04 | Settings and ports for the dead timed path | `config/mount_alignment_settings.py:33-45` (`rate_preset` "7", `pulse_ms` 1000, `calibration_rate_preset` "6", pulse caps); `mount/movement_sizing.py:35` (rate-preset table); `onstep/mount_pulse_adapter.py:103-111` (`install_rate`/`installed_rate`: "accepted but never consulted") | must centralize (delete or re-purpose after the P01 decision) | config object: `MountAlignmentSettings` | `tests/core/config/test_mount_alignment_settings.py` | S6.8 |
| P05 | Stale copies of movement/device facts in comments | `ui/mount_test_move_runner.py:7,33,77,318` (removed `IndiMountPulseAdapter`/`IndiMountParkAdapter`); `apps/collimation_tool/main.py:110-111` ("moves below 720″ are refused" vs the real 30″ at `mount_pulse_adapter.py:45`); `ui/mount_park_panel.py:164` ("unpark also drives it home", contradicted by `mount_park_adapter.py:77-88` after `87700da`); `filter_wheel/indi_filter_wheel_adapter.py:5-25` (points to the non-existent `focus.indi_focuser_adapter`; "no real EFW hardware identified … unverified placeholder") | superficial (stale documentation, not code); fix in the owning step | — | — | S6.8 / S6.1 |

**Counts:** 43 items. **must centralize: 34** · intentional: 4 (T07, C03, M06, L03) ·
superficial: 4 (D09, T08, G03, P05) · open: 1 (M04). Of the must-centralize items,
**9 carry a LIVE contradiction** (D01, T01, T05, K01, M03, M05, L01, L02, P01/P02
counted as one).

---

## 2. Detail for "must centralize" items

The live contradictions come first, in risk order. Each one needs a failing regression
**before** any fix (#52 rule).

### P01 / P02: the Mount Align and reacquisition movement path cannot move the real mount (LIVE, highest)

- **Duplicated decision:** "how a commanded move reaches the mount". OnStepAdapter 0.4.x
  (`onstep/mount_pulse_adapter.py`) offers one working primitive, `move_angular`
  (30″–36000″, `:44-46,113-140`). `pulse_axis` always refuses (`:143-151`) and
  `capabilities().supports_pulse_guiding` is `False` (`:73`). The app still routes moves
  through the old timed path in four places:
  1. **Nudges** RA±/Dec± go through `_after_nudge_before_capture` → `runner.submit(..., duration_ms)`
     (`mount_test_move_panel.py:2745-2749`). No arcsec is ever passed, so
     `_move_with_retry` falls back to `pulse_axis` (`mount_test_move_runner.py:163-178`).
  2. **Screen moves** use `submit_sequence` with `(axis, dir, ms)` 3-tuples (`:2938-2942`).
     Same result.
  3. **Calibration bootstrap:** `_step_arcsec` returns `None` until a rate is installed
     (`:2318-2324`). A rate is only installed by `_learn_rate` from a **successful timed**
     move (`:2116-2158`). So the first step of every direction is `pulse_axis`, and a
     rate can never be installed.
  4. **Guide-assisted reacquisition** (#39): `CollimationRecenterPolicy` calls
     `self._mount.pulse_axis` directly (`recenter_policy.py:113`) and returns
     `"pulse_rejected"`.
- **P02:** an angular refusal for "at home" or "time not trusted" falls back to
  `pulse_axis` (`runner:91-96,172-176`). On 0.4.x that fallback is refused as well, after
  6 × 0.3 s of retries (T06). The error that is reported is the fallback's own message
  (`_NO_PULSE_PRIMITIVE`), so the real refusal reason is hidden.
- **How it diverged:** migration `304178e` replaced the adapter and **deleted**
  `tests/collimation/ui/test_calibration_real_onstepadapter.py`. That was the only test
  driving panel → runner → real OnStepAdapter shape. The remaining tests
  (`test_calibration_angular.py:131`) use a fake mount that accepts pulses. That fake
  encodes the old contract, not the production one.
- **Contradicts AGENTS.md:** "Mount Align should calculate useful movement up front" with
  angular seeds (the "Mount Align movement policy" section), and "Manual/bootstrap RA±/Dec±
  … must use the OnStepAdapter boundary".
- **Confidence:** high from static reading. **Not hardware-verified** (project memory says
  the Mount Align motion buttons have not been exercised on the rig since 0.4.0). A
  cheap first proof is a regression test that wires `MountTestMovePanel` to the real
  `OnStepMountPulseAdapter` over `FakeOnStepIndiClient` and presses RA+.
- **Risk:** Mount Align calibration, nudges, screen moves and #39 reacquisition are all
  non-functional on the real rig. Owner and step: `mount_motion.py` application service, S6.8.

### M03 / M01 / M02: Mount Align owns tracking policy that contradicts the global mode (LIVE)

- **M03:** the runner turns tracking **off** before every move
  (`mount_test_move_runner.py:312-336`). It calls `unpark()` when parked, otherwise
  `stop_tracking()`, which on OnStep is an **emergency stop** (abort + tracking off,
  `mount_park_adapter.py:99-111`). This happens regardless of mode. In star mode the panel
  requires tracking **ON** (`mount_test_move_panel.py:1200`) and verifies it before the
  BEFORE capture (`:1225-1237`). The move then drops it, and the AFTER-capture
  verification turns it back on (`ensure_tracking_mode` → `start_tracking`). Every star
  calibration step therefore toggles tracking twice between BEFORE and AFTER. That is
  exactly the "tracking leak / blurred frames" class this runner's own docstring
  describes, just in the opposite direction.
- **M01:** in ASTRONOMICAL mode the panel's own Star/Terrestrial toggle alone decides the
  required tracking state (`:1197-1200`). AGENTS.md: "Do not add another local
  Terrestrial/Star environment toggle inside Mount Align."
- **M02:** when the enforcer exists and the mount is available, the check goes through
  `TrackingEnforcer.verify` (3 s settle, trail recorded). Otherwise it calls
  `ensure_tracking_mode` directly with a 0 s settle and no trail (`:1207-1223`). T03 has
  the same root cause.
- **Owner:** `TrackingEnforcer` (`astrotool_core/mount/operating_mode.py`), extended with
  "required tracking for workflow X in mode Y". Mount Align and the S6.8 movement service
  only ask it. Step: S6.4.

### L02 / L01: busy/in-flight cleanup is fixed in one place and missing in the others (LIVE)

- **L02:** `MountTestMoveRunner._run` always publishes an outcome and clears `busy`, even
  when the worker crashes (`6b0a215`, #49). The three other runners do not:
  `AutofocusRunner._run` (`autofocus_runner.py:60-64`), `FineCollimationRunner._run`
  (`fine_collimation_runner.py:59-63`), and `FovCalibrator._run`/`_run_star_field`/
  `_run_artificial_star` (`fov_calibrator.py:215-253`). Any exception leaves `busy=True`
  for good. The trigger is reachable: `AutofocusController.run` has only a `finally`
  (`autofocus_controller.py:160-197`), and `OnStepFocuserAdapter.move_absolute` converts
  only `ValueError` into a result (K02), so an INDI `TimeoutError`/`RuntimeError`
  escapes. The effect: `FocuserPanel._autofocus_running` stays True, the poll timer keeps
  running, and Auto Focus plus In/Out stay disabled until restart. *Static inference; no
  test covers the exception path* (`test_autofocus_runner.py` has none).
- **L01:** `3fc09ae` moved the stuck-Busy timeout out of an `elif` in FilterWheelPanel and
  FocuserPanel. **MountParkPanel still has the old shape** (`mount_park_panel.py:232-251`):
  the timeout is reachable only when *not* busy. For the real OnStep adapter the worker
  branch (`:214-231`) clears the in-flight flag when the worker finishes, so the practical
  impact is limited to ports without `long_running_actions` (fakes, `NoMountPark`,
  future adapters). *Latent, not an active rig bug.*
- **L01 (FocuserPanel):** the comment at `focuser_panel.py:431-437` says In/Out
  re-enabling "doesn't depend on the stuck state ever clearing", but
  `_update_move_buttons_enabled` still gates on `not self._focuser.is_moving()` (`:452`).
  So after the timeout, In/Out stay disabled while the driver reports Busy. The `_on_stop`
  docstring (`:333-336`) says this is intended, so the code may be right and the
  `_poll_status` comment wrong. Either way the knowledge contradicts itself. No
  stuck-Busy test exists for FocuserPanel or MountParkPanel. FilterWheelPanel has one
  (`test_filter_wheel_panel.py:182`).
- **T01b (same mechanism, autofocus):** `BoundedFocusSearcher._wait_for_move_settled`
  (`autofocus_search.py:436-439`) treats "`is_moving()` is False" as settled right away.
  FocuserPanel's module docstring (`:22-36`) records that the driver's Busy lags a move by
  tens of ms, which is why the panel waits for *seen-Busy then Ok*. The autofocus search
  does not apply that knowledge. `move_absolute` may be blocking inside OnStepAdapter
  (*uncertain*), which would make this harmless.
- **Owner:** `collimation_tool/application/operation_lifecycle.py`. Step: S6.3.

### K01 / K02 / K03: connect/disconnect handling diverged across panels (LIVE)

- The `7bb7b68` + `ca5bb92` pattern (catch everything, log, record `last_connect_error`,
  expose it in diagnostics) was applied to exactly three panels. The other copies
  disagree:
  - `FilterWheelPanel` catches `Exception` but neither logs nor records the error, and
    `diagnostic_context()` has no `last_connect_error` (`filter_wheel_panel.py:132-144,277-286`).
    This is the "shown-and-lost" defect `ca5bb92` fixed elsewhere.
  - `CameraPanel._on_toggle_stream` calls `self._camera.connect()` with no guard
    (`camera_panel.py:458`). *Uncertain* whether a connected ToupTek adapter's
    `connect()` can raise there.
  - Guide tool `_on_connect_camera` (`guide_tool/ui/main_window.py:196-203`) catches only
    `ConnectionError` (the `7bb7b68` bug class) and never disconnects the previous camera
    (the #45 ERROR_BUSY bug class `CameraPanel` fixed). Its test
    (`test_guide_main_window.py:68`) only raises `ConnectionError`.
- **K02:** the exception set for OnStep operations is written out twice
  (`connection.py:77`, `mount_pulse_adapter.py:138`) and narrowed to `ValueError` in the
  focuser adapter. That is why L02 is reachable.
- **K03:** `disconnect()`/`stop()` are unguarded in every panel, and `closeEvent` has no
  per-panel isolation. `MountParkPanel._enforce_tracking("connect")` can raise
  `RuntimeError` from `stop_tracking()` after `_connected` is already True, which leaves
  the button and label inconsistent.
- **Owner:** `DeviceConnectionService` (S6.2) for the panel side, and `astrotool_core/onstep`
  for the adapter-side exception set.

### D01 / D02 / D03: device identity and config sources (partly LIVE)

- **D01:** `c7c4bb8` corrected the constant but deliberately kept a second literal in
  `registry.py:90` ("an unreachable-in-practice safety net"). CONTRIBUTING.md forbids
  exactly that. The simulator `FakeIndiServer` still defaults to the known-wrong
  "ToupTek EFW 1" (`fake_indi_server.py:5,33`), and `tests/core/indi/test_indi_client.py:13`
  relies on it. The adapter adds a third default, "Filter Wheel" (`:45`). About 15 test
  files hard-code "ToupTek EFW 2" instead of importing the constant, which is why the
  correction touched 8 files.
- **D02:** the same indiserver is addressed as "localhost" by the filter wheel and as
  "127.0.0.1" by OnStep. Port 7624 is stated four times.
- **D03:** each config reader defines its own path constant, so tests must patch consumers
  one by one (`tests/conftest.py`).
- **Owner:** `astrotool_core/config/paths.py` + `indi_endpoint.py`, with
  `filter_wheel/config.py` as the sole EFW identity owner. Step: S6.1, plus the
  config-source contract test #55 asks for (inject an alternate config and assert every
  consumer sees it).

### T05 / T04 / L05 / D07: the "fresh frame after motion" knowledge is split

- Mount Align learned (diagnostic 9ca6daa3) that a fixed 2 s wait cannot cover long
  exposures, and now scales it (`mount_test_move_panel.py:1250-1291`). Autofocus uses a
  fixed 5 s (`autofocus_controller.py:112`). With an exposure above about 2 s,
  `2×exposure+1` is more than 5 s, so autofocus can report `FRAME_ACQUISITION_FAILED`
  on a healthy stream. *Static inference, unverified.*
- Settle after motion is 1000 ms in Mount Align and 750 ms in recentering (T04). Stability
  sampling defaults are stated twice (D07). Auto-exposure pause/resume is done three ways
  (L05).
- **Owner:** `collimation_tool/application/fresh_frame.py`. Step: S6.7.

### C01 / C02: capability handling is repeated ad hoc

- The ToupTek adapter now gates with model flags (`531a952`) and a probe-once E_NOTIMPL
  cache (`4730c55`). Those are two mechanisms for one concept, with eleven hand-written
  `and self._supports_*` guards. `get_descriptor()` still does two SDK range queries on
  every call and is called per frame (`camera_panel.py:431`). If either query ever failed
  persistently, it would reproduce the `531a952` log spam through `_try()`.
- On the mount side, capability is inferred from `hasattr`/`getattr` in five UI places.
  The one declared capability (`supports_pulse_guiding`) is ignored, which is how P01
  went unnoticed.
- **Owner:** an adapter capability table (S6.5) plus explicit `MountCapabilities`/park
  capabilities on the ports.

### Remaining must-centralize items (low risk, no contradiction today)

- **D04** (−10 °C in four places): one default in `camera_settings.py`, and the panel reads it.
- **D05** (train identifiers): today the only safety net is `.lower()` matching
  (`main_window.py:632-646`). *Uncertain:* the SmartTScope role vocabulary is external, so
  the enum must parse it rather than redefine it.
- **D06/M05** (target distance, artificial-star identity): `_calibration_distance_m` uses
  the registration button, so choosing "Artificial star" in fine collimation without also
  switching registration mode seeds calibration at 10 km instead of 30 m. The impact is
  small (per AGENTS.md seeds, about 7 % in arcsec), because the measured shift is
  authoritative.
- **D08:** the 25 % target is stated in both settings and `SizingPolicy`.
- **T02:** the runner re-polls park state with a 5 s budget after the adapter has already
  confirmed it. The panel uses 30 s for the same transition.
- **T06/P04:** retry constants and settings exist only for the dead timed path. Remove or
  re-purpose them once P01 is decided.
- **K04:** the excepthook is duplicated between the two apps.
- **G01/G02:** unit conversions are inline in the panel. They move to the domain with
  S6.6 as pure functions with direct tests.

---

## 3. False positives considered and rejected

| Candidate | Why it is not duplicated knowledge |
|-----------|------------------------------------|
| Poll intervals (T08) | UI refresh cadence per widget, not an external device behaviour |
| `blockSignals(True)/setChecked(False)/blockSignals(False)` in four connect handlers | A Qt idiom. The knowledge is in K01 (what to do on failure), not in the button reset |
| Connect timeouts 5 s (OnStep) vs 10 s (INDI EFW) (T07) | Different drivers and devices |
| `DEFAULT_REMOTE_DIAGNOSTICS_DIR` string (`diagnostics/remote.py:33`) vs `DEFAULT_DIAGNOSTICS_DIR` (`service.py:56`) | The remote one is a path on the Pi, expanded by the remote shell; it is not the local `Path.home()`. Arguably still one fact, but the two are used in different address spaces |
| OnStepAdapter's own 7624/127.0.0.1 defaults (`.venv/.../onstep_adapter/indi_transport.py`) | External package. This repo always passes an explicit `IndiRuntimeConfig` |
| Observer site and device in `testing/fake_onstep_indi_client.py` (D09) | A test double that mirrors the config on purpose |
| Plate-scale formula | One owner already: `optics/smarttscope_config.py:31,48`; `movement_sizing` consumes it via `CameraGeometry` |
| `SIDEREAL_ARCSEC_PER_S` | One owner already: `mount/movement_sizing.py` |
| `hypot`/`atan2`/ms↔s conversions (G03) | Generic arithmetic on different quantities |
| Guide app `DriftEstimator`/`GuideCorrectionPolicy` vs collimation recentering | Intentionally different domain logic (#55 non-goal: do not merge Guide/Collimation domain logic). Only the *movement executor* part is listed (P03) |
| Filter-name table `_BUILT_IN_FILTER_NAMES` / abbreviations | Single owner (`filter_wheel/config.py:43-66`) |
| Focuser step sizes `(1, 10, 100, 200)` | Single owner (`focuser_panel.py:72`), a pure UI choice |
| `_STATUS_MESSAGES`/`_MOTION_STATUS_MESSAGES` tables | Presentation text, single place |
| CameraPanel temperature poll without cooling support (C03) | Documented intentional behaviour; the adapter owns the capability cache |
| `diagnostic_context()` in every panel | Different device state per panel. The *shape* (last error, blockers) is covered by K01 |

---

## 4. `mount_test_move_panel.py`: responsibility map

174,731 bytes, 3,340 lines. One QWidget class, `MountTestMovePanel` (L761-3266, 2,506
lines, 95 methods), plus 16 helper classes/functions. Target layers follow AGENTS.md
"Application layering". Qt panel → application service → port → adapter.

| # | Responsibility | Members today (line) | Where it should live |
|---|----------------|----------------------|----------------------|
| R1 | Widgets, layout, button binding | `__init__` L762 (345 lines, mixes widget construction with about 60 state fields), `_build_direction_pad` L1108, `_build_screen_move_pad` L1131, `_update_buttons_enabled` L3115, `_sync_availability_message` L3154, `_show_capture_progress` L1565, `set_target_mode` L1184 | **UI**: stays. `__init__` shrinks once state moves to services |
| R2 | Device connect/disconnect | `_on_toggle_connect` L1155, `stop` L3252 | **Application**: `DeviceConnectionService` (S6.2) |
| R3 | Operating-mode/tracking policy | `_target_mode` L1181, `_required_tracking_mode` L1189, `_tracking_failure_message` L1202, `_verify_tracking_mode` L1225, local toggle in `__init__` L1010-1024 | **Domain policy**: `TrackingEnforcer` (S6.4). The local toggle is removed |
| R4 | Frame acquisition, freshness, stability, capture validity | `_capture` L1239, `_missing_label` L1247, `_fresh_frame_timeout_s` L1250, `_verified_capture_timeout_s` L1282, `_capture_both` L1293 (83 lines), `_snapshot_capture_request` L1378, `_capture_blocking` L1398, `_apply_capture` L1442 (73), `_reusable_reference` L1516, `_request_capture` L1530, `_run_capture_job` L1557, `_poll_capture_job` L1573, `_pause/_resume_auto_exposure` L1751/L1755; module helpers `_frame_acquisition_result` L421, `_capture_invalid_result` L442, `_stability_evidence_dict` L453, `_VerifiedReference` L479, `_CaptureRequest/_CaptureResult/_CaptureJob` L661-703 | **Application**: fresh-frame-after-motion service (S6.7), shared with autofocus |
| R5 | Failure classification and messages | `MeasurementFailureClass` L378, `_capture_failure_detail` L1651 (48), `_capture_failure_message` L1700, `_STATUS_MESSAGES` L325, `_MOTION_STATUS_MESSAGES` L355, `_degenerate_calibration_message` L3286 (46) | **Application** (classification) plus a small **UI** presenter (text) |
| R6 | Operation lifecycle (idle/running/cancelled/failed, cleanup) | `is_idle` L1587, `_cancel_activity` L1597, `_fail_safe` L1610, `_on_stop` L2981, `_poll` L2998, `_poll_inner` L3009 (56), `_PendingAction` L719, `MountInterfaceState` L644, `interface_state` L3094 | **Application**: `OperationLifecycle` (S6.3). The panel keeps a timer that calls `service.poll()` |
| R7 | Calibration orchestration (step queue, before/move/after, abort, return, matrix) | `_CALIBRATION_STEPS` L631, `_CalibrationStep(Role)` L578-615, `_on_run_calibration_clicked` L1759, `_begin_calibration` L1766, `_start_next_calibration_step` L1792, `_after_before_capture` L1820, `_submit_calibration_step` L1835 (52), `_abort_calibration` L1888 (63), `_finish_calibration_step` L1952 (85), `_after_after_capture` L2038, `_measure_step_responses` L2160, `_record_direction_response` L2495, `_after_axis_group` L2423 (71), `_finish_calibration` L2517 (81), `calibration_for` L2599 | **Application**: `collimation_tool/application/mount_align_calibration.py` (S6.6), a state machine over ports and the S6.7 frame service |
| R8 | Movement sizing, rate learning, arcsec accounting | `_init_sizing` L2220 (71), `_account_arcsec` L2077, `_net_return` L2090, `_learn_rate` L2116 (43), `_mount_supports_angular` L2292, `_installed_rate` L2298, `_step_duration_ms` L2304, `_step_arcsec` L2318, `_is_probe` L2326, `_smallest_fov_key` L2336, `_retry_probe_if_needed` L2345 (56), `_exclude_camera_bounded` L2402, `_is_no_motion` L2200, `_no_motion_text` L2206, `_exclude_camera_no_motion` L2213, `_NO_MOTION_*` L474-475, `_REFERENCE_MAX_AGE_S` L472 | **Domain**: `astrotool_core/mount/movement_sizing.py` (pure functions plus a per-run accounting value object). The rate-learning part is redesigned in S6.8 (P01) |
| R9 | Manual positioning (nudge, screen move) | `_on_nudge_clicked` L2605, `_begin_nudge` L2612 (122), `_after_nudge_before_capture` L2735, `_finish_nudge` L2774, `_after_nudge_after_capture` L2805 (76), `_screen_move_size` L2882, `_screen_move_fraction` L2888, `_on_screen_move_clicked` L2895 (60), `_finish_screen_move` L2956; `ScreenDirection` L533, `MovementSize` L560, `_NudgeMove` L707 | **Application**: positioning commands on the S6.8 movement service. Duration/arcsec maths go to the **domain** (G02) |
| R10 | Response computation and formatting | `_build_response` L3066, `_measure_brightest_source` L3269, `_format_response` L3279, `_response_dict` L3334 | **Domain** (`response_from_positions`, already in `astrotool_core.mount`) plus a **UI** formatter |
| R11 | Diagnostics/evidence | `diagnostic_activity` L1626, `diagnostic_frames` L1712, `diagnostic_camera_state` L1723, `diagnostic_stability_evidence` L1737, `diagnostic_capture_timeline` L3173, `diagnostic_context` L3179 (51), `diagnostic_backlash_evidence` L3231, `_log_motion` L2102 | **Application** (each service exposes its own evidence; #52 item 6). The panel only aggregates |
| R12 | Background mount sequence | `ui/mount_test_move_runner.py` (Qt-free, but placed in `ui/`) | **Application**: move to `collimation_tool/application/`, then fold into the S6.8 service |

**Suggested extraction order.** Each step is small and behaviour-neutral unless marked.
Each step starts with the panel's existing tests green and adds direct service tests
that do not build `MainWindow`.

1. **Move without changing** (S6.6 prep): relocate the Qt-free pieces. Module helpers
   (R5 message tables, `MeasurementFailureClass`, `_degenerate_calibration_message`,
   R10 formatters) and `mount_test_move_runner.py` (R12) move to `application/`, with
   re-exports kept for the tests. Pure moves.
2. **Domain maths** (S6.6): pull R8/G01/G02 conversions into `movement_sizing` /
   `axis_calibration` as pure functions with unit tests. The panel calls them.
3. **Connection lifecycle** (S6.2): R2 behind `DeviceConnectionService`, shared with
   Focuser, MountPark and FilterWheel (K01-K03).
4. **Operation lifecycle** (S6.3): R6 behind `OperationLifecycle`. `_fail_safe` becomes
   the shared terminal cleanup (L02/L04).
5. **Fresh-frame service** (S6.7): R4 plus auto-exposure pausing, then shared with
   `AutofocusController` (T05/L05).
6. **Mode/tracking** (S6.4, *behaviour change*): remove the local toggle (R3/M01),
   route all checks through `TrackingEnforcer` (M02), stop the runner's unconditional
   tracking-off (M03). Each change behind its own failing regression.
7. **Calibration orchestration** (S6.6): R7 becomes an application state machine driven
   by the panel's timer. The panel only renders its state.
8. **Movement path** (S6.8, *behaviour change*): P01/P02/P03. One angular movement
   service for calibration, nudges, screen moves and recentering, sized up front as
   AGENTS.md requires. Delete the timed fallback and its settings (T06/P04). It needs a
   regression that wires the real `OnStepMountPulseAdapter` over `FakeOnStepIndiClient`
   first. Given P01's severity the coordinator may want to move this ahead of steps 6-7.
   That ordering decision belongs to the coordinator or user.
9. **Positioning** (R9) onto the S6.8 service, and evidence (R11) onto each service.

---

## 5. Baseline metrics (at `615f44f`)

### 5.1 File sizes

UI modules (bytes on disk / lines):

| Module | Bytes | Lines |
|--------|------:|------:|
| `apps/collimation_tool/ui/mount_test_move_panel.py` | 174,731 | 3,340 |
| `apps/collimation_tool/ui/main_window.py` | 56,520 | 1,069 |
| `apps/collimation_tool/ui/camera_panel.py` | 53,545 | 1,048 |
| `apps/collimation_tool/ui/focuser_panel.py` | 25,922 | 545 |
| `apps/collimation_tool/ui/fine_collimation_panel.py` | 19,548 | 460 |
| `apps/collimation_tool/ui/mount_test_move_runner.py` (Qt-free) | 18,957 | 393 |
| `apps/collimation_tool/ui/mount_park_panel.py` | 12,756 | 285 |
| `apps/collimation_tool/ui/live_view.py` | 12,515 | 282 |
| `apps/collimation_tool/ui/filter_wheel_panel.py` | 12,273 | 293 |
| `apps/collimation_tool/ui/fov_calibrator.py` (Qt-free) | 11,686 | 275 |
| `apps/guide_tool/ui/main_window.py` | 10,027 | 245 |
| `apps/collimation_tool/ui/frame_analyzer.py` (Qt-free) | 5,663 | 131 |
| `apps/collimation_tool/ui/flow_layout.py` | 4,574 | 118 |
| `apps/guide_tool/ui/live_view.py` | 3,571 | 90 |
| `apps/collimation_tool/ui/autofocus_runner.py` (Qt-free) | 2,907 | 76 |
| `apps/collimation_tool/ui/fov_overlay.py` (Qt-free) | 2,842 | 74 |
| `apps/collimation_tool/ui/fine_collimation_runner.py` (Qt-free) | 2,826 | 76 |

Six modules under `ui/` import no PySide6 at all (runners, `fov_calibrator`,
`frame_analyzer`, `fov_overlay`). They are application code in the UI package.

Largest application/core modules:

| Module | Bytes | Lines |
|--------|------:|------:|
| `packages/astrotool_core/camera/touptek_adapter.py` | 37,039 | 819 |
| `packages/astrotool_core/target/translation_offset.py` | 27,927 | 505 |
| `packages/astrotool_core/registration/terrestrial_registrar.py` | 25,460 | 569 |
| `apps/collimation_tool/application/autofocus_search.py` | 20,941 | 454 |
| `packages/astrotool_core/config/mount_alignment_settings.py` | 18,320 | 352 |
| `packages/astrotool_core/diagnostics/service.py` | 18,198 | 439 |
| `packages/astrotool_core/mount/axis_calibration.py` | 16,873 | 367 |
| `apps/collimation_tool/application/star_acquisition.py` | 15,357 | 311 |
| `packages/astrotool_core/acquisition/auto_exposure.py` | 15,057 | 301 |
| `apps/collimation_tool/application/autofocus_controller.py` | 13,565 | 293 |
| `apps/guide_tool/application/guide_controller.py` | 11,635 | 290 |

Totals: `apps/` + `packages/` production Python = 1,118,258 bytes / 25,594 lines.
`apps/*/ui` + `apps/*/application` = 526,179 bytes / 11,020 lines, of which
`mount_test_move_panel.py` alone is 33 %.

### 5.2 Longest functions/methods (AST `end_lineno − lineno + 1`)

| Lines | Function | Location |
|------:|----------|----------|
| 397 | `MainWindow.__init__` | `apps/collimation_tool/ui/main_window.py:211` |
| 345 | `MountTestMovePanel.__init__` | `apps/collimation_tool/ui/mount_test_move_panel.py:762` |
| 193 | `CameraPanel.__init__` | `apps/collimation_tool/ui/camera_panel.py:162` |
| 147 | `FocuserPanel.__init__` | `apps/collimation_tool/ui/focuser_panel.py:116` |
| 127 | `load_mount_alignment_settings` | `packages/astrotool_core/config/mount_alignment_settings.py:226` |
| 122 | `ArtificialStarRegistrar.register` | `packages/astrotool_core/registration/artificial_star_registrar.py:91` |
| 122 | `MountTestMovePanel._begin_nudge` | `apps/collimation_tool/ui/mount_test_move_panel.py:2612` |
| 116 | `TerrestrialRegistrar.register` | `packages/astrotool_core/registration/terrestrial_registrar.py:454` |
| 103 | `acquire_verified_frame` | `packages/astrotool_core/acquisition/motion_aware_acquisition.py:129` |
| 98 | `TerrestrialRegistrar._search` | `packages/astrotool_core/registration/terrestrial_registrar.py:339` |
| 98 | `MountTestMoveRunner._run_sequence` | `apps/collimation_tool/ui/mount_test_move_runner.py:284` |
| 97 | `FocusedStarAcquisition.attempt_guide_reacquisition` | `apps/collimation_tool/application/star_acquisition.py:181` |
| 85 | `MountTestMovePanel._finish_calibration_step` | `apps/collimation_tool/ui/mount_test_move_panel.py:1952` |
| 85 | `LiveView.set_stretched_frame` | `apps/collimation_tool/ui/live_view.py:184` |
| 83 | `MountTestMovePanel._capture_both` | `apps/collimation_tool/ui/mount_test_move_panel.py:1293` |
| 82 | `AutofocusController.run` | `apps/collimation_tool/application/autofocus_controller.py:150` |
| 81 | `MountTestMovePanel._finish_calibration` | `apps/collimation_tool/ui/mount_test_move_panel.py:2517` |

Six of the top 17 are in `mount_test_move_panel.py`.

### 5.3 Dependency-direction snapshot (UI → core/application)

| UI module | Imports from `astrotool_core` / application | Adapter *implementation* imported directly? |
|-----------|---------------------------------------------|---------------------------------------------|
| `collimation_tool/ui/main_window.py` | acquisition, camera, config, diagnostics, diffraction, filter_wheel (port + registry), focus, frames, mount (ports, `movement_sizing`, `operating_mode`, `no_mount*`), optics, registration (alignment, astap_adapter, optical_prior, result); application: autofocus_controller, star_acquisition; domain: target_mode | **Yes**: `AstapCliSolver` (default construction, `:159,236`) and `camera.list_devices` (ToupTek SDK enumeration, `:135-137`) |
| `collimation_tool/ui/camera_panel.py` | acquisition (`StreamController`, `acquire_stable_frame`, auto-exposure), camera, config, frames; application: collimation_controller; domain: collimation_measurement/state | **Yes**: `TouptekCameraAdapter` (`default_camera_factory`, `:121`) and `list_devices` |
| `collimation_tool/ui/mount_test_move_panel.py` | acquisition (motion-aware, stable-frame), config (`MountAlignmentSettings`), mount (ports, calibration, `movement_sizing` internals, `operating_mode`, `ensure_tracking_mode`), target (`detect_sources`, `measure_translation_offset`) | No (ports only), but it imports **domain algorithms** directly (detection, translation offset, sizing), which is the application-layer work listed in §4 |
| `collimation_tool/ui/mount_test_move_runner.py` | mount ports | No |
| `collimation_tool/ui/focuser_panel.py` | acquisition (result type), focus port; application: autofocus_controller, autofocus_search | No |
| `collimation_tool/ui/mount_park_panel.py` | mount park port, operating_mode | No |
| `collimation_tool/ui/filter_wheel_panel.py` | filter_wheel port | No |
| `collimation_tool/ui/fov_calibrator.py` | registration registrars (artificial-star, star-field, terrestrial), `AstapSolver` port, optical_prior, result | No (registrars are domain algorithms; ASTAP through the port) |
| `collimation_tool/ui/fine_collimation_panel.py` | diffraction; application: fine_collimation_controller, star_acquisition; domain: target_mode | No |
| `collimation_tool/ui/frame_analyzer.py`, `live_view.py` | frames; application: collimation_controller; domain | No |
| `guide_tool/ui/main_window.py` | camera, diagnostics, frames; application: guide_controller; domain: correction_model | **Yes**: `TouptekCameraAdapter`, `list_devices` |

Composition roots (`apps/collimation_tool/main.py` → `astrotool_core.onstep` adapters and
`filter_wheel.registry`; `apps/guide_tool/main.py` → `FakeCamera`) are *supposed* to
import adapters and are not flagged.

**Flagged:** three UI modules construct adapter implementations directly (ToupTek
camera ×2, ASTAP CLI ×1) instead of receiving a factory from the composition root. The
defaults are injectable (constructor parameters), so tests are not affected. Moving the
default into `main.py` is a behaviour-neutral follow-up and belongs with S6.2 (device
construction/lifecycle).

---

## Discovered dependencies (for the tracker)

- **P01/P02** (Mount Align and #39 reacquisition cannot move the real mount on 0.4.x) is
  more severe than an ordinary duplication item. It may deserve its own issue, and the
  coordinator should decide whether S6.8 goes before S6.4/S6.6. It relates to #31/#46
  and possibly to an OnStepAdapter capability question (a verified small-move or
  timed-move primitive). Per AGENTS.md that would be an OnStepAdapter change, not a local
  workaround.
- **M04** needs a user decision: should the autofocus Star/Terrestrial choice follow
  `OperatingMode`?
- **L02** (runners that never clear busy on exception) touches files owned by S6.3. Its
  regression test should be written there first.
