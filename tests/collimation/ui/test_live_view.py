import numpy as np
from collimation_tool.ui.live_view import LiveViewLabel
from PySide6.QtWidgets import QApplication


def _frame(height: int, width: int) -> np.ndarray:
    return np.full((height, width), 500.0, dtype=np.float32)


class TestAspectPreservingScale:
    """See the two-camera-panel feature request: a panel's live view must
    not stretch X and Y by different factors, even when its widget size
    doesn't match the source frame's aspect ratio."""

    def test_wide_frame_in_a_square_label_is_not_stretched_non_uniformly(
        self, qapp: object
    ) -> None:
        label = LiveViewLabel()
        label.resize(200, 200)  # square widget, wide (2:1) source frame
        label.set_frame(_frame(100, 200), measurement=None)

        pixmap = label.pixmap()
        assert not pixmap.isNull()
        # KeepAspectRatio: the 2:1 source must still be 2:1 after scaling,
        # not stretched to fill the square label in both dimensions.
        assert pixmap.width() == 2 * pixmap.height()

    def test_tall_frame_in_a_wide_label_keeps_its_aspect_ratio(self, qapp: object) -> None:
        label = LiveViewLabel()
        label.resize(400, 100)  # wide widget, tall (1:2) source frame
        label.set_frame(_frame(200, 100), measurement=None)

        pixmap = label.pixmap()
        assert not pixmap.isNull()
        assert pixmap.height() == 2 * pixmap.width()

    def test_resizing_after_a_frame_is_shown_rescales_it(self, qapp: object) -> None:
        label = LiveViewLabel()
        label.show()
        label.resize(200, 200)
        QApplication.processEvents()
        label.set_frame(_frame(100, 200), measurement=None)
        first_width = label.pixmap().width()

        label.resize(400, 400)
        QApplication.processEvents()
        second_width = label.pixmap().width()

        assert second_width > first_width
        # Still 2:1 after the resize-triggered rescale.
        assert label.pixmap().width() == 2 * label.pixmap().height()

    def test_no_frame_yet_has_no_pixmap(self, qapp: object) -> None:
        label = LiveViewLabel()
        assert label.pixmap().isNull()
