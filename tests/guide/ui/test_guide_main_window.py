import time

from astrotool_core.camera.fake_camera import FakeCamera
from guide_tool.ui.main_window import MainWindow


def test_window_starts_idle(qapp: object) -> None:
    window = MainWindow(FakeCamera())
    assert window._status_label.text() == "Start guiding to begin."
    assert not window._start_button.isChecked()


def test_starting_guiding_and_polling_shows_healthy_status_and_frame(qapp: object) -> None:
    window = MainWindow(FakeCamera())
    window._start_button.setChecked(True)
    try:
        deadline = time.monotonic() + 2.0
        while "Health: healthy" not in window._status_label.text():
            assert time.monotonic() < deadline, "guiding never reported healthy in time"
            time.sleep(0.02)
            window._poll_status()

        assert not window._live_view.pixmap().isNull()
    finally:
        window._start_button.setChecked(False)


def test_stopping_guiding_updates_the_label(qapp: object) -> None:
    window = MainWindow(FakeCamera())
    window._start_button.setChecked(True)
    window._start_button.setChecked(False)
    assert window._status_label.text() == "Stopped."
