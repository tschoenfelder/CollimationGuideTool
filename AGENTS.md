# AGENTS.md

## Purpose

This file contains repository-level architectural rules that coding agents must preserve.

These rules are not suggestions. If a requested implementation appears to conflict with them, stop and surface the conflict instead of silently bypassing the architecture.

---



## OnStep connection ownership — mandatory

CollimationGuideTool must **never open any connection to the OnStep controller that bypasses OnStepAdapter**.

This applies to the full OnStep device surface, not only RA/DEC motion. CollimationGuideTool must not directly create:

- a serial connection to the OnStep controller;
- a raw LX200 TCP/socket connection;
- an INDI client connection to `LX200 OnStep` for mount movement;
- a separate direct INDI connection to the same OnStep device for park/unpark, tracking, state or focuser operations.

All OnStep-backed functionality used by CollimationGuideTool must be provided through OnStepAdapter. The adapter may internally use serial, TCP, INDI, or another supported transport, but that choice must remain hidden behind the adapter boundary.

If the existing deployment has `indi_lx200_OnStep` owning the serial port, resolve that ownership inside the OnStepAdapter/deployment architecture. Do not preserve direct CollimationGuideTool-to-OnStep INDI adapters as a workaround.

This rule does not prohibit INDI for non-OnStep devices such as cameras or external filter wheels.



## Consumer enforcement responsibility — mandatory

OnStepAdapter cannot technically prevent CollimationGuideTool/GuideTool from opening a second direct connection to the OnStep controller. Therefore enforcement of the adapter boundary is a **consumer responsibility**.

CollimationGuideTool/GuideTool must actively ensure that no direct OnStep connection path exists outside OnStepAdapter. This includes serial, raw LX200 TCP/socket, and direct INDI access to the OnStep device.

Any existing direct OnStep access is technical debt to be removed. Any new direct OnStep access is an architectural violation and must not be merged.

Tests/review should protect this boundary where practical, for example by asserting that mount/focuser/park/tracking construction is sourced through the OnStepAdapter integration rather than a direct OnStep-specific INDI adapter.


## Mount movement boundary — mandatory

For **RA/DEC mount movement**, `OnStepAdapter` is the authoritative hardware-control boundary.

CollimationGuideTool is responsible for:

1. deciding the desired image-space movement;
2. converting optical configuration + target distance + frame geometry into a desired angular RA/DEC correction;
3. calling the OnStepAdapter movement API;
4. acquiring/validating camera evidence when a measurement is required;
5. measuring the achieved pixel displacement;
6. closing the loop with a bounded correction when necessary.

CollimationGuideTool must **not** implement a second normal RA/DEC movement stack by directly:

- selecting INDI slew-rate presets;
- toggling `TELESCOPE_MOTION_NS` / `TELESCOPE_MOTION_WE`;
- sleeping to time the movement;
- issuing raw directional stop commands;
- maintaining its own duplicate mount-rate calibration model.

If OnStepAdapter is missing a capability needed by CollimationGuideTool, the correct solution is to extend/fix OnStepAdapter and then consume that capability here.

Do not work around a missing adapter capability with a parallel raw INDI implementation in this repository.

---

## Relevant OnStepAdapter API

Current OnStepAdapter exposes bounded movement APIs including:

```python
client.mount.move_ra(offset_arcsec, mode="center")
client.mount.move_dec(offset_arcsec, mode="center")
```

These angular correction APIs use direction-specific motion calibration and require image/plate-solve verification afterward.

It also exposes bounded adapter-owned timed/manual motion APIs for cases where angular motion is not yet applicable.

Application code may use those APIs where appropriate, but raw protocol sequencing remains inside OnStepAdapter.

---

## Mount Align movement policy

Mount Align should calculate useful movement up front rather than discover it through tiny pulses.

Calibration target:

- approximately 25% of the relevant frame dimension;
- practical acceptance band approximately 20–30%;
- predominantly horizontal response -> use 25% of frame width;
- predominantly vertical response -> use 25% of frame height.

Current reference seeds:

- G3M678M / C8: ~195"/110" at infinity or 10 km; ~182"/102" at 30 m;
- ATR585M / C8: ~283"/159" at infinity or 10 km; ~264"/148" at 30 m;
- GPCMOS02000 / 180 mm: ~1595"/897" at infinity or 10 km; ~1585"/892" at 30 m.

These are initial angular requests. The measured pixel displacement is authoritative.

Use the smallest participating FOV for the first shared calibration movement. A wider camera may receive a second larger movement if its measured displacement is <~10% of its relevant frame dimension or confidence is insufficient.

---

## Manual movement versus measurement

Manual/bootstrap RA+/RA-/Dec+/Dec- movement is a positioning action.

It must:

- work before a CalibrationMatrix exists;
- use the OnStepAdapter boundary;
- remain bounded and cancellable;
- keep the Qt event loop and live camera stream responsive;
- not wait for stable BEFORE/AFTER measurement frames;
- not perform image correlation merely to finish the button action.

Calibration/verification is different:

```text
stable BEFORE -> adapter movement -> stable AFTER -> measure dx/dy
```

Frames captured during movement may be displayed but must never be used as measurement evidence.

---

## Operating mode

The global Operating Mode is the single source of truth for terrestrial versus astronomical behavior.

- Terrestrial -> tracking OFF.
- Astronomical -> workflow-appropriate tracking policy.

Do not add another local Terrestrial/Star environment toggle inside Mount Align.

Natural Star vs Artificial Star is an orthogonal target-type distinction and may remain where the image algorithm genuinely requires it.

---

## Proof of solution

The team/agent owns the proof of a fix.

For a defect:

```text
reproduce or characterize
-> add failing deterministic regression
-> implement fix
-> prove regression passes
-> run broader quality gates
-> only then request field confirmation
```

Do not use the user as the primary test harness for timing-sensitive or hard-to-reproduce hardware issues.

For hardware-facing behavior, prefer:

- fake/stub OnStepAdapter tests;
- fake INDI only where INDI itself is the subject under test;
- deterministic state/timing simulations;
- captured UUID diagnostics;
- permanent regression datasets.

Field confirmation is the final validation layer, not the main proof.

---

## Architectural enforcement checklist

A mount-related change is not complete unless review can answer **yes** to all applicable questions:

- Is normal RA/DEC movement routed through OnStepAdapter?
- Is requested movement expressed in angular terms at the application/adapter boundary where possible?
- Is there no duplicate raw INDI movement state machine in Mount Align?
- Is manual movement decoupled from image-measurement waits?
- Are calibration movements verified from stable camera data?
- Are all relevant failure paths automatically tested?
- Are the issue-specific acceptance criteria proven before requesting field testing?

If not, do not merge the change.
