import numpy as np
from collimation_tool.domain.collimation_measurement import CircleEllipseFit, DonutMeasurement
from collimation_tool.ui.fov_overlay import FovOverlayRect
from collimation_tool.ui.live_view import LiveViewLabel, stretch_rgb_to_uint8, stretch_to_uint8
from PySide6.QtCore import Qt
from PySide6.QtGui import QImage
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


class TestDegenerateUniformFrame:
    """Real-hardware bug ("guide stays black"): a sensor pinned at its
    true ADC ceiling produces a perfectly uniform frame with hi <= lo
    after the percentile stretch. The old fallback always rendered
    BLACK, indistinguishable from a genuinely empty/no-signal frame —
    hiding that the sensor was actually saturated (see auto_exposure's
    companion saturation-fraction fix for the other half of this bug)."""

    def test_a_uniform_bright_frame_renders_white_not_black(self) -> None:
        gray = stretch_to_uint8(np.full((64, 64), 4094.0, dtype=np.float32))
        assert gray.min() == 255

    def test_a_uniform_zero_frame_still_renders_black(self) -> None:
        gray = stretch_to_uint8(np.zeros((64, 64), dtype=np.float32))
        assert gray.max() == 0

    def test_a_small_bright_spot_missed_by_subsampling_is_still_detected(self) -> None:
        # The subsample (stride 4) can land entirely between a small
        # bright feature's pixels, making the *sample* look uniform even
        # though the full frame isn't — must not fall through to the
        # degenerate all-white case and wash out the real, mostly-dim
        # frame.
        mono = np.full((64, 64), 100.0, dtype=np.float32)
        mono[1:3, 1:3] = 50_000.0  # small enough to plausibly miss stride-4 sampling
        gray = stretch_to_uint8(mono)
        assert gray[32, 32] < 50
        assert gray[1, 1] > 200


class TestStretchRgbToUint8:
    """See the "guide cam is color, but picture seems mono" bug: the live
    view was always built from the mono luma plane, even for a color
    camera, so a color sensor's feed never showed color at all."""

    def test_output_is_three_channel_uint8(self) -> None:
        rgb = np.full((32, 32, 3), 500.0, dtype=np.float32)
        stretched = stretch_rgb_to_uint8(rgb)
        assert stretched.dtype == np.uint8
        assert stretched.shape == (32, 32, 3)

    def test_color_ratios_are_preserved_not_stretched_independently(self) -> None:
        # A tinted frame (more red than green/blue) with genuine spatial
        # variation (a gradient, not flat — a flat frame hits the
        # degenerate all-white/all-black case tested separately below,
        # which has no color to preserve in the first place). An
        # independent per-channel stretch would normalize each channel to
        # its own full range and destroy the tint (turning it gray); a
        # single luma-derived range applied to all channels preserves the
        # ratio between them.
        gradient = np.linspace(0.0, 1.0, 32).reshape(1, 32)
        rgb = np.zeros((32, 32, 3), dtype=np.float32)
        rgb[..., 0] = 500.0 + 2500.0 * gradient
        rgb[..., 1] = 500.0 + 500.0 * gradient
        rgb[..., 2] = 500.0 + 100.0 * gradient
        stretched = stretch_rgb_to_uint8(rgb)
        assert stretched[..., 0].mean() > stretched[..., 1].mean() > stretched[..., 2].mean()

    def test_a_bright_spot_is_visible_in_all_three_channels(self) -> None:
        rgb = np.full((64, 64, 3), 100.0, dtype=np.float32)
        rgb[30:34, 30:34, :] = 50_000.0
        stretched = stretch_rgb_to_uint8(rgb)
        assert stretched[32, 32, :].min() > 200
        assert stretched[0, 0, :].max() < 50

    def test_a_uniform_saturated_frame_renders_white_not_black(self) -> None:
        # Same degenerate-frame handling as the mono stretch_to_uint8 —
        # see TestDegenerateUniformFrame.
        rgb = np.full((32, 32, 3), 4094.0, dtype=np.float32)
        stretched = stretch_rgb_to_uint8(rgb)
        assert stretched.min() == 255

    def test_a_uniform_zero_frame_still_renders_black(self) -> None:
        rgb = np.zeros((32, 32, 3), dtype=np.float32)
        stretched = stretch_rgb_to_uint8(rgb)
        assert stretched.max() == 0


class TestColorFrameDisplay:
    """LiveViewLabel must accept an (H, W, 3) stretched array (a color
    camera's live view) in addition to the existing 2D mono case."""

    def test_a_color_stretched_frame_displays_without_error(self, qapp: object) -> None:
        label = LiveViewLabel()
        label.resize(100, 100)
        rgb = np.zeros((50, 50, 3), dtype=np.uint8)
        rgb[..., 0] = 200  # a visibly red frame
        label.set_stretched_frame(rgb, measurement=None)
        assert not label.pixmap().isNull()

    def test_a_color_frames_actual_color_is_preserved_on_screen(self, qapp: object) -> None:
        label = LiveViewLabel()
        rgb = np.zeros((20, 20, 3), dtype=np.uint8)
        rgb[..., 0] = 10  # deliberately red-dominant, not grayscale
        rgb[..., 1] = 200
        rgb[..., 2] = 10
        label.set_stretched_frame(rgb, measurement=None)

        assert label._base_pixmap is not None
        color = label._base_pixmap.toImage().pixelColor(10, 10)
        assert color.green() > color.red()
        assert color.green() > color.blue()


class TestFovPolygonOverlay:
    """A calibrated (fov_registration) match takes precedence over the
    config-only placeholder rectangle, and draws as an actual rotated
    quadrilateral rather than an axis-aligned box."""

    def test_a_polygon_is_drawn_at_the_given_corners(self, qapp: object) -> None:
        label = LiveViewLabel()
        # A rotated (diamond-shaped) quadrilateral centered in a 100x100 frame.
        polygon = [(50.0, 10.0), (90.0, 50.0), (50.0, 90.0), (10.0, 50.0)]
        label.set_frame(_frame(100, 100), measurement=None, fov_polygon=polygon)

        assert label._base_pixmap is not None
        image = label._base_pixmap.toImage()
        edge_color = image.pixelColor(50, 10)
        assert edge_color.red() > 200
        assert edge_color.green() > 200
        assert edge_color.blue() < 100

    def test_polygon_takes_precedence_over_fov_rect_when_both_are_given(
        self, qapp: object
    ) -> None:
        label = LiveViewLabel()
        rect = FovOverlayRect(x=0.2, y=0.2, width=0.6, height=0.6)  # would draw at (20,20)-(80,80)
        polygon = [(50.0, 10.0), (90.0, 50.0), (50.0, 90.0), (10.0, 50.0)]
        label.set_frame(_frame(100, 100), measurement=None, fov_rect=rect, fov_polygon=polygon)

        assert label._base_pixmap is not None
        image = label._base_pixmap.toImage()
        # The rect's top edge (y=20) would be yellow if it were drawn —
        # only the polygon (whose topmost point is y=10) should be.
        rect_edge_color = image.pixelColor(50, 20)
        assert not (
            rect_edge_color.red() > 200
            and rect_edge_color.green() > 200
            and rect_edge_color.blue() < 100
        )


class TestMatchedPointMarker:
    """Issue #37: the artificial-star registration's own matched-point
    marker -- a true red crosshair+circle, independent of fov_rect/
    fov_polygon (identifies one point, not a footprint)."""

    def test_a_red_marker_is_drawn_at_the_matched_point(self, qapp: object) -> None:
        label = LiveViewLabel()
        label.set_frame(_frame(100, 100), measurement=None, matched_point=(50.0, 50.0))

        assert label._base_pixmap is not None
        image = label._base_pixmap.toImage()
        # The marker's own crosshair passes directly through its center.
        color = image.pixelColor(50, 50)
        assert color.red() > 200
        assert color.green() < 50
        assert color.blue() < 50

    def test_no_marker_drawn_when_matched_point_is_none(self, qapp: object) -> None:
        label = LiveViewLabel()
        label.set_frame(_frame(100, 100), measurement=None, matched_point=None)

        assert label._base_pixmap is not None
        image = label._base_pixmap.toImage()
        color = image.pixelColor(50, 50)
        assert not (color.red() > 200 and color.green() < 50 and color.blue() < 50)

    def test_set_stretched_frame_also_accepts_a_matched_point(self, qapp: object) -> None:
        label = LiveViewLabel()
        label.resize(100, 100)
        label.set_stretched_frame(
            stretch_to_uint8(_frame(100, 100)), measurement=None, matched_point=(50.0, 50.0)
        )
        assert not label.pixmap().isNull()


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


def _rgb_array(image: QImage) -> np.ndarray:
    """(H, W, 3) uint8 RGB copy of *image* (any format)."""
    rgb = image.convertToFormat(QImage.Format.Format_RGB888)
    height, width = rgb.height(), rgb.width()
    raw = np.frombuffer(rgb.constBits(), dtype=np.uint8, count=rgb.sizeInBytes())
    rows = raw.reshape(height, rgb.bytesPerLine())
    return rows[:, : width * 3].reshape(height, width, 3).copy()


def _green_mask(rgb: np.ndarray) -> np.ndarray:
    """Pixels exactly the cross colour (pure green, 0/255/0)."""
    return (rgb[..., 0] == 0) & (rgb[..., 1] == 255) & (rgb[..., 2] == 0)


def _ring_measurement() -> DonutMeasurement:
    outer = CircleEllipseFit(center_x=60.0, center_y=40.0, radius_x=25.0, radius_y=25.0)
    inner = CircleEllipseFit(center_x=63.0, center_y=42.0, radius_x=8.0, radius_y=8.0)
    return DonutMeasurement(
        outer_ring=outer,
        inner_hole=inner,
        error_x_px=3.0,
        error_y_px=2.0,
        error_magnitude_px=3.6,
        error_angle_deg=33.7,
        confidence=0.9,
    )


class TestCentreCross:
    """Issue #57: every displayed frame shows a green cross through the
    centre of the displayed (scaled, aspect-kept) image -- full-length
    lines, exactly 2 screen px thick at any widget size, drawn on the
    scaled pixmap only (the native base pixmap, which the diagnostic
    display snapshot saves, is unchanged)."""

    # (widget size, frame shape (h, w), expected displayed size (w, h)).
    # The second widget's aspect (640x260) differs from the frame's 2:1,
    # so the image is height-limited and narrower than the widget.
    _CASES = [
        ((400, 400), (100, 200), (400, 200)),
        ((640, 260), (100, 200), (520, 260)),
    ]

    @staticmethod
    def _shown(label: LiveViewLabel) -> np.ndarray:
        return _rgb_array(label.pixmap().toImage())

    def _show(
        self, size: tuple[int, int], frame_shape: tuple[int, int], **overlays: object
    ) -> LiveViewLabel:
        label = LiveViewLabel()
        label.resize(*size)
        label.set_frame(_frame(*frame_shape), measurement=None, **overlays)  # type: ignore[arg-type]
        return label

    def test_cross_is_two_px_thick_full_length_and_centred(self, qapp: object) -> None:
        for size, frame_shape, (exp_w, exp_h) in self._CASES:
            rgb = self._shown(self._show(size, frame_shape))
            height, width, _ = rgb.shape
            assert (width, height) == (exp_w, exp_h)
            green = _green_mask(rgb)

            # Even sizes: the 2 px pair sits symmetric around the centre.
            rows = [height // 2 - 1, height // 2]
            cols = [width // 2 - 1, width // 2]
            for r in rows:
                assert green[r, :].all(), f"row {r} not fully green at {size}"
            for c in cols:
                assert green[:, c].all(), f"column {c} not fully green at {size}"

            # Exactly 2 px: only those rows / columns contain green at all.
            green_rows = np.flatnonzero(green.all(axis=1))
            green_cols = np.flatnonzero(green.all(axis=0))
            assert list(green_rows) == rows
            assert list(green_cols) == cols
            off_cross = green.copy()
            off_cross[rows, :] = False
            off_cross[:, cols] = False
            assert not off_cross.any(), f"green outside the cross at {size}"

    def test_cross_follows_a_resize(self, qapp: object) -> None:
        label = LiveViewLabel()
        label.show()
        label.resize(400, 400)
        QApplication.processEvents()
        label.set_frame(_frame(100, 200), measurement=None)
        label.resize(640, 260)
        QApplication.processEvents()

        green = _green_mask(self._shown(label))
        assert green.shape == (260, 520)
        assert list(np.flatnonzero(green.all(axis=1))) == [129, 130]
        assert list(np.flatnonzero(green.all(axis=0))) == [259, 260]

    def test_base_pixmap_has_no_green(self, qapp: object) -> None:
        label = self._show((640, 260), (100, 200))
        assert label._base_pixmap is not None
        assert not _green_mask(_rgb_array(label._base_pixmap.toImage())).any()

    def test_existing_overlays_are_unchanged_off_the_cross(self, qapp: object) -> None:
        overlays = {
            "fov_rect": FovOverlayRect(x=0.2, y=0.2, width=0.6, height=0.6),
            "matched_point": (150.0, 70.0),
        }
        for size, frame_shape, _ in self._CASES:
            label = LiveViewLabel()
            label.resize(*size)
            label.set_frame(_frame(*frame_shape), measurement=_ring_measurement(), **overlays)  # type: ignore[arg-type]
            assert label._base_pixmap is not None
            base = _rgb_array(label._base_pixmap.toImage())
            # Rings, FOV rect and matched point are still in the native frame.
            assert ((base[..., 0] < 120) & (base[..., 1] > 180) & (base[..., 2] > 220)).any()
            assert base[20, 100, 2] < 100  # yellow FOV top edge (centre of edge)
            assert base[70, 150, 0] > 200 and base[70, 150, 1] < 50  # red marker

            shown = self._shown(label)
            expected = _rgb_array(
                label._base_pixmap.scaled(
                    label.pixmap().size(),
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                ).toImage()
            )
            height, width, _ = shown.shape
            keep = np.ones((height, width), dtype=bool)
            keep[[height // 2 - 1, height // 2], :] = False
            keep[:, [width // 2 - 1, width // 2]] = False
            # Off the cross the displayed image is exactly the scaled frame
            # with its overlays -- nothing else changed.
            assert np.array_equal(shown[keep], expected[keep])
