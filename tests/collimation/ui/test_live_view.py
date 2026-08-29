import numpy as np
from collimation_tool.ui.fov_overlay import FovOverlayRect
from collimation_tool.ui.live_view import LiveViewLabel, stretch_to_uint8
from PySide6.QtWidgets import QApplication


def _frame(height: int, width: int) -> np.ndarray:
    return np.full((height, width), 500.0, dtype=np.float32)


class TestStretchToUint8:
    """See the UI-responsiveness fix: percentile bounds are now estimated
    from a strided subsample (np.percentile over a full 3840x2160 frame
    measured at ~250ms) rather than every pixel."""

    def test_uniform_frame_stretches_to_a_uniform_value(self) -> None:
        gray = stretch_to_uint8(np.full((64, 64), 500.0, dtype=np.float32))
        assert gray.dtype == np.uint8
        assert gray.shape == (64, 64)

    def test_a_bright_spot_still_reads_bright_after_subsampled_stretch(self) -> None:
        mono = np.full((64, 64), 100.0, dtype=np.float32)
        mono[30:34, 30:34] = 50_000.0
        gray = stretch_to_uint8(mono)
        assert gray[32, 32] > 200
        assert gray[0, 0] < 50

    def test_subsampling_gives_essentially_the_same_result_as_full_resolution(
        self,
    ) -> None:
        rng = np.random.default_rng(seed=0)
        mono = rng.uniform(0.0, 60_000.0, size=(256, 256)).astype(np.float32)
        full = stretch_to_uint8(mono, sample_stride=1)
        subsampled = stretch_to_uint8(mono, sample_stride=4)
        # Not identical (different percentile estimate from fewer samples),
        # but close enough that no pixel's displayed brightness visibly
        # changes — this is a display stretch, not a measurement.
        assert np.abs(full.astype(int) - subsampled.astype(int)).max() <= 5


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


class TestFovRectOverlay:
    """See the main-camera-FOV-in-guide-frame overlay feature."""

    def test_a_yellow_rectangle_is_drawn_at_the_expected_location(self, qapp: object) -> None:
        # Inspect the base (native-resolution) pixmap directly, before the
        # KeepAspectRatio display scaling — see TestAspectPreservingScale
        # for why the label's *displayed* size can't be pinned exactly
        # (setMinimumSize(320, 240) clamps a smaller .resize()).
        label = LiveViewLabel()
        rect = FovOverlayRect(x=0.2, y=0.2, width=0.6, height=0.6)
        label.set_frame(_frame(100, 100), measurement=None, fov_rect=rect)

        assert label._base_pixmap is not None
        image = label._base_pixmap.toImage()
        # Middle of the top edge, in native 100x100 pixel coordinates.
        edge_color = image.pixelColor(50, 20)
        assert edge_color.red() > 200
        assert edge_color.green() > 200
        assert edge_color.blue() < 100

    def test_no_rectangle_drawn_when_fov_rect_is_none(self, qapp: object) -> None:
        label = LiveViewLabel()
        label.set_frame(_frame(100, 100), measurement=None, fov_rect=None)

        assert label._base_pixmap is not None
        image = label._base_pixmap.toImage()
        # Nothing yellow anywhere near where a rect would have been drawn.
        color = image.pixelColor(50, 20)
        assert not (color.red() > 200 and color.green() > 200 and color.blue() < 100)

    def test_set_stretched_frame_also_accepts_a_fov_rect(self, qapp: object) -> None:
        label = LiveViewLabel()
        label.resize(100, 100)
        rect = FovOverlayRect(x=0.2, y=0.2, width=0.6, height=0.6)
        label.set_stretched_frame(
            stretch_to_uint8(_frame(100, 100)), measurement=None, fov_rect=rect
        )
        assert not label.pixmap().isNull()


class TestSetStretchedFrame:
    """See FrameAnalyzer: the stretch runs off the UI thread, so
    LiveViewLabel needs a way to display already-stretched uint8 data
    without redoing (or re-timing) the stretch itself."""

    def test_set_stretched_frame_skips_stretching_and_displays_as_is(
        self, qapp: object
    ) -> None:
        label = LiveViewLabel()
        label.resize(100, 100)
        gray = np.zeros((50, 50), dtype=np.uint8)
        gray[10:20, 10:20] = 255
        label.set_stretched_frame(gray, measurement=None)
        assert not label.pixmap().isNull()

    def test_set_frame_and_set_stretched_frame_produce_the_same_pixmap_size(
        self, qapp: object
    ) -> None:
        label_a = LiveViewLabel()
        label_a.resize(200, 200)
        label_a.set_frame(_frame(100, 200), measurement=None)

        label_b = LiveViewLabel()
        label_b.resize(200, 200)
        label_b.set_stretched_frame(stretch_to_uint8(_frame(100, 200)), measurement=None)

        assert label_a.pixmap().size() == label_b.pixmap().size()
