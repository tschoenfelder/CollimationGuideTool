# CollimationTool und GuideTool: Gemeinsame Architektur und Regressionsschutz

## Zielbild

Es sind **zwei Anwendungen**, `CollimationTool` und `GuideTool`, mit einem gemeinsamen technischen Kern. Die zweite zentrale Anforderung ist, Änderungen so einzugrenzen und abzusichern, dass nicht betroffene Funktionalität nachweisbar unverändert bleibt.

Kollimation und Guiding können denselben Video-, Zielerkennungs- und Geräteunterbau nutzen, bleiben aber fachlich unterschiedliche Funktionen.

## Zwei Apps, ein gemeinsamer Kern

```text
                    ┌────────────────────────┐
                    │   CollimationTool App  │
                    │   PySide6 UI + Workflow│
                    └───────────┬────────────┘
                                │
                    ┌───────────▼────────────┐
                    │                        │
                    │     astrotool-core     │
                    │                        │
                    │ Kamera / Frames / ROI  │
                    │ Sternsuche / Tracking  │
                    │ INDI / Logging / Replay│
                    │                        │
                    └───────────▲────────────┘
                                │
                    ┌───────────┴────────────┐
                    │      GuideTool App     │
                    │   PySide6 UI + Workflow│
                    └────────────────────────┘
```

„Gemeinsamer Kern“ bedeutet nicht, alles in eine große wiederverwendbare Klasse zu packen. Gemeinsam wird nur, was fachlich und technisch tatsächlich identisch ist.

## Gemeinsamer Basiscode

```text
astrotool_core/
├── camera/
│   ├── port.py
│   ├── capabilities.py
│   ├── touptek_adapter.py
│   ├── replay_camera.py
│   └── fake_camera.py
│
├── frames/
│   ├── frame.py
│   ├── pixel_format.py
│   ├── analysis_plane.py
│   └── frame_buffer.py
│
├── target/
│   ├── point_source.py
│   ├── detector.py
│   ├── roi_selector.py
│   └── roi_tracker.py
│
├── mount/
│   ├── port.py
│   ├── no_mount.py
│   ├── indi_adapter.py
│   └── axis_calibration.py
│
├── acquisition/
│   ├── single_capture.py
│   ├── stream_controller.py
│   └── acquisition_state.py
│
├── session/
│   ├── session_context.py
│   ├── event_log.py
│   └── frame_recorder.py
│
└── testing/
    ├── frame_factory.py
    ├── replay_dataset.py
    ├── fake_touptek.py
    └── fake_mount.py
```

Dieser Kern darf **keine Kenntnis von „Kollimation“ oder „Guiding“ als UI-Anwendung haben**.

## App-spezifischer Code

### CollimationTool

```text
collimation_tool/
├── domain/
│   ├── collimation_measurement.py
│   ├── symmetry_analysis.py
│   ├── diffraction_analysis.py
│   ├── focus_metric.py
│   └── collimation_state.py
│
├── application/
│   ├── collimation_controller.py
│   ├── focus_controller.py
│   └── recenter_policy.py
│
└── ui/
    ├── main_window.py
    ├── collimation_view.py
    ├── focus_view.py
    └── collimation_overlays.py
```

### GuideTool

```text
guide_tool/
├── domain/
│   ├── guide_error.py
│   ├── drift_estimator.py
│   ├── correction_model.py
│   └── guiding_state.py
│
├── application/
│   ├── guide_controller.py
│   ├── calibration_controller.py
│   └── correction_policy.py
│
└── ui/
    ├── main_window.py
    ├── guide_view.py
    ├── calibration_view.py
    └── guide_overlays.py
```

## Was gemeinsam sein sollte

| Funktion | Gemeinsamer Kern? | Begründung |
|---|---:|---|
| ToupTek-Kameraerkennung | Ja | Identischer SDK-Zugriff |
| Kamerafähigkeiten | Ja | Identisches Capability-Modell |
| Einzelbild und Stream | Ja | Identische Aufnahmeinfrastruktur |
| Mono-/Farbbehandlung | Ja | Identisches Frame- und Pixelformat |
| Transformation zum Analysebild | Ja | Beide benötigen ein 2D-Intensitätsbild |
| Punktquellenerkennung | Ja | Beide suchen zunächst einen Stern |
| Automatische ROI-Auswahl | Ja | Identischer Ausgangsvorgang |
| ROI-Nachführung | Ja | Beide müssen das Ziel im Bild verfolgen |
| Replay gespeicherter Frames | Ja | Gemeinsame Testinfrastruktur |
| Sessionlogging | Ja | Identischer technischer Mechanismus |
| INDI-Verbindung | Ja | Nur ein Adapter |
| Ermittlung der Achsenwirkung | Ja | Technische Kalibration |
| Fokusmetrik | Eher CollimationTool | Zunächst dort fachlich benötigt |
| Kollimationsbewertung | Nein | Ausschließlich CollimationTool |
| Guidefehler und Korrekturregler | Nein | Ausschließlich GuideTool |
| Mountbewegung | Gemeinsamer Adapter, getrennte Policy | Zugriff gemeinsam, Entscheidung app-spezifisch |
| Overlays | Basistypen gemeinsam | Konkrete Darstellung app-spezifisch |
| UI | Nein | Zwei eigenständige Anwendungen |

## Gemeinsamer INDI-Zugriff, getrennte Steuerungslogik

Es darf genau **einen** technischen INDI-Zugriff geben:

```python
class MountPort(Protocol):
    def connect(self) -> None: ...
    def disconnect(self) -> None: ...
    def capabilities(self) -> MountCapabilities: ...
    def status(self) -> MountStatus: ...
    def pulse_axis(
        self,
        axis: MountAxis,
        direction: AxisDirection,
        duration_ms: int,
    ) -> CommandResult: ...
```

Der Adapter weiß:

- wie die Verbindung hergestellt wird,
- welche Properties vorhanden sind,
- wie Status gelesen wird,
- wie ein begrenzter Achsenimpuls gesendet wird,
- wie Fehler und Verbindungsverlust gemeldet werden.

Der Adapter weiß ausdrücklich nicht:

- ob gerade kollimiert wird,
- ob der Stern rezentriert werden soll,
- ob ein Guidefehler korrigiert werden soll,
- wie groß eine fachlich sinnvolle Korrektur ist.

Das entscheiden zwei unterschiedliche Policies:

```python
class CollimationRecenterPolicy:
    ...
```

```python
class GuideCorrectionPolicy:
    ...
```

Damit kann eine Änderung des Guiding-Reglers nicht unbeabsichtigt die Kollimations-Rezentrierung verändern.

## Schutz vor Kollateralschäden

Nicht fehlende Features sind das größte Risiko, sondern unkontrollierte Änderungen über Modulgrenzen hinweg. Dagegen braucht das Projekt technische Leitplanken.

### 1. Kleine öffentliche Schnittstellen

Jedes gemeinsame Subsystem erhält eine explizite öffentliche API.

```python
from astrotool_core.camera import (
    CameraDescriptor,
    CameraCapabilities,
    CameraPort,
    Frame,
)
```

Andere Module importieren keine privaten Implementierungsdetails:

```python
# Nicht erlaubt
from astrotool_core.camera.touptek_adapter import _SdkCallbackHandler
```

### 2. Dependency Rule

Die Abhängigkeiten dürfen nur in eine Richtung zeigen:

```text
CollimationTool UI ─┐
                    ├─→ App-spezifische Domain
GuideTool UI ───────┘              │
                                   ▼
                           astrotool_core ports
                                   ▲
                                   │
                      ToupTek- und INDI-Adapter
```

Verboten wären beispielsweise:

```text
astrotool_core → collimation_tool
astrotool_core → guide_tool
touptek_adapter → PySide6 widget
roi_tracker → IndiMountAdapter
```

Insbesondere darf `RoiTracker` niemals selbst eine Montierung bewegen. Er liefert lediglich eine gemessene Bildabweichung.

### 3. Contract-Tests für jeden Adapter

Jeder Kameraadapter muss dieselben Tests bestehen:

```python
@pytest.mark.parametrize(
    "camera_factory",
    [
        fake_camera_factory,
        replay_camera_factory,
        touptek_camera_factory,
    ],
)
def test_camera_contract(camera_factory):
    camera = camera_factory()
    ...
```

Entsprechendes gilt für:

- `NoMountAdapter`
- `FakeMountAdapter`
- `IndiMountAdapter`

Wenn intern der ToupTek- oder INDI-Code geändert wird, weist der Contract-Test nach, dass die öffentlich erwartete Funktionalität gleich geblieben ist.

### 4. Characterization Tests vor Änderungen

Für bereits funktionierenden Code gilt:

> Vor dem Refactoring wird das beobachtete Verhalten als Test festgeschrieben.

```python
def test_existing_roi_reacquisition_behavior():
    frames = load_replay("collimation_star_moves_after_adjustment")

    results = run_roi_tracker(frames)

    assert results.lock_states == [
        LOCKED,
        LOCKED,
        LOST,
        SEARCHING,
        REACQUIRED,
    ]
    assert results.final_target == pytest.approx(
        Point(812.4, 463.1),
        abs=0.5,
    )
```

Das macht eine beabsichtigte Verhaltensänderung sichtbar, statt sie als unbemerkten Seiteneffekt einzuschleusen.

### 5. Golden-Master-Tests für Replaysequenzen

```text
datasets/
├── collimation/
│   ├── mono_centered/
│   ├── mono_adjustment_shift/
│   ├── color_bayer/
│   └── artificial_star/
│
└── guiding/
    ├── steady_drift/
    ├── axis1_response/
    ├── axis2_response/
    └── lost_star/
```

Verglichen werden numerische Ergebnisse, nicht komplette UI-Screenshots:

- Zielposition
- ROI-Zustand
- Fokusmetrik
- Kollimationsmetrik
- gemessener Driftvektor
- vorgeschlagene Guidekorrektur

Toleranzen müssen fachlich definiert werden. Ein bitgenauer Vergleich von Fließkommazahlen wäre wahrscheinlich zu fragil.

### 6. Testsuiten nach Auswirkungsbereich

```text
pytest tests/core
pytest tests/collimation
pytest tests/guide
pytest tests/integration
```

Vor jeder Übernahme laufen mindestens:

```text
core
collimation
guide
```

Auch dann, wenn angeblich nur das CollimationTool geändert wurde.

### 7. Änderungsklassifikation

Jede Änderung wird vor der Implementierung einem Bereich zugeordnet:

```text
CORE-CAMERA
CORE-FRAME
CORE-TARGET
CORE-MOUNT
CORE-SESSION
APP-COLLIMATION
APP-GUIDE
UI-COLLIMATION
UI-GUIDE
```

Daraus ergibt sich die erwartete Testwirkung:

```text
APP-COLLIMATION geändert
→ Core-Tests
→ Collimation-Tests
→ Guide-Smoke-Tests

CORE-TARGET geändert
→ vollständige Core-Tests
→ vollständige Collimation-Tests
→ vollständige Guide-Tests
```

### 8. Kein beiläufiges Refactoring

Eine Featureänderung sollte nicht gleichzeitig:

- Dateien umbenennen,
- öffentliche Typen ändern,
- Imports umorganisieren,
- Datenmodelle erweitern,
- Algorithmen „vereinfachen“,
- Fehlerbehandlung neu schreiben.

Wenn Refactoring notwendig ist, erfolgt es als separater, verhaltensneutraler Schritt mit grünen Tests davor und danach.

### 9. Änderungsbudget für KI-gestützte Patches

Für jeden Patch sollte gelten:

```text
Erlaubte Dateien:
- explizit benannte Implementierungsdateien
- zugehörige Tests

Nicht erlaubt:
- Änderungen außerhalb der Liste
- neue Fallbacklogik ohne Anforderung
- Entfernung bestehender Prüfungen
- Ersetzen funktionierender Adapter
- Änderung öffentlicher Signaturen ohne Migration
```

Ein Patch, der vorsorglich viele weitere Module anfasst, wird nicht akzeptiert, auch wenn die Tests zufällig grün sind.

## Repository-Entscheidung

Empfohlen wird **ein Repository und ein Python-Paketworkspace**, nicht zwei Repositories:

```text
astro-tools/
├── pyproject.toml
├── packages/
│   └── astrotool_core/
├── apps/
│   ├── collimation_tool/
│   └── guide_tool/
├── tests/
│   ├── core/
│   ├── collimation/
│   ├── guide/
│   ├── contracts/
│   └── integration/
└── datasets/
    ├── collimation/
    └── guiding/
```

Daraus entstehen zwei Desktop-Einstiegspunkte:

```toml
[project.scripts]
collimation-tool = "collimation_tool.main:main"
guide-tool = "guide_tool.main:main"
```

Auf dem Raspberry Pi erscheinen dann zwei getrennte Menüeinträge:

- CollimationTool
- GuideTool

Sie verwenden aber dieselbe installierte Version von `astrotool_core`.

## Architekturentscheidung

**Zwei getrennte Anwendungen mit getrennten Workflows und UIs, aber genau ein gemeinsamer, UI-unabhängiger Kern für ToupTek, Frames, Ziel-/ROI-Verfolgung, Replay, INDI und Logging.**

Für die Änderungsstabilität gilt:

**Kein Patch gilt als fertig, solange nicht sowohl die direkt betroffene Testsuite als auch die Regressionstests der jeweils anderen Anwendung erfolgreich gelaufen sind.**

Damit werden beide Extreme vermieden:

- keine duplizierten Kamera-/INDI-Implementierungen,
- kein monolithischer „SuperController“, bei dem jede Änderung Guiding und Kollimation gleichzeitig gefährdet.
