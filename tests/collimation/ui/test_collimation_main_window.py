import time

from astrotool_core.camera.replay_camera import ReplayCamera
from astrotool_core.testing.frame_factory import donut_image
from collimation_tool.ui.main_window import MainWindow

_SHAPE = (240, 240)
_CENTER = (120.0, 120.0)


def _donut_camera(offset: tuple[float, float]) -> ReplayCamera:
    array = donut_image(
        _SHAPE,
        outer_center=_CENTER,
        outer_radius=50.0,
        inner_center=(_CENTER[0] + offset[0], _CENTER[1] + offset[1]),
        inner_radius=20.0,
        peak=3000.0,
        background=100.0,
    )
    return ReplayCamera.from_arrays([array], cycle=True)


def test_window_starts_idle(qapp: object) -> None:
    window = MainWindow(_donut_camera((0.0, 0.0)))
    assert window._recommendation_label.text() == "Start the stream to begin."
    assert not window._start_button.isChecked()


def test_starting_the_stream_and_polling_measures_and_shows_overlay(qapp: object) -> None:
    window = MainWindow(_donut_camera((5.0, -2.0)))
    window._start_button.setChecked(True)
    try:
        deadline = time.monotonic() + 2.0
        while "Error" not in window._recommendation_label.text():
            assert time.monotonic() < deadline, "no measurement observed in time"
            time.sleep(0.02)
            window._poll_frame()

        assert not window._live_view.pixmap().isNull()
    finally:
        window._start_button.setChecked(False)


def test_stopping_the_stream_updates_the_label(qapp: object) -> None:
    window = MainWindow(_donut_camera((0.0, 0.0)))
    window._start_button.setChecked(True)
    window._start_button.setChecked(False)
    assert window._recommendation_label.text() == "Stream stopped."
    assert window._stream is None
