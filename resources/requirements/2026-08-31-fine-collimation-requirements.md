# CollimationTool – Fine Collimation Implementation Requirements

## 1. Product Goal

The CollimationTool shall support a complete SCT collimation workflow:

```text
rough collimation using a defocused donut
→ focus the star
→ fine collimation using the focused diffraction pattern
→ optional verification using a Tri-Bahtinov mask
```

The normal fine-collimation workflow shall **not require a mask**.

A Tri-Bahtinov mask may later provide an additional independent measurement method, but it shall not be a prerequisite for achieving fine collimation.

The current donut-based measurement shall be treated as:

```text
rough collimation achieved
```

and not as proof of final optical collimation.

---

# 2. General Implementation Principles

The implementation shall proceed incrementally.

Each stage shall:

- provide independently useful behavior;
- have deterministic automated acceptance tests;
- avoid requiring real seeing conditions for normal CI execution;
- use public/application interfaces where possible;
- preserve the existing donut-based workflow;
- keep image-analysis algorithms independent of the UI;
- keep hardware interaction behind narrow interfaces;
- use real observations later as regression evidence;
- avoid speculative architecture or unnecessary infrastructure.

Unknown empirical parameters shall remain configurable or be represented by clearly documented initial defaults.

Do not embed temporary assumptions into core algorithms if they can instead be expressed as configuration.

---

# Stage 1 – Focused-Star Acquisition, Tracking and Reacquisition

## Goal

Provide a reliable focused-star target for fine collimation.

The selected collimation star shall be tracked even when turning the collimation screws moves it:

- within the main-camera ROI;
- outside the ROI but still inside the main-camera field;
- or outside the main-camera field while still visible in the wider-FOV guide camera.

A star leaving the current ROI shall **not** immediately be classified as lost.

The intended hierarchy is:

```text
selected collimation star
        ↓
track inside fine-collimation ROI
        ↓
star leaves ROI?
        │
        ├── no → continue fine analysis
        │
        └── yes
             ↓
      search main-camera frame
             │
      ┌──────┴──────┐
    found          not found
      │               │
      ↓               ↓
move ROI         search guide camera
                      │
               ┌──────┴──────┐
             found          not found
               │               │
               ↓               ↓
      mount recentering      target lost
               ↓
        main camera sees star
               ↓
        main tracker takes over
               ↓
          move ROI to star
               ↓
      resume fine collimation
```

## 1.1 Main-camera role

The main/collimation camera is the primary source for:

- focused diffraction measurements;
- fine-collimation ROI acquisition;
- frame registration;
- stacking;
- radial-profile measurement;
- fine-collimation asymmetry measurement.

The analysis ROI shall be treated as a movable analysis window around a tracked target, not as a fixed image location.

Software cropping of full frames is acceptable initially. Native camera ROI support is optional.

The ROI should include enough pixels for:

- the central diffraction peak;
- at least the first diffraction ring where sampling permits;
- sufficient local background for noise estimation.

## 1.2 Guide-camera role

The guide camera is the secondary, wider-field source for:

- target recovery;
- identifying the star after it has left the main-camera field;
- determining the correction needed to return it to the main-camera field;
- assisting mount recentering.

The guide camera shall **not** automatically become the source of the fine-collimation measurement.

Its role in this workflow is target reacquisition and recentering.

## 1.3 Camera FOV relationship

Guide-camera-assisted reacquisition requires a known relationship between:

```text
guide-camera image coordinates
        ↕
main-camera field of view
```

The implementation shall use the existing/calibrated relationship between both cameras where available, including:

- translation;
- scale;
- rotation.

A nominal centered FOV relationship may be used only as an explicitly uncalibrated fallback.

Automatic guide-camera-assisted mount recentering shall require sufficient calibration confidence.

## 1.4 Target identity

The tool shall preserve the identity of the selected collimation star during reacquisition.

When multiple candidate stars are present, selection should use available evidence such as:

- predicted target position;
- previous target location;
- brightness;
- local neighborhood;
- movement consistency;
- confidence.

If target identity is ambiguous, the tool shall stop automated reacquisition and request user intervention rather than silently switching to another star.

## 1.5 Reacquisition safety

Loss of the star shall never cause uncontrolled mount searching.

Reacquisition movement shall be:

- bounded;
- iterative;
- verified after each movement;
- cancellable;
- stopped on divergence;
- stopped when target identity becomes ambiguous.

The tool shall prefer:

```text
stop and ask for user intervention
```

over continuing with uncertain identification.

---

## Stage 1 Acceptance Criteria

### AC 1.1 – ROI extraction

```gherkin
Scenario: Extract an ROI around a selected star
  Given a synthetic frame containing one star at a known location
  When the star is selected for fine collimation
  Then the returned ROI is centered on the star within the configured tolerance
  And the ROI dimensions match the requested dimensions
```

### AC 1.2 – Follow star inside ROI

```gherkin
Scenario: Follow the selected collimation star
  Given a selected collimation star
  And the star moves by a small known image offset
  When the next frame is processed
  Then the same star remains selected
  And the ROI follows the new star position
```

### AC 1.3 – Star moves outside the current ROI

```gherkin
Scenario: Follow the collimation star when a screw adjustment moves it outside the current ROI
  Given a collimation star is selected
  And a fine-collimation ROI is active around that star
  When a collimation screw adjustment moves the star outside the current ROI
  But the star remains detectable in the main camera frame
  Then the tool shall reacquire the same star in the main camera frame
  And move the analysis ROI to the new star position
  And continue fine-collimation acquisition without requiring the user to select the star again
```

A star leaving only the ROI shall not be classified as `star_lost`.

### AC 1.4 – Reacquire with the wider-FOV guide camera

```gherkin
Scenario: Use the guide camera when the star leaves the main camera field of view
  Given a collimation star is selected in the main camera
  And the guide camera has a wider field of view
  And the relative main-camera field of view within the guide-camera image is known or calibrated
  When a collimation adjustment moves the star outside the main camera field of view
  But the same star remains detectable in the guide camera
  Then the tool shall identify the star in the guide-camera image
  And determine the correction required to move the star toward the main-camera field of view
  And use the mount to perform the required recentering movement
  And verify the result from newly acquired camera frames
```

Reacquisition shall continue iteratively until:

- the star enters the main-camera field;
- the configured retry/movement limit is reached;
- the mount rejects a correction;
- the star can no longer be identified reliably;
- reacquisition is diverging;
- or the user cancels.

### AC 1.5 – Transfer back to main-camera tracking

```gherkin
Scenario: Complete guide-camera-assisted reacquisition
  Given the selected star was reacquired using the guide camera
  And mount corrections have returned the star to the main-camera field of view
  When the main camera detects the selected star
  Then the main-camera tracker shall take over from the guide-camera reacquisition
  And the fine-collimation ROI shall be centered on the reacquired star
  And fine-collimation acquisition may resume
```

No new manual star selection shall be required.

### AC 1.6 – Preserve target identity

```gherkin
Scenario: Multiple stars are visible during reacquisition
  Given the selected collimation star moves outside the main-camera ROI
  And multiple candidate stars are visible in the main or guide camera
  When reacquisition is attempted
  Then the tool shall prefer the candidate consistent with the predicted target position and previous target properties
  And shall not silently switch to a different star when target identity is ambiguous
```

### AC 1.7 – Reacquisition failure

```gherkin
Scenario: Selected star cannot be reacquired
  Given the selected collimation star has left the main-camera ROI
  And it cannot be identified reliably in the main-camera frame
  And it cannot be identified reliably in the guide-camera frame
  When the configured reacquisition attempts are exhausted
  Then the tool shall report the target as lost
  And automatic mount corrections shall stop
  And no fine-collimation measurement shall be generated from another unverified star
```

Failure reasons should be diagnostic and machine-readable, for example:

- `target_not_found_main`
- `target_not_found_guide`
- `target_ambiguous`
- `mount_correction_rejected`
- `reacquisition_diverging`
- `reacquisition_timeout`
- `cancelled`

---

# Stage 2 – Short-Exposure Frame Registration

## Goal

Align successive short-exposure focused-star images so atmospheric image motion does not smear the diffraction pattern.

## Requirements

For each usable ROI frame, the software shall estimate the central star position.

Frames shall be translated to a common reference position before stacking.

The registration shall:

- not rely on mount movement;
- tolerate small image motion;
- tolerate moderate brightness variation;
- tolerate moderate noise;
- reject frames for which the star cannot be measured reliably.

## Acceptance Criteria

### AC 2.1 – Translational registration

```gherkin
Scenario: Register shifted synthetic star images
  Given several identical synthetic star images
  And each image is shifted by a known amount
  When the images are registered
  Then their measured central peaks align within the configured tolerance
```

### AC 2.2 – Brightness variation

```gherkin
Scenario: Registration survives brightness variation
  Given star frames with different intensity scaling
  When registration is performed
  Then the recovered positions remain within tolerance
```

### AC 2.3 – Invalid frame rejection

```gherkin
Scenario: Reject an unusable frame
  Given a frame without a detectable star
  When registration is attempted
  Then the frame is marked unusable
  And it is not added to the registered stack
```

---

# Stage 3 – Quality Selection and Lucky Stacking

## Goal

Generate a stable diffraction image from short exposures while reducing seeing effects.

## Requirements

The software shall maintain a bounded rolling set of recent registered star frames.

Each frame shall receive a quality measurement.

Possible indicators include:

- peak sharpness;
- FWHM;
- peak intensity relative to local background;
- compactness;
- registration confidence.

The exact quality formula is an implementation decision.

The system shall support stacking:

- all valid registered frames;
- or a configurable best fraction.

Memory usage shall remain bounded.

## Acceptance Criteria

### AC 3.1 – Stack registered frames

```gherkin
Scenario: Stack aligned star frames
  Given multiple registered synthetic diffraction images
  When the stack is produced
  Then the stacked central peak remains centered
  And signal-to-noise improves compared with an individual noisy frame
```

### AC 3.2 – Poor frame rejection

```gherkin
Scenario: Reject degraded seeing frames
  Given a sequence containing sharp and deliberately blurred star frames
  When quality selection is enabled
  Then the degraded frames receive poorer quality scores
  And the selected stack contains the better-ranked frames
```

### AC 3.3 – Bounded history

```gherkin
Scenario: Long acquisition does not grow memory indefinitely
  Given more frames than the configured rolling capacity
  When frames continue arriving
  Then retained frame history remains within the configured bound
```

---

# Stage 4 – Radial Diffraction Profile

## Goal

Measure the radial intensity distribution of the focused, stacked star image.

## Requirements

The system shall calculate a radial intensity profile around the registered star center.

The result shall expose at least:

- central peak intensity;
- radial distance in pixels;
- normalized intensity;
- detectable ring maxima where present;
- background level.

The UI shall be able to display the profile.

The analysis result shall remain usable without the UI.

## Acceptance Criteria

### AC 4.1 – Symmetric PSF profile

```gherkin
Scenario: Measure a symmetric diffraction pattern
  Given a synthetic rotationally symmetric PSF
  When its radial profile is calculated
  Then the measured profile is rotationally invariant within tolerance
```

### AC 4.2 – Ring detection

```gherkin
Scenario: Detect the first diffraction ring
  Given a synthetic Airy-like pattern with a resolvable first ring
  When the radial profile is analysed
  Then the first-ring position is detected within tolerance
```

### AC 4.3 – Insufficient sampling

```gherkin
Scenario: Diffraction structure is insufficiently sampled
  Given an image where the expected Airy structure is below useful sampling
  When fine-collimation analysis is requested
  Then the result reports insufficient sampling
  And does not generate a misleading fine-collimation recommendation
```

---

# Stage 5 – Optical Diffraction Reference Model

## Goal

Generate the expected diffraction scale for the configured optical system.

## Requirements

The model shall use available optical parameters such as:

- telescope aperture;
- focal ratio or focal length;
- camera pixel size;
- effective wavelength or filter wavelength;
- central obstruction ratio where applicable.

The expected Airy scale shall be calculated independently from the measured profile.

Missing required parameters shall produce an explicit degraded/unknown-model state rather than silently guessed values.

The model shall also be usable for synthetic test generation.

## Acceptance Criteria

### AC 5.1 – Known Airy scale

```gherkin
Scenario: Calculate expected diffraction scale
  Given aperture, focal ratio, pixel size and wavelength with known reference values
  When the diffraction scale is calculated
  Then the expected Airy radius matches the independent reference within tolerance
```

### AC 5.2 – Missing optical parameter

```gherkin
Scenario: Required optical configuration is incomplete
  Given the pixel size is unavailable
  When diffraction comparison is requested
  Then the system reports that absolute diffraction scaling is unavailable
  And no fabricated default is silently substituted
```

---

# Stage 6 – Focused-Star Symmetry / Coma Measurement

## Goal

Determine whether the focused diffraction pattern is rotationally symmetric and derive a fine-collimation error direction.

This is the core maskless fine-collimation measurement.

## Requirements

The measurement shall provide at least:

- asymmetry magnitude;
- asymmetry direction;
- measurement confidence;
- validity/reason status.

The exact algorithm is not prescribed.

Possible implementations may use:

- azimuthal intensity balance;
- ring asymmetry;
- PSF residuals;
- sector comparison;
- another validated diffraction-based method.

For a rotationally symmetric diffraction pattern:

```text
asymmetry ≈ zero
```

For a controlled asymmetric synthetic pattern:

```text
measured direction ≈ known imposed direction
```

## Acceptance Criteria

### AC 6.1 – Symmetric star

```gherkin
Scenario: Well-collimated synthetic star
  Given a rotationally symmetric focused diffraction pattern
  When fine collimation is measured
  Then the measured asymmetry is within the defined collimation tolerance
  And the result is classified as fine-collimated
```

### AC 6.2 – Known asymmetry

```gherkin
Scenario: Synthetic coma-like asymmetry
  Given a synthetic diffraction pattern with a known asymmetry direction
  When fine collimation is measured
  Then the reported correction direction matches the expected direction within tolerance
```

### AC 6.3 – Noise resistance

```gherkin
Scenario: Fine-collimation result survives moderate noise
  Given repeated noisy realizations of the same synthetic diffraction pattern
  When each sequence is stacked and analysed
  Then the recovered error direction remains statistically consistent
```

### AC 6.4 – Low confidence

```gherkin
Scenario: Diffraction structure is not sufficiently measurable
  Given a star image with insufficient signal or severe seeing degradation
  When fine collimation is measured
  Then confidence is below the actionable threshold
  And no screw recommendation is issued
```

---

# Stage 7 – Fine-Collimation UI

## Goal

Expose the maskless fine-collimation result clearly to the user.

## Requirements

The UI shall provide a dedicated fine-collimation view containing:

- enlarged focused-star stack;
- star center;
- measured asymmetry/error vector;
- confidence;
- radial profile;
- sufficient-sampling/SNR indication;
- current fine-collimation state.

The UI shall clearly distinguish:

```text
ROUGH COLLIMATION
```

from:

```text
FINE COLLIMATION
```

The existing donut result shall not be presented as proof of final fine collimation.

## Acceptance Criteria

### AC 7.1 – Valid result displayed

```gherkin
Scenario: Fine-collimation measurement is available
  Given a valid focused-star result
  When the UI updates
  Then the stacked star is displayed
  And the measured error direction is visible
  And confidence is visible
  And the radial profile is displayed
```

### AC 7.2 – Invalid result state

```gherkin
Scenario: Fine measurement cannot be trusted
  Given insufficient sampling or confidence
  When the UI updates
  Then the user is shown why the result is not actionable
  And no false green/collimated indication is shown
```

---

# Stage 8 – Integrate Screw Calibration and Recommendations

## Goal

Reuse the existing screw-response learning so fine-collimation measurements can produce actionable screw guidance.

## Requirements

The existing screw-response model shall be usable with the new fine-collimation error vector.

Recommendations shall contain:

- screw identifier;
- rotation direction;
- qualitative adjustment size;
- confidence.

Recommendations shall only be produced from valid fine-collimation measurements.

## Acceptance Criteria

### AC 8.1 – Learned screw response

```gherkin
Scenario: Known screw calibration maps fine error to recommendation
  Given stored screw-response calibration
  And a known fine-collimation error vector
  When a recommendation is calculated
  Then the expected screw is selected
  And the expected turn direction is returned
```

### AC 8.2 – No calibration available

```gherkin
Scenario: Screw mapping has not been learned
  Given a valid fine-collimation error
  But no screw-response calibration exists
  When guidance is requested
  Then the measurement is still shown
  But no invented screw recommendation is generated
```

---

# Stage 9 – Automatic Star Recentring

## Goal

Integrate the existing mount-recentering capability into the collimation workflow.

This stage complements the guide-camera-assisted reacquisition from Stage 1.

## Requirements

After a manual collimation-screw adjustment shifts the star, the tool shall be able to recenter it automatically.

The recentering logic shall:

- use empirical mount calibration;
- remain independent from optical collimation measurement;
- never confuse telescope pointing correction with optical collimation correction;
- support main-camera measurements;
- support guide-camera-assisted recovery when the star has left the main-camera field;
- remain bounded and cancellable.

## Acceptance Criteria

### AC 9.1 – Recenter shifted star

```gherkin
Scenario: Star moves after screw adjustment
  Given the collimation star was previously centred
  And a screw adjustment moves the star by a known amount
  When recentering is requested
  Then mount corrections reduce the image displacement
  Until the configured centring tolerance is reached
```

### AC 9.2 – Diverging movement

```gherkin
Scenario: Mount calibration is wrong
  Given mount corrections repeatedly increase the star displacement
  When automatic recentering runs
  Then the divergence guard stops further corrections
  And the user is shown the failure reason
```

### AC 9.3 – Star lost during recentering

```gherkin
Scenario: Star is lost during recentering
  When no valid target measurement can be obtained
  And guide-camera-assisted reacquisition also fails
  Then mount correction stops
  And the result reports star_lost
```

---

# Stage 10 – Seeing / Measurement-Quality Separation

## Goal

Help distinguish poor optical collimation from poor atmospheric seeing.

## Requirements

The application shall calculate separate indicators for:

- instantaneous/raw star quality;
- registered/stacked star quality.

Possible metrics include:

- raw FWHM statistics;
- stacked FWHM;
- frame quality distribution;
- usable-frame percentage.

No absolute astronomical seeing model is required initially.

The goal is to determine whether current conditions permit a trustworthy fine-collimation measurement.

## Acceptance Criteria

### AC 10.1 – Good seeing sequence

```gherkin
Scenario: Stable sharp input frames
  Given a sequence with low simulated image motion and blur
  When seeing quality is evaluated
  Then a high fraction of frames is classified as usable
```

### AC 10.2 – Poor seeing sequence

```gherkin
Scenario: Strongly varying blurred input frames
  Given a sequence with large random motion and blur
  When seeing quality is evaluated
  Then measurement quality is degraded
  And confidence in fine collimation is reduced
```

---

# Stage 11 – Save Collimation Result

## Goal

Produce a reproducible result artifact from a successful or failed collimation session.

## Requirements

A saved collimation result should include:

- timestamp;
- camera settings;
- telescope/optical parameters;
- rough donut measurement;
- focused stacked star;
- radial profile;
- fine-collimation measurement;
- screw recommendation if available;
- measurement-quality indicators;
- relevant configuration.

This is separate from UUID-based diagnostics.

Diagnostics answer:

```text
What went wrong?
```

A saved collimation result answers:

```text
What optical result was measured?
```

## Acceptance Criteria

### AC 11.1 – Result can be reconstructed

```gherkin
Scenario: Save a fine-collimation result
  Given a valid completed measurement
  When the user saves the result
  Then the saved artifact contains the measured image data
  And the numerical measurement values
  And the optical parameters required to interpret them
```

---

# Stage 12 – Optional Tri-Bahtinov Fine Collimation

## Goal

Support a Tri-Bahtinov mask as an optional independent fine-collimation method.

The maskless diffraction method remains the normal workflow.

## Requirements

When a Tri-Bahtinov mask is used, the application may analyse the diffraction spikes and derive a fine-collimation error.

Both fine-collimation methods should use a common application-facing result where practical:

```text
FineCollimationMeasurement
    error magnitude
    direction / sector
    confidence
    validity
    method
```

Conceptually:

```text
FineCollimationAnalyzer
        │
        ├── FocusedDiffractionAnalyzer
        │
        └── TriBahtinovAnalyzer
```

The UI shall clearly identify which measurement method produced the result.

## Acceptance Criteria

### AC 12.1 – Maskless mode works independently

```gherkin
Scenario: Fine collimation without a mask
  Given no Tri-Bahtinov mask is installed
  When a suitable focused star sequence is captured
  Then fine collimation can be measured using the diffraction-based method
```

### AC 12.2 – Tri-Bahtinov measurement

```gherkin
Scenario: Tri-Bahtinov pattern is present
  Given a synthetic or recorded Tri-Bahtinov diffraction pattern
  When Tri-Bahtinov analysis is selected
  Then the spike displacement is measured
  And the corresponding collimation result is produced
```

### AC 12.3 – Cross-check

```gherkin
Scenario: Both fine-collimation methods are available
  Given a focused diffraction measurement
  And a Tri-Bahtinov measurement from the same optical state
  When both are valid
  Then both results are shown independently
  And disagreement is visible rather than silently averaged away
```

---

# Recommended Implementation Order

```text
1. Focused-star acquisition, tracking and reacquisition
        ↓
2. Short-exposure frame registration
        ↓
3. Lucky stacking
        ↓
4. Radial profile
        ↓
5. Diffraction reference model
        ↓
6. Maskless symmetry/coma measurement
        ↓
7. Fine-collimation UI
        ↓
8. Screw recommendation integration
        ↓
9. Automatic recentering
        ↓
10. Seeing / quality indication
        ↓
11. Save collimation result
        ↓
12. Optional Tri-Bahtinov analysis
```

Stages 1–6 form the technical core.

Stages 1–7 provide the first useful **maskless fine-collimation MVP**.

The Tri-Bahtinov implementation should be added only after the maskless measurement pipeline is working and validated.

---

# First Maskless Fine-Collimation MVP

The MVP is complete when:

- the user can select a focused star;
- the software tracks a movable ROI around it;
- the star can be reacquired in the full main-camera frame after leaving the ROI;
- the guide camera can assist reacquisition when the star leaves the main-camera field;
- mount corrections can return the star into the main-camera field when calibration is sufficient;
- target identity is preserved or ambiguity is reported;
- short-exposure frames are registered;
- poor frames can be rejected;
- good frames are stacked;
- the radial diffraction profile is measurable;
- a synthetic known asymmetric PSF produces the correct error direction;
- a symmetric PSF is classified as fine-collimated;
- insufficient sampling/SNR produces an explicit non-actionable result;
- the UI displays the stacked star, radial profile, error vector and confidence;
- all functionality is deterministic-testable without telescope hardware;
- the existing donut rough-collimation workflow remains unchanged.

---

# Long-Term User Workflow

```text
select star
    ↓
defocus
    ↓
donut rough collimation
    ↓
rough state acceptable
    ↓
focus star
    ↓
start short-exposure ROI acquisition
    ↓
registration + quality selection + stacking
    ↓
fine diffraction analysis
    ↓
adjust recommended screw
    ↓
star moved?
    │
    ├── still in ROI → continue
    │
    ├── in main frame → move ROI
    │
    └── outside main frame
            ↓
       find in guide camera
            ↓
       mount recentering
            ↓
       main camera reacquires
            ↓
       resume fine analysis
    ↓
repeat
    ↓
fine collimation achieved
```

Optional verification:

```text
fine collimation achieved
    ↓
insert Tri-Bahtinov mask
    ↓
independent verification
```

---

# Agent Completion Rules

For each implementation stage:

1. implement only the active stage/issue scope;
2. add or update deterministic acceptance tests first or alongside implementation;
3. preserve existing rough-collimation behavior;
4. do not bypass quality gates;
5. do not silently invent product thresholds;
6. keep empirical thresholds configurable;
7. preserve diagnostics for failure/reacquisition paths;
8. keep main-camera fine analysis separate from guide-camera recovery;
9. keep mount movement separate from optical collimation measurement;
10. prefer explicit failure states over uncertain automated behavior.

Work is complete only when:

- the stage acceptance criteria are satisfied;
- relevant safety paths are tested;
- all repository quality gates pass;
- diagnostics remain usable;
- no unrelated feature scope was added.
