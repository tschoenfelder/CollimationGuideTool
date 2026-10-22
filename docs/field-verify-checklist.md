# Field-verify checklist (after the audit restructuring, #50–#55)

The restructuring (S0–S6.8d, `docs/restructuring-tasks.md`) is proven on simulators, fakes and fake time.
Real hardware is the **final confirmation**, not the proof. This list collects every `field_only_assumptions`
entry of `proofs/*.toml` into one session plan. Each item names the manifest that owns it; tick it there (move
it out of `field_only_assumptions`) when it is confirmed, or file a diagnostic bundle when it is not.

Order: do section 0 once, then the daytime Terrestrial session (1), then a supervised night session (2).

## 0. Upgrade and first start (Pi)

- [ ] Sync the code: `git fetch origin && git reset --hard origin/main` (only after the user's go-ahead).
- [ ] Upgrade the venv to OnStepAdapter 0.5.0 with the sha256 check and `--force-reinstall --no-deps`, then the
      build check — `install.md`, "Upgrading the Pi to OnStepAdapter 0.5.0". *(39-astro-guide-pulse)*
- [ ] `~/.CollimationGuideTool/config.toml` still has `[mount_alignment] pulse_ms = 500`: the log shows exactly one
      warning "pulse_ms is no longer used … ignored" and nothing else changes (checked off-rig against the real
      file on 2026-10-22). Optionally delete the line afterwards. *(55-dead-timed-path)*
- [ ] Camera connect log: one INFO line `TouptekCameraAdapter(...): opened model=... model.flag=0x... -> mono|colour,
      USB3-capable=...` per camera (G3M678M, ATR585M, GPCMOS02000KPA); note the flag words. No repeated
      "SDK call failed" WARNING; at most one INFO "… not implemented by this camera" per feature.
      *(51-touptek-mono-flag, 51-touptek-noframe-timeout, 55-capability-tables)*
- [ ] The filter wheel ("ToupTek EFW 2") connects via 127.0.0.1. *(55-single-source-defaults, 47-efw-device-name)*
- [ ] Mount and focuser connect; Terrestrial mode reports tracking OFF after connect and after unpark.
      *(44-terrestrial-tracking-off, 55-one-mount-motion-executor)*

## 1. Daytime, Terrestrial mode (tracking OFF)

Mount Align (angular moves):
- [ ] A nudge with **no camera streaming** moves the mount and only notes "no-motion check not possible" — the
      field incident 07d37da6. *(52-nudge-returns)*
- [ ] Nudges return at once; the background no-motion check warns only when nothing moved. *(52-nudge-returns)*
- [ ] Run Calibration reaches the 25 % band in one or two moves; the matrix is plausible (orientation, signs).
      *(46-calibration-sizing, 46-mount-align-angular-path)*
- [ ] Moves of 30″ … ~2166″ (nudge cap `max_nudge_arcsec`) are accepted; a GOTO plus settle leaves the mount at
      rest before the AFTER frame. *(46-mount-align-angular-path)*
- [ ] Screen moves converge with the real camera rotation; which sizes are disabled matches the cameras' plate
      scale. *(31-backlash-screen-move, 31-manual-move-texts)*
- [ ] Backlash calibration (8 steps per direction) runs. *(31-backlash-screen-move)*
- [ ] Stability thresholds behave sensibly with wind / mount ringing. *(30-motion-aware-acquisition)*

Stop and safety:
- [ ] Mount Align Stop during a GOTO stops the mount at once; the controls stay disabled until OnStepAdapter's
      call ends (≤ 30 s + ≤ 5 s); a focuser move is not stopped by it. *(49-stop-during-angular-goto)*
- [ ] Global **Stop mount** during a **park slew** stops the slew; the Mount panel shows "park stopped by the user
      — waiting for OnStepAdapter…" and stays busy up to 120 s (OnStepAdapter `park()` has no cancel).
      *(55-global-mount-stop)*
- [ ] Switching the global mode during a Mount Align GOTO stops it ("operating mode changed").
      *(48-one-mode-source)*
- [ ] Quit during a move stops the mount first; no GUI freeze beyond the documented bounds. *(49, 49-focuser-stop)*

Focuser and autofocus:
- [ ] Focuser Stop during a long move stops it at once (FOCUS_ABORT_MOTION); In/Out stay disabled ≤ 5 s
      afterwards; a jog never freezes the window. *(49-focuser-stop)*
- [ ] A Stop during autofocus ends "Cancelled" and the focuser stays where it stopped. *(49-focuser-stop)*
- [ ] A real focus sweep converges (artificial star and terrestrial target). *(33-autofocus, 35-autofocus-final-validation)*

Artificial star, registration, reacquisition:
- [ ] Choosing Artificial star switches to Terrestrial (tracking OFF) — user decision A. *(48-one-mode-source)*
- [ ] ArtificialStarRegistrar on a real pinhole seen by both cameras (rotation ≈ 0 assumption). *(37-artificial-star-registration)*
- [ ] Positive terrestrial Main+Guide match after re-pointing. *(29-cross-camera-registration)*
- [ ] Guide-assisted reacquisition of the artificial star moves the mount (angular) and converges; Cancel stops
      the mount. *(39-artificial-star-target, 39-reacquisition-angular)*

Cameras:
- [ ] ATR585M: Cooling off and Disconnect leave the cooler off (temperature drifts back to ambient).
      *(55-capability-tables)*
- [ ] A first frame > 5 s after stream start is waited for (not a timeout). *(52-exposure-snapshot, 52-fresh-frame-after-motion)*

## 2. Night, Astronomical mode (tracking ON) — supervised

- [ ] **First Astronomical calibration, supervised**: Mount Align uses guide pulses, tracking stays ON throughout,
      no emergency stop. It can take minutes of guiding on the wide Guide camera. Compare the measured guide rate
      with the 1.0× seed (`guide_rate_seed_x`). *(31-astro-guide-pulse-mount-align)*
- [ ] OnStep's 'G' flag clears within OnStepAdapter's post-chunk wait for the app's probe lengths
      (250/750/1000/3000 ms); otherwise every pulse fails and tracking goes OFF (reported). *(39-astro-guide-pulse)*
- [ ] Settle after guide pulses is sufficient with the configured `settle_ms`. *(31-astro-guide-pulse-mount-align)*
- [ ] Stop during a guide pulse: the mount stops, the text says "NOT tracking any more"; restore tracking at the
      hand controller. Note whether the running guide timer also ended. *(31-astro-guide-pulse-mount-align)*
- [ ] High declination (> ~60°): the wide camera's RA calibration may end "bounded" (no cos(dec)). Note the dec.
      *(31-astro-guide-pulse-mount-align)*
- [ ] Guide-pulse reacquisition of a real star (calibration probes, correction, tracking ON afterwards).
      *(39-astro-guide-pulse, 15-focused-star-acquisition)*
- [ ] Fine-collimation live loop on a real stacked star. *(21-fine-collimation-ui)*
- [ ] Sparse-star translation on real point-source frames. *(32-sparse-star-translation)*

## Not reproduced (operational causes, watch only)

Why the INDI CONNECTION timed out (diag-11e564ad); why the EFW stayed Busy (diag-73c7d59d); whether the live
`move_rejected` is gone with the OnStep serialization (diag-7b21bdf1); why fe91004f saw no image motion (43);
the GPCMOS02000KPA "camera busy" calibration timeout from 2026-09-24 (51-touptek-noframe-timeout).

## Pending external requests (OnStepAdapter; not filed — ask the user)

- A bare tracking-off (`TRACK_OFF` without `ABORT`), so turning tracking off is not an emergency stop (S6.8 R2).
- A cancellable `park()`; today an ABORT stops the slew but the call waits out its 120 s status loop (S6.8c-R5).
