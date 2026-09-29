"""FlowLayout — a QLayout that wraps child widgets to a new line instead
of clipping or requiring a scrollbar.

Field report (2026-09-29): rows packing several fixed-size buttons/labels
into one `QHBoxLayout` (e.g. Mount Align's mode buttons + Calibrate FOV +
Keep calibrating) were fixed once by hand-splitting into narrower rows
tuned against one platform's measured pixel widths (commit `59a89df`) --
and still clipped on the real Pi screen, whose font metrics differ from
both the Windows-native testing and the offscreen Qt platform CI runs
under. A `FlowLayout` wraps by construction at whatever width it's
actually given, so it never needs re-tuning against a specific platform's
measurements again.

Ported from Qt's own well-known "Flow Layout" example (the standard
pattern for this in Qt/PySide -- no third-party dependency needed).
"""

from __future__ import annotations

from PySide6.QtCore import QMargins, QPoint, QRect, QSize, Qt
from PySide6.QtWidgets import QLayout, QLayoutItem, QSizePolicy, QWidget


class FlowLayout(QLayout):
    def __init__(
        self, parent: QWidget | None = None, *, margin: int = 0, spacing: int = -1
    ) -> None:
        super().__init__(parent)
        if margin >= 0:
            self.setContentsMargins(QMargins(margin, margin, margin, margin))
        self.setSpacing(spacing)
        self._items: list[QLayoutItem] = []

    def __del__(self) -> None:
        while self.count():
            self.takeAt(0)

    def addItem(self, item: QLayoutItem) -> None:  # noqa: N802 -- Qt override
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int) -> QLayoutItem | None:  # noqa: N802 -- Qt override
        if 0 <= index < len(self._items):
            return self._items[index]
        return None

    def takeAt(self, index: int) -> QLayoutItem | None:  # noqa: N802 -- Qt override
        if 0 <= index < len(self._items):
            return self._items.pop(index)
        return None

    def expandingDirections(self) -> Qt.Orientation:  # noqa: N802 -- Qt override
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:  # noqa: N802 -- Qt override
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802 -- Qt override
        return self._do_layout(QRect(0, 0, width, 0), test_only=True)

    def setGeometry(self, rect: QRect) -> None:  # noqa: N802 -- Qt override
        super().setGeometry(rect)
        self._do_layout(rect, test_only=False)

    def sizeHint(self) -> QSize:  # noqa: N802 -- Qt override
        return self.minimumSize()

    def minimumSize(self) -> QSize:  # noqa: N802 -- Qt override
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        margins = self.contentsMargins()
        size += QSize(
            margins.left() + margins.right(), margins.top() + margins.bottom()
        )
        return size

    def _do_layout(self, rect: QRect, *, test_only: bool) -> int:
        margins = self.contentsMargins()
        left, top, right, bottom = (
            margins.left(), margins.top(), margins.right(), margins.bottom(),
        )
        effective = rect.adjusted(left, top, -right, -bottom)
        x, y = effective.x(), effective.y()
        line_height = 0
        spacing = self.spacing()

        for item in self._items:
            widget = item.widget()
            space_x = spacing
            space_y = spacing
            if spacing == -1 and widget is not None:
                space_x = widget.style().layoutSpacing(
                    QSizePolicy.ControlType.PushButton,
                    QSizePolicy.ControlType.PushButton,
                    Qt.Orientation.Horizontal,
                )
                space_y = widget.style().layoutSpacing(
                    QSizePolicy.ControlType.PushButton,
                    QSizePolicy.ControlType.PushButton,
                    Qt.Orientation.Vertical,
                )
            next_x = x + item.sizeHint().width() + space_x
            if next_x - space_x > effective.right() and line_height > 0:
                x = effective.x()
                y = y + line_height + space_y
                next_x = x + item.sizeHint().width() + space_x
                line_height = 0

            if not test_only:
                item.setGeometry(QRect(QPoint(x, y), item.sizeHint()))

            x = next_x
            line_height = max(line_height, item.sizeHint().height())

        return y + line_height - rect.y() + bottom
