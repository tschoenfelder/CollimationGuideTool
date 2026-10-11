# Movement paths: inventory and competing-path findings (S6.8 analysis)

**Task:** S6.8, issues #55, #31 and #46, checked against the AGENTS.md sections "Mount movement boundary",
"OnStep connection ownership", "Consumer enforcement", "Mount Align movement policy",
"Manual movement versus measurement" and "Operating mode".
**Status:** analysis only. No production or test code was changed. Fixes need their own contract
after S6.6a lands.
**Baseline:** `HEAD` = `b785bf3` (2026-10-11). Every `file:line` below comes from `git show HEAD:<path>`,
not from the working tree, because S6.6a is splitting `mount_test_move_panel.py` concurrently. The
panel line numbers will move once S6.6a lands, but the method names stay valid.
**Installed OnStepAdapter:** 0.5.0 (`.venv/Lib/site-packages/onstep_adapter`). Facts quoted from it are
marked *[OnStepAdapter]*.

Path abbreviations used in the tables:

| Short | Path |
|-------|------|
| `panel` | `apps/collimation_tool/ui/mount_test_move_panel.py` |
| `runner` | `apps/collimation_tool/ui/mount_test_move_runner.py` |
| `pulse_adapter` | `packages/astrotool_core/onstep/mount_pulse_adapter.py` |
| `park_adapter` | `packages/astrotool_core/onstep/mount_park_adapter.py` |
| `focuser_adapter` | `packages/astrotool_core/onstep/focuser_adapter.py` |
| `recenter` | `apps/collimation_tool/application/recenter_policy.py` |
| `acq` | `apps/collimation_tool/application/star_acquisition.py` |
| `gpr` | `apps/collimation_tool/application/guide_pulse_reacquisition.py` |
| `mw` | `apps/collimation_tool/ui/main_window.py` |

---

## 0. Summary

| Count | What |
|------:|------|
| 25 | motion/tracking/park/focuser paths inventoried (section 1) |
| 7 | mount RA/Dec motion paths reachable in production (M1–M7) |
| 2 | production executors for the same intent, "move the mount by an angle on an axis": the Mount Align runner and the recenter policy (finding F2). A third one, GuideTool's, is dormant. |
| 0 | raw serial, LX200 or INDI OnStep accesses outside OnStepAdapter (section 1.6, verified) |
| 13 | findings: 3 HIGH, 5 MEDIUM, 5 LOW (section 2) |

The three HIGH findings:

1. **F1, M03 still LIVE.** Before every Mount Align move, the runner sends OnStep's *emergency stop*
   (`ABORT` + `TRACK_OFF`), whatever the mode. In Astronomical mode this contradicts the
   reacquisition's own rule ("stopping tracking ... is not this workflow's decision", `gpr:13-17`).
   A **screen move in Astronomical mode silently leaves the mount not tracking**, because nothing
   re-enables tracking afterwards.
2. **F2, two production movement executors.** They have different preconditions (park check,
   tracking, Stop, settle). The duplication audit's P03 is still live, but P01/P02 are fixed
   (`787ceed`, `f24a523`, `feb46e5`).
3. **F3, a park slew has no in-app Stop.** Mount Align's Stop button is enabled only while Mount
   Align itself is busy (`panel:3455`), and MountParkPanel has no Stop button.

---

## 1. Inventory

Legend for the primitive column: **angular** = `OnStepMountPulseAdapter.move_angular` →
`IndiMount.move_ra_axis_deg` / `move_dec_axis_deg`. This is a blocking finite GOTO of 30″–36000″ that
holds `operation_lock` for the whole move. *[OnStepAdapter]* `indi_axis_motion.py:85-97` refuses a
parked, slewing or **tracking** mount, and `:205-221` issues `TRACK_OFF` itself at the end of the move.
**guide** = `OnStepMountPulseAdapter.guide_pulse` → `IndiMount.guide_pulse`. This is a 0.5.0 timed
guide with tracking kept on, sent in 500 ms chunks. **timed** = `pulse_axis`. It **always refuses** on
the production adapter (`pulse_adapter:532-540`, `capabilities().supports_pulse_guiding=False`,
`:225-235`).

### 1.1 Mount RA/Dec motion, production

| # | Entry point | Call chain at HEAD | Primitive | Preconditions checked | Stop/abort coverage | Tests |
|---|-------------|--------------------|-----------|-----------------------|---------------------|-------|
| M1 | Mount Align → *Run Calibration*, measured steps (FIRST/REPEAT/growth probes) | `panel._begin_calibration` 1843 → `_start_next_calibration_step` 1869 → BEFORE capture (`_capture_both` 1357, tracking verify 1473) → `_submit_calibration_step` 1912 → `runner.submit_sequence` 324 → `_run_sequence` 398 → fresh park read 433, then **`unpark()` 443 or `stop_tracking()` 445** → `_move_with_retry` 246 → `move_angular` `pulse_adapter:355` | angular (production). The timed branch is reachable only on a mount that reports timed pulses. | Global mode via `TrackingEnforcer.verify` before the BEFORE capture and after every step (`panel:1269`, `:2103`). Floor from `capabilities().min_angular_arcsec` (`panel:2418-2427`). Size from the #46 25 % seed (`_init_sizing` 2300). Fresh park reading (S6.0c). No mode check inside the runner itself. | Stop → `runner.abort` 527 + `pulse_adapter.abort` 274 (lock-free emergency stop; latched cancel refuses the next step). All runner waits take the Stop event (S6.3). A running GOTO ends at OnStepAdapter's own deadline (≤ 30 s). | `tests/collimation/ui/test_mount_align_real_onstep_adapter.py` (Calibration/RealisticOptics/StopDuringABlockingAngularGoto), `test_calibration_angular.py`, `test_calibration_sizing.py`, `test_calibration_failure_recovery.py`, `test_mount_test_move_runner*.py`, `tests/core/onstep/test_onstep_adapters.py::TestStopDuringAngularGoto` |
| M2 | Calibration RETURN steps (unmeasured, part of `_CALIBRATION_STEPS`) | as M1. No BEFORE capture (`panel:1887-1890`). | angular | As M1. Tracking re-verified after the step (`panel:2103`). | As M1 | as M1 |
| M3 | Calibration failure → "stranded return" | `_abort_calibration` 1965 → `_return_chunks` 2429 → `runner.submit_sequence` 2019 (fire-and-forget; return value ignored) | angular | Floor only: sub-floor residuals are reported as "not returned". **No tracking re-verify after it** (`_poll_inner` 3374-3388). | Stop as M1. After Stop no return is sent and the net offset is reported (`_on_stop` 3289-3301). | `test_calibration_failure_recovery.py`, `test_calibration_angular.py` |
| M4 | RA±/Dec± nudge (uncalibrated: sized from optics; calibrated: sized from the matrix) | `_on_nudge_clicked` 2822 → `_begin_nudge` 2829 → BEFORE capture or reused reference (`_reusable_reference` 1587) → `_after_nudge_before_capture` 2977 → `runner.submit_sequence` 2994 → as M1 → AFTER capture + correlation (`_after_nudge_after_capture` 3056) | angular | Cap `_nudge_cap_arcsec` 2488 (derived from a **rate-preset table**, see F11). Floor refusal before sending (`:2937-2944`). Tracking verify on each capture. **A BEFORE frame is required: with none, nothing is sent** (`:2887-2891`, `:2980-2986`). | As M1 | `test_mount_align_real_onstep_adapter.py::TestManualMovesMoveTheRealAdapter`, `test_mount_test_move_panel_fresh_frames.py`, `test_calibration_frame_validity.py` |
| M5 | Screen move (#31 Phase D: up/down/left/right by frame fraction) | `_on_screen_move_clicked` 3146 → `solve_screen_move` (`astrotool_core/mount/axis_calibration.py`) → `_plan_angular_screen_move` 3221 → `plan_angular_move` (`application/angular_move_plan.py`) → `runner.submit_sequence` 3205 → as M1 | angular | Whole plan validated before sending (floor/cap/drop rule shared with M6). **No tracking verify at all** (no capture by design, `_finish_screen_move` 3255). | As M1 | `test_mount_align_real_onstep_adapter.py::...test_a_screen_move_*`, `tests/collimation/application/test_angular_move_plan.py`, `tests/core/mount/test_axis_calibration.py::TestSolveScreenMove` |
| M6 | Fine collimation lost the star → guide-assisted reacquisition, Terrestrial or Astronomical with tracking OFF | `FineCollimationRunner` → `FineCollimationController.run` → `mw._reacquire_via_guide` 810 → `acq.attempt_guide_reacquisition` 306 → `gpr.choose_reacquisition_motion` 111 (decision at `acq:365`) → `rearm_after_stop` `acq:453` → `CollimationRecenterPolicy.center` (`acq:467-475`) → `recenter._angular_correction` 347 → `move_angular` | angular | `measurement_gate("fine_collimation")` once at start (`fine_collimation_panel.py:300`). Mode and fresh tracking reading up front. Floor/cap/drop plan. **No park check and no tracking command.** | Fine-collimation *Cancel* → `lifecycle.cancel()` only. `cancel_check` runs **between corrections** (`recenter:226`), not between the ≤ 2 steps of one plan, and not during the 750 ms settle (`recenter:292-293`, no cancel). **No `mount.abort()`**: a running GOTO is not stopped by Cancel. | `tests/collimation/application/test_recenter_policy_simulated.py`, `test_star_acquisition_simulated.py`, `test_recenter_policy.py`, `test_angular_move_plan.py`, `test_equivalent_duration.py` (proof `39-reacquisition-angular`) |
| M7 | Same entry point, Astronomical with tracking ON and 0.5.0 capability | `gpr.calibrate_guide_pulses` 169 (probes, `guide_pulse` call `gpr:214`) then `recenter._guide_correction` 306 → `pulse_adapter.guide_pulse` 396 | guide | Mode re-read between pulses (`operating_mode_changed`). Fresh tracking reading, fail-closed. Bounds from the installed package. `tracking_off` is detected after a failed pulse. Never starts or stops tracking itself. | Cancel checked before **each** pulse/probe (`recenter:284`). A pulse in flight runs to its end (≤ 5 s). A Stop latched by Mount Align is honoured (`pulse_adapter:417-425`). Cancel itself sends no stop. | `tests/collimation/application/test_guide_pulse_reacquisition.py`, `test_star_acquisition_guide_pulse_simulated.py`, `tests/core/onstep/test_onstep_simulator.py::TestGuidePulse*`, `tests/contracts/test_onstep_guide_pulse_contract.py` (proof `39-astro-guide-pulse`) |

### 1.2 Stop / emergency stop

| # | Entry point | Chain | Effect | Tests |
|---|-------------|-------|--------|-------|
| S1 | Mount Align *Stop* (enabled only while Mount Align is busy, `panel:3455`) | `panel._on_stop` 3281 → `runner.abort` 527 + `pulse_adapter.abort` 274 → stop worker → `IndiMount.stop()` = `emergency_stop` | `ABORT` + `TRACK_OFF` in **every** mode. Lock-free, so it also stops M6/M7 and any GOTO on the shared adapter. | `test_mount_align_real_onstep_adapter.py::TestStopDuringABlockingAngularGoto`, `test_onstep_adapters.py::TestStopWorkerSafety` |
| S2 | Global Operating Mode changed during a Mount Align run | `mw._apply_operating_mode_to_ui` 760 → `panel.operating_mode_changed` 1221 → `_on_stop` | as S1 | `tests/collimation/ui/test_operating_mode_ui.py` |

### 1.3 Park / unpark / tracking

| # | Entry point | Chain | Primitive | Preconditions | Stop coverage | Tests |
|---|-------------|-------|-----------|---------------|---------------|-------|
| T1 | MountParkPanel *Park* | `mount_park_panel.py:201` → `_begin_action` 173 (worker thread) → `park_adapter.park` 83 → `IndiMount.park()` | park slew | OnStepAdapter's `home_motion_enabled` gate. Buttons disabled while in flight. | **None in the app** (F3) | `test_mount_park_panel_simulated.py`, `tests/contracts/test_mount_park_contract.py` |
| T2 | MountParkPanel *Unpark* | `:204` → `park_adapter.unpark` 109 → then `enforce("unpark")` (`mount_park_panel.py:264/292`) | switch only, not a slew (`park_adapter:113-123`) | #44 enforced after it | n/a (short) | as T1, `test_operating_mode_ui.py::TestConnectEnforcement` |
| T3 | Runner auto-unpark (mount parked when a Mount Align move starts) | `runner:442-443` → `park_adapter.unpark` | switch | Fresh park reading. **No #44 `enforce("unpark")`, unlike T2** (F12). | Stop ends the confirmation wait | `test_mount_test_move_runner.py` |
| T4 | Runner "tracking off" before every Mount Align move (M03) | `runner:444-445` → `park_adapter.stop_tracking` 131 → `IndiMount.stop()` = **emergency stop** | `ABORT` + `TRACK_OFF` | **None: mode-independent** (F1) | n/a | `test_mount_test_move_runner.py:368,506`. No test for Astronomical mode. |
| T5 | TrackingEnforcer `enforce`/`verify` → `ensure_tracking_mode` (`astrotool_core/mount/tracking_mode.py:75`, commands at `:129/:131`) | Callers: mode change `mw:747`; connect/unpark `mount_park_panel.py:65,157`; `measurement_gate` `mw:794` (fine collimation, autofocus, FOV calibration); Mount Align capture verify `panel:1269` (enforcer) **or `:1276` direct `ensure_tracking_mode` (M02)**; P-a retry `mw:775` | `start_tracking` (`enable_tracking`) / `stop_tracking` (emergency stop) | Terrestrial ⇒ OFF, refuses ON. Fail-closed on a busy mount. | The busy wait is bounded. `cancel=` is not yet wired from Stop (S6.6b item). | `tests/core/mount/test_operating_mode.py`, `test_tracking_mode.py`, `test_operating_mode_ui.py` |
| T6 | App quit | `mw.closeEvent` 1141 → `stop_each`: Mount panel `stop` 314 → `DeviceConnectionService.disconnect(cancel_motion=True)` → `stop_tracking` (emergency stop, **takes `operation_lock`**) → … → Mount Align `panel.stop` 3592 (**no runner/adapter abort**) | emergency stop | — | Waits behind a running GOTO (F8) | `test_collimation_main_window.py` |
| T7 | Runner `park_after=True` re-park | `runner:500-516` | park slew | — | Stop skips it | **Test-only**: production always passes `park_after=False` (`panel:1928,1937,2027,2990,2997,3207`) |

### 1.4 Focuser motion

| # | Entry point | Chain | Thread | Stop coverage | Tests |
|---|-------------|-------|--------|---------------|-------|
| F-a | Focuser *In*/*Out* jog (steps 1/10/100/200) | `focuser_panel.py:326/329` → `_begin_move` 315 → `focuser_adapter.move` 140 → under **`operation_lock`** → `IndiFocuser.move_absolute` (*[OnStepAdapter]* `indi_focuser.py:78-135`, **blocks until reached, ≤ 30 s**) | **GUI thread** | See F-b | `tests/collimation/ui/test_focuser_panel.py`, `test_focuser_panel_simulated.py`, `tests/contracts/test_focuser_contract.py` |
| F-b | Focuser *Stop* | `focuser_panel.py:332` → cancels autofocus, then `focuser_adapter.stop` 180 → under **`operation_lock`** → `IndiFocuser.stop` | GUI thread | **Waits for the lock held by the move it should stop** (F6). OnStepAdapter's own `stop()` sets `_cancel_requested` without its motion lock, so it is designed to interrupt a running move. | no Stop-during-move test via the real adapter shape |
| F-c | Autofocus search | `focuser_panel.py:367` (gate `measurement_gate("autofocus")` 373) → `AutofocusRunner` → `autofocus_search.py:414` `move_absolute` | worker | Cancel between steps and in settle/frame waits. The current move runs to its end. | `tests/collimation/application/test_autofocus_search.py`, `test_autofocus_controller_fresh_frames_simulated.py`, `test_autofocus_artificial_star.py`, `tests/collimation/ui/test_autofocus_runner.py` |
| F-d | Autofocus return-to-start | `autofocus_controller.py:304` `move_absolute` + poll | worker | as F-c | as F-c |
| F-e | App quit | `FocuserPanel.stop` 581 → cancel → `focuser.stop()` | GUI | as F-b | `test_collimation_main_window.py` |

### 1.5 Dormant, dead or not shipped

| # | Path | Why it does not move the real mount | Kept alive by |
|---|------|-------------------------------------|---------------|
| D1 | GuideTool `GuideCorrectionPolicy.send` → `pulse_axis` (`apps/guide_tool/application/correction_policy.py:25`) | `guide_tool/main.py` wires `FakeCamera` and no mount. `GuideController(camera, measure_only=True)` (`guide_tool/ui/main_window.py:113,228`). Even if wired, the production adapter refuses `pulse_axis`. It has **no mode, tracking or Stop handling**. | `tests/guide/application/test_correction_policy.py` |
| D2 | GuideTool `run_calibration` → `calibrate_axes` → `pulse_axis` (`astrotool_core/mount/axis_calibration.py:80,107`) | no UI caller (only re-exported in `guide_tool/application/__init__.py:7`) | `tests/guide/application/test_calibration_controller.py`, `tests/core/mount/test_axis_calibration.py`, `tests/integration/test_axis_calibration_replay.py` |
| D3 | Runner timed path: `_pulse_with_retry` 200, timed fallback `runner:267-275` (`MOTION_TIMED_FALLBACK`), `rate_preset` plumbing | `supports_pulse_guiding=False` on the only production mount. `_move_with_retry` returns the refusal unmasked (`:267-270`, `:276-277`). | `test_mount_test_move_runner.py:628`, `test_calibration_angular.py:236` (fakes that *accept* pulses) |
| D4 | Mount Align timed bootstrap + `_learn_rate` 2196 → `install_rate` (`panel:2225`; adapter `:345-353` "accepted but never consulted") | Active only if the mount has angular moves **and** timed pulses (`_rate_for` 2464-2473). Production is angular-only. | `test_calibration_angular.py::TestBootstrapThenAngular` |
| D5 | Recenter timed correction `recenter:335-345` | as D3 | `test_recenter_policy.py::TestTimedPathCharacterization` (strict xfail on rotation) |
| X1 | `scripts/onstep_adapter_uat.py`: Mount Pulse section `pulse_axis` :217; park/unpark/stop tracking; focuser `move_absolute` :314 | Not shipped. Its pulse section **can never move the mount** on 0.4+/0.5.0 (always refused). It cannot exercise `move_angular` or `guide_pulse`. | — |
| X2 | `scripts/terrestrial_focus_sweep.py:102,129` `move_absolute` | Not shipped. Focuser only, via `OnStepFocuserAdapter` (boundary respected). | — |

### 1.6 Raw OnStep / INDI / serial access: none found

- **Import contract:** `pyproject.toml:154-161` (import-linter) forbids `onstep_adapter` and `serial` in
  `astrotool_core`, `collimation_tool` and `guide_tool`, except `astrotool_core.onstep.*` and the
  test double. The only `onstep_adapter` importers at HEAD are `onstep/connection.py:35`,
  `focuser_adapter.py:24`, `mount_park_adapter.py:20`, `mount_pulse_adapter.py:85-87`,
  `onstep/settings.py:19` and `testing/fake_onstep_indi_client.py:48-49`.
- **Token scan:** the boundary guard is **`tests/collimation/test_onstep_boundary.py`**. The task brief
  says `tests/contracts/…`, but no such file exists. I re-ran its scan with an extended token set:
  `TELESCOPE_MOTION_*`, `_SLEW_RATE`, `_PARK`, `_TRACK_STATE`, `_ABORT_MOTION`, `FOCUS_ABORT_MOTION`,
  `TELESCOPE_TIMED_GUIDE_*`, `ABS/REL_FOCUS_POSITION`, `ON_COORD_SET`, `EQUATORIAL_EOD_COORD`,
  `LX200 OnStep`, `indi_lx200_OnStep`. The only hits are the allowed `[indi] device` default
  (`onstep/settings.py:111`), its test double (`fake_onstep_indi_client.py:762`), and docstring or
  comment mentions of `ABS_FOCUS_POSITION` in `indi/client.py:15`, `testing/fake_indi_server.py:57`
  and `testing/sim_onstep.py:109,256`. None of these is a command.
- **Raw transports:** `serial`, `PyIndi`, `OnStepClient` and LX200 `:Mg…#` strings: none. `socket` appears
  only in `astrotool_core/indi/client.py` (generic hand-rolled INDI client). Its only production user is
  `filter_wheel/indi_filter_wheel_adapter.py:50,93` (EFW, allowed by AGENTS.md), plus test servers.
- **Unused OnStepAdapter motion APIs:** `IndiMount.go_home`, `goto`, `move_ra/dec`
  (*[OnStepAdapter]* `indi_mount.py:23,65-86`) are not called anywhere in the app.

**Conclusion:** the AGENTS.md boundary holds. No INDI slew-rate presets, no `TELESCOPE_MOTION_*`
toggles, no sleep-timed motion and no raw directional stops exist in this repository. The remaining
boundary-adjacent debt is policy (F1, F2, F11), not transport.

---

## 2. Findings

Severity reflects risk on the real rig. "LIVE" means reachable in production today.

### F1 HIGH, LIVE: M03 is unchanged. Mount Align emergency-stops before every move, whatever the mode

- **Where:** `runner:442-460`. If the mount is parked, the runner calls `unpark()`. Otherwise it calls
  `stop_tracking()`. That is `park_adapter:131-143` → `IndiMount.stop()` = `emergency_stop`
  (`ABORT` + `TRACK_OFF`), because OnStepAdapter 0.5.0 has **no bare tracking-off API**
  (*[OnStepAdapter]* `indi_mount.py`: only `stop`/`enable_tracking`).
- **Is the tracking-off itself necessary?** For the **angular** primitive, yes. OnStepAdapter refuses a
  local axis move on a tracking mount (`indi_axis_motion.py:85-97`) and turns tracking off itself at the
  end of the GOTO (`:205-221`). So the runner's command is a precondition of the primitive, not
  arbitrary. What is wrong is **who decides, and with which primitive**:
  - **Terrestrial:** redundant. TrackingEnforcer already keeps tracking OFF. The cost is a routine
    *emergency* stop before every step, and an `ABORT` that could cut short any other motion in flight
    on the shared connection (see F7).
  - **Astronomical:** Mount Align requires tracking **ON** for measurement
    (`TrackingEnforcer.measurement_tracking`, `operating_mode.py:100-104`; `panel:1244-1254`). Each
    measured step therefore runs: verify ON (may call `start_tracking`) → runner emergency stop → GOTO
    (ends with `TRACK_OFF`) → re-verify after the step (`panel:2103`), which calls `start_tracking` again.
    `start_tracking` needs `tracking_authority_policy = "controller_managed"` on the rig
    (`park_adapter:145-159`). Otherwise every Astronomical calibration fails at the first re-verify.
  - **Astronomical screen move (M5):** no capture, so no re-verify. **The mount is left not tracking
    with no message.** The same holds for the stranded return (M3). A nudge (M4) is re-enabled only
    because its AFTER capture verifies tracking.
- **Competing policy:** reacquisition decides the opposite for the same situation. `gpr:13-17`:
  Astronomical + tracking ON ⇒ use guide pulses or *refuse*, because "stopping tracking to make it
  possible is not this workflow's decision (#44)". Mount Align stops tracking silently. Two workflows,
  two answers to "may a move stop tracking?".
- **Bypass of the #44 owner:** T4 changes tracking outside `TrackingEnforcer`, so there is no trail
  entry in the diagnostics `tracking_policy` evidence. (#44 itself, Terrestrial ⇒ OFF, is *not*
  violated, because the effect is OFF.)
- **Tests:** no test covers Astronomical Mount Align around a move, or tracking after an Astronomical
  screen move (the audit already noted this under M03). Confidence: high for the static chain.
  Real-rig behaviour is unverified: Mount Align motion has not run on the rig since 0.4.0.
- **Open decision for the user:** in Astronomical mode, should Mount Align
  (a) calibrate and move with guide pulses (tracking kept, like M7; the matrix would then be in
  guide-pulse ms, a different unit from the angular calibration),
  (b) refuse motion while tracking (like M7 without capability), or
  (c) keep stop → GOTO → restart tracking, but explicitly, through the enforcer, and also after screen
  moves and returns?

### F2 HIGH, LIVE: two production movement executors for one intent (#31/#46; audit P03)

| Aspect | Mount Align runner (M1–M5) | Recenter policy (M6/M7) |
|--------|----------------------------|-------------------------|
| Park check / auto-unpark | yes (fresh reading, `runner:433-443`) | **no**: OnStepAdapter refuses a parked mount and the reason is reported |
| Tracking handling | emergency stop before every move (F1) | none. Decided once up front (`gpr:111-128`). |
| Stop | runner event + adapter latch; every wait interruptible | `cancel_check` between corrections only. Settle not cancellable (`recenter:292-293`). No adapter abort. |
| Settle | `settle_ms` from `[mount_alignment]` | `RecenterConfig.settle_ms = 750` (`recenter:129`) |
| Re-arm after Stop | `rearm_after_stop` on an accepted submit (`runner:365`) | `rearm_after_stop` (`acq:453`) |
| Planning | `plan_angular_move` (screen moves) | `plan_angular_move` (shared, S6.0b) |

The planning layer (`angular_move_plan.py`, `equivalent_duration.py`) is already shared, which is
good. The **executor** is not. AGENTS.md "Mount movement boundary" steps 3–6 are implemented twice,
with different safety envelopes. D2 is a dormant third executor. P01/P02 themselves (timed path on the
production mount) are **fixed**: `787ceed` (#46 manifest), `f24a523` (S6.0b) and `feb46e5` (S6.0d).

### F3 HIGH, LIVE: a park slew has no in-app Stop

T1 is a real slew lasting seconds to minutes (`park_adapter:83-101`). MountParkPanel has only
Connect, Park, Unpark and Confirm-home buttons (`mount_park_panel.py:80-103`). The only lock-free stop,
`pulse_adapter.abort`, is reachable only through Mount Align's Stop, and that button is enabled only
while **Mount Align** is busy (`panel:3455`). The fallback is the hand controller or a power cut. This
is uncertain only in that OnStepAdapter's park may have its own internal timeout. It still offers no
operator abort.

### F4 MEDIUM, LIVE: a manual nudge still waits for measurement frames (AGENTS.md "Manual movement versus measurement")

A nudge (M4) needs a BEFORE capture or a reused verified reference, and it **refuses to move when no
frame or star is available** (`panel:2887-2891`, `:2980-2986`). It then blocks the panel on a stable AFTER capture
and runs a correlation (`_finish_nudge` 3025, `_after_nudge_after_capture` 3056). AGENTS.md says manual
movement "must not wait for stable BEFORE/AFTER measurement frames" and must "work before a
CalibrationMatrix exists". The uncalibrated path works, but only if a frame exists. Screen moves (M5)
already follow the rule. So two manual-movement behaviours compete in one panel. A user decision
already exists ("return immediately, #43 check in background", S6.6b list). **Owner: S6.6b.**

### F5 MEDIUM, LIVE: reacquisition Cancel does not stop the mount

Fine-collimation *Cancel* (`fine_collimation_panel.py:325`) only sets the lifecycle cancel. A running
angular GOTO (≤ 30 s) or guide pulse (≤ 5 s) continues, and the 750 ms settle and the second step of a
2-axis plan are not checked. This is already listed as a gap in `proofs/39-reacquisition-angular.toml`,
but no step owns it.

### F6 MEDIUM, LIVE: focuser Stop cannot interrupt a focuser move, and a jog blocks the GUI thread

`focuser_adapter.move`/`move_absolute` hold `operation_lock` around OnStepAdapter's **blocking**
`IndiFocuser.move_absolute` (polls until reached, ≤ 30 s). `focuser_adapter.stop` takes the same
lock (`:180-189`), so it runs only **after** the move it should stop has ended. The jog runs on the
GUI thread (`focuser_panel.py:324`), so the window freezes for the travel time, and also behind any
mount GOTO: `proofs/49-stop-during-angular-goto.toml` records that both "a focuser move still queues
behind the goto" and "FocuserPanel's Stop ... still wait behind a running mount GOTO". OnStepAdapter's
`IndiFocuser.stop()` is built for concurrent use (`_cancel_requested`, no motion lock), so the app's
lock defeats it. This is the same pattern S6.0c fixed for the mount. Practical impact is smaller
(jog steps ≤ 200, short travel), but autofocus moves and a stuck-Busy driver hit the full 30 s.
Confidence: high from static reading of both sides. No test drives Stop during a focuser move through
the real adapter shape.

### F7 MEDIUM, LIVE (uncertain reachability): no single in-flight mount motion across workflows

Mount Align (runner), reacquisition (fine collimation) and MountParkPanel (park/unpark) share one
`OnStepMountPulseAdapter`/`OnStepMountParkAdapter` pair but are not mutually exclusive. Only
individual commands are serialized by `operation_lock`. A Park clicked mid-calibration slews between
steps. A runner `stop_tracking` (emergency stop, F1) sent while a reacquisition GOTO is queued or
finishing aborts it. `rearm_after_stop` from one workflow clears a Stop latched by another. The
runner's own event still blocks Mount Align, so this cross-effect is limited. *Uncertain:* whether the
#36 tab layout makes simultaneous operation practical. I did not check this in the UI.

### F8 MEDIUM, LIVE: quitting during a GOTO stops the mount late

`closeEvent` stops the Mount panel first. Its `disconnect(cancel_motion=True)` → `stop_tracking` →
emergency stop **under `operation_lock`**, so on the GUI thread it waits up to 30 s behind a running
Mount Align GOTO. Only then is the mount stopped. Mount Align's own `stop()` (`panel:3592`) never calls
`_on_stop` or the lock-free `abort()` (audit L03, still open). *Uncertain:* whether
`DeviceConnectionService` runs `cancel` on the GUI thread. That is likely given `stop_each`, but I did
not trace it.

### F9 LOW: dead timed-motion code and settings (audit P04, still live)

D3, D4, D5, `pulse_adapter.install_rate/installed_rate` (`:345-353`), `pulse_axis` (`:532`),
`_NO_PULSE_PRIMITIVE`, `MOTION_TIMED`/`MOTION_TIMED_FALLBACK`, `park_after=True` (T7), and settings
`pulse_ms`, `rate_preset`, `calibration_rate_preset` (`config/mount_alignment_settings.py:196-215`)
plus `RATE_PRESET_X` (`mount/movement_sizing.py:34`). They are alive only through fakes that accept
timed pulses. The fake contract encodes a mount that production no longer has (audit P01 "How it
diverged").

### F10 LOW: the GuideTool correction policy (dormant) would bypass every movement rule if wired

D1 has no Operating-mode check, no tracking handling and no Stop, and uses `pulse_axis`, which
production refuses. It is harmless today (no mount wired). Wiring a mount must go through the shared
executor (F2) and `guide_pulse` (Astronomical, tracking on), not `pulse_axis`.

### F11 LOW, LIVE: a residual app-side rate model sizes the nudge cap

`_nudge_cap_arcsec` (`panel:2488-2497`) = `max_nudge_pulse_ms` × the *slew-rate preset table* rate
for `rate_preset` "7". The live angular cap (~2166″) is therefore derived from an INDI preset table.
AGENTS.md forbids "maintaining its own duplicate mount-rate calibration model". It is harmless as a
number, but it is the wrong owner, and it is coupled to the dead timed settings in F9.

### F12 LOW: runner auto-unpark skips the #44 unpark enforcement

T2 enforces tracking after an unpark, but T3 does not. OnStep's delayed auto-tracking after `UNPARK`
(the runner docstring at 36-44 describes it) could turn tracking on before the GOTO. That GOTO would
then be *refused* (OnStepAdapter precondition) rather than move with tracking, so this is not unsafe.
It would surface as a confusing refusal. *Uncertain:* whether 0.5.0's `unpark()` still triggers the
quirk, because it confirms "unparked with tracking off".

### F13 LOW: stale movement documentation (audit P05, still live)

- `runner:7,33,91,420` name the removed `IndiMountPulseAdapter`/`IndiMountParkAdapter`.
- `apps/collimation_tool/main.py:110` says "moves below 720″ are refused", but the real floor is 30″
  (`pulse_adapter:102`).
- `mount_park_panel.py:186` says "unpark also drives it home". That contradicts `park_adapter:113-123`.
- `park_adapter:131-136` calls `closeEvent` the "real caller" of `stop_tracking`. The busiest caller is
  the runner before every move.
- Boundary-test gaps: the token list lacks `TELESCOPE_TIMED_GUIDE_*`, `ABS/REL_FOCUS_POSITION` and
  `ON_COORD_SET`. Nothing restricts who may import the generic `astrotool_core.indi.client`, which could
  reach the OnStep device. Adding `ABS_FOCUS_POSITION` would trip on three docstrings, so those need
  rewording first.

---

## 3. Recommended fixes

Each fix needs a failing regression first (#52 rule). Unless a row says otherwise, implementation waits
for S6.6a.

| # | Fix | Findings | Owner |
|---|-----|----------|-------|
| R1 | **User decision first** (F1 options a/b/c). Then one rule, owned by `TrackingEnforcer`, answers "may this move stop tracking, and who restores it?". The runner stops calling `stop_tracking()` on its own. Terrestrial: no per-move stop (the enforcer holds OFF; a tracking mount gets refused by the GOTO precondition and reported). Astronomical: per the decision, never silently. Regression tests: an Astronomical screen move must leave tracking as the policy says; there must be no emergency stop before a Terrestrial move. | F1, F12 | **S6.8 fix** (enforcer extension with S6.4's owner) |
| R2 | Request **OnStepAdapter `disable_tracking()`** (a bare `TRACK_OFF` without `ABORT`), so tracking-off stops being an emergency stop. This is external, so record it in the OnStepAdapter tracker and do not implement it locally (AGENTS.md). | F1, F7 | **new** (external request) |
| R3 | One application service, `collimation_tool/application/mount_motion.py`. It owns the angular/guide primitive choice (from capabilities + R1), the park check, the floor/cap validation (it calls `angular_move_plan`), interruptible settle, the Stop latch/re-arm, and **one in-flight mount motion app-wide** (busy refusal across Mount Align, reacquisition and park). The runner and `CollimationRecenterPolicy` become callers. | F2, F5, F7 | **S6.8 fix** |
| R4 | Wire fine-collimation Cancel to the shared lock-free Stop (via R3). Check cancel between plan steps and inside the settle. | F5 | **S6.8 fix** (falls out of R3) |
| R5 | A **global mount Stop**: always enabled while any mount motion is in flight, park included, calling the lock-free `abort()`. Park/unpark go through R3's in-flight tracking. | F3 | **new** (UI + R3) |
| R6 | Nudge returns immediately; the #43 displacement check runs in the background; a missing frame never blocks the move. | F4 | **S6.6b** (already on its list) |
| R7 | Apply the S6.0c pattern to the focuser: lock-free `stop()` (call OnStepAdapter's concurrent `IndiFocuser.stop`), jog off the GUI thread, reads that don't queue. Regression: Stop during a simulated long move returns at once and the move ends early. | F6 | **new** (S6.0c-like step for the focuser) |
| R8 | Mount Align `stop()` calls `_on_stop()` (lock-free abort) **before** the Mount panel's disconnect, or `closeEvent` aborts all motion first. | F8 | **S6.8 fix** (small; or S6.3 follow-up) |
| R9 | Delete the timed path: D3/D4/D5, `install_rate`/`installed_rate`, `pulse_axis` on the OnStep adapter (or keep it only on `MountPort` for fakes until GuideTool is wired), `park_after=True`, and the timed settings (P04, with a config-migration note). Move the fakes to the production contract (angular-only). Replace the F11 nudge cap with an explicit arcsec setting. | F9, F11 | **S6.8 fix** (after R3) |
| R10 | GuideTool: keep it dormant, but mark `GuideCorrectionPolicy`/`run_calibration` as "must route through R3 + `guide_pulse` before wiring a mount". Update `scripts/onstep_adapter_uat.py` to exercise `move_angular`/`guide_pulse` instead of `pulse_axis`. | F10, X1 | **new** (low priority) |
| R11 | Stale texts (F13). Extend the boundary token list and add an import-linter contract limiting `astrotool_core.indi.client` to `filter_wheel` + `testing`. | F13 | **S6.8 fix** (docs/tests only); the panel texts go to **S6.6b** ("stale rate-preset texts") |

### Uncertainty register

- **Not hardware-verified at all:** every Mount Align motion path since the 0.4.0 migration (memory,
  `46-mount-align-angular-path` field list), the Astronomical guide-pulse path through this app
  (`39-astro-guide-pulse`), and F1's real effect (depends on `tracking_authority_policy` on rasppi3).
- **F7/F8 reachability:** inferred from the code. I did not drive it in the UI.
- **F6 magnitude:** depends on real focuser speed. The 30 s worst case needs a stuck-Busy driver
  (already seen once: incident a4ffe048).
- **F12:** depends on whether 0.5.0's `unpark()` still triggers OnStep's delayed auto-tracking.
