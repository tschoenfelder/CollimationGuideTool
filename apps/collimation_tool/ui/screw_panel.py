"""Teaching the three collimation screws: the operator turns one, then says which and how.

Each click compares the picture now (median of the next few Main measurements) with the
picture at the previous click, and learns what that turn did.
"""

from __future__ import annotations

import json
import logging
import statistics
from functools import partial
from pathlib import Path

from PySide6.QtWidgets import QComboBox, QGridLayout, QLabel, QPushButton, QWidget

from collimation_tool.domain.collimation_measurement import DonutMeasurement
from collimation_tool.domain.screw_guidance import TURN_SIZES, Offset, Screw, ScrewModel
from collimation_tool.ui.collimation_overlay import SCREW_COLORS, offset_share

_log = logging.getLogger(__name__)

#: Main measurements whose median is one reading (single frames scatter by about 1 %).
READINGS_PER_PICTURE = 5


class ScrewPanel(QWidget):
    def __init__(self, model: ScrewModel, *, store_path: Path) -> None:
        super().__init__()
        self._model = model
        self._store_path = store_path
        #: The picture the next turn is compared with.
        self._reference: Offset | None = None
        #: The turn being read (None = only remembering the picture), and its readings.
        self._turn: tuple[Screw, bool, float] | None = None
        self._readings: list[Offset] | None = None

        layout = QGridLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        intro = QLabel(
            "<b>Screws</b>: press Remember picture once. Then turn ONE screw, take your hand "
            "off, and press what you turned."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro, 0, 0, 1, 4)
        self._remember_button = QPushButton("Remember picture")
        self._remember_button.clicked.connect(self._on_remember)
        self._size_combo = QComboBox()
        for label, size in TURN_SIZES.items():
            self._size_combo.addItem(label, size)
        layout.addWidget(self._remember_button, 1, 0, 1, 2)
        layout.addWidget(QLabel("Turned by:"), 1, 2)
        layout.addWidget(self._size_combo, 1, 3)
        self._turn_buttons: dict[tuple[Screw, bool], QPushButton] = {}
        self._learned_labels: dict[Screw, QLabel] = {}
        for row, screw in enumerate(Screw, start=2):
            name = QLabel(f"<b>{screw.value.capitalize()}</b>")
            name.setStyleSheet(f"color: {SCREW_COLORS[screw].name()};")
            layout.addWidget(name, row, 0)
            for column, (clockwise, text) in enumerate(((True, "↻ cw"), (False, "↺ ccw")), 1):
                button = QPushButton(text)
                button.clicked.connect(partial(self._on_turned, screw, clockwise))
                self._turn_buttons[(screw, clockwise)] = button
                layout.addWidget(button, row, column)
            learned = QLabel("")
            self._learned_labels[screw] = learned
            layout.addWidget(learned, row, 3)
        self._forget_button = QPushButton("Forget screws")
        self._forget_button.clicked.connect(self._on_forget)
        layout.addWidget(self._forget_button, 5, 0, 1, 2)
        self._status = QLabel("")
        self._status.setWordWrap(True)
        layout.addWidget(self._status, 6, 0, 1, 4)

        self._load()
        self._refresh()

    @property
    def status_text(self) -> str:
        return self._status.text()

    def add_measurement(self, measurement: DonutMeasurement | None) -> None:
        """Every new Main measurement (None: no donut measured in that frame)."""
        if self._readings is None:
            return
        if measurement is None:
            self._status.setText("Waiting for a measurable donut…")
            return
        self._readings.append(offset_share(measurement))
        if len(self._readings) < READINGS_PER_PICTURE:
            self._status.setText(
                f"Reading the picture… {len(self._readings)}/{READINGS_PER_PICTURE}"
            )
            return
        picture = (
            statistics.median(x for x, _ in self._readings),
            statistics.median(y for _, y in self._readings),
        )
        turn, self._turn, self._readings = self._turn, None, None
        if turn is None or self._reference is None:
            self._status.setText("Picture remembered. Turn one screw.")
        else:
            screw, clockwise, size = turn
            learned, text = self._model.observe(
                screw, clockwise=clockwise, size=size, before=self._reference, after=picture
            )
            if learned:
                self._save()
            self._status.setText(text)
        self._reference = picture
        self._refresh()

    def _on_remember(self) -> None:
        self._turn, self._readings = None, []
        self._status.setText("Reading the picture…")
        self._refresh()

    def _on_turned(self, screw: Screw, clockwise: bool) -> None:
        self._turn = (screw, clockwise, float(self._size_combo.currentData()))
        self._readings = []
        self._status.setText("Reading the picture…")
        self._refresh()

    def _on_forget(self) -> None:
        self._model.forget()
        self._reference, self._turn, self._readings = None, None, None
        self._save()
        self._status.setText("Screws forgotten.")
        self._refresh()

    def _refresh(self) -> None:
        reading = self._readings is not None
        self._remember_button.setEnabled(not reading)
        for button in self._turn_buttons.values():
            button.setEnabled(not reading and self._reference is not None)
        for screw, label in self._learned_labels.items():
            turns = self._model.turns_learned(screw)
            label.setText(f"learned ({turns})" if turns else "not learned")

    def _load(self) -> None:
        if not self._store_path.is_file():
            return
        try:
            data = json.loads(self._store_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            _log.warning("could not read %s: %s", self._store_path, exc)
            return
        if isinstance(data, dict):
            self._model.load_data(data)

    def _save(self) -> None:
        try:
            self._store_path.parent.mkdir(parents=True, exist_ok=True)
            self._store_path.write_text(json.dumps(self._model.to_data()), encoding="utf-8")
        except OSError as exc:
            _log.warning("could not save %s: %s", self._store_path, exc)
