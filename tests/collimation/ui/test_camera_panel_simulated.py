"""#52 K01/K03 (S6.2): CameraPanel's stream start/stop and release over the production
`TouptekCameraAdapter` on the simulated ToupTek SDK (fake time).

`_on_toggle_stream` called `self._camera.connect()` with no guard (audit K01): a camera that
cannot be opened raised out of the Start-stream click, leaving the button checked and the camera
selector disabled. A failure injected in each phase -- connect (start of stream), the stream
start itself, stopping the stream (cancel) and the release (disconnect) -- must be shown and
logged once, never raised, and leave the panel able to start again.
"""

from __future__ import annotations

import sys

import pytest
from astrotool_core.acquisition.stream_controller import StreamController
from astrotool_core.camera.touptek_adapter import TouptekCameraAdapter
from astrotool_core.testing import SimulatedToupcamSdk, install_simulated_toupcam
from astrotool_core.testing.sim_touptek import E_ACCESSDENIED, HRESULTException
from astrotool_core.timing import FakeClock
from collimation_tool.ui.camera_panel import CameraPanel


def _fail_next(
    monkeypatch: pytest.MonkeyPatch, target: object, name: str, error: Exception
) -> None:
    """Make `target.name` raise `error` once, then behave normally again."""
    real = getattr(target, name)
    armed = [True]

    def once(*args: object, **kwargs: object) -> object:
        if armed[0]:
            armed[0] = False
            raise error
        return real(*args, **kwargs)

    monkeypatch.setattr(target, name, once)


def _warnings(caplog: pytest.LogCaptureFixture, text: str) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.levelname == "WARNING" and text in r.getMessage()
    ]


def _panel(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[CameraPanel, TouptekCameraAdapter, SimulatedToupcamSdk]:
    sdk = SimulatedToupcamSdk(clock=FakeClock())
    install_simulated_toupcam(monkeypatch, sdk)
    sdk.add_camera("G3M678M", device_id="cam-1")
    adapter = TouptekCameraAdapter(camera_id="cam-1")
    return CameraPanel(adapter, title="Main", device_lister=lambda: []), adapter, sdk


def _assert_idle_and_restartable(panel: CameraPanel) -> None:
    assert not panel._start_button.isChecked()
    assert panel._start_button.text() == "Start stream"
    assert panel._stream is None
    assert panel._camera_combo.isEnabled() and panel._connect_button.isEnabled()


class TestStreamStartGuardsConnect:
    def test_a_camera_that_cannot_be_opened_is_shown_not_raised_and_a_retry_streams(
        self, qapp: object, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The SDK's Open() raising its own HRESULTException (not a ConnectionError), e.g.
        E_ACCESSDENIED while another process still holds the device."""
        panel, _, sdk = _panel(monkeypatch)
        _fail_next(monkeypatch, sdk, "open", HRESULTException(E_ACCESSDENIED))

        with caplog.at_level("WARNING"):
            panel._start_button.setChecked(True)  # must not raise

        _assert_idle_and_restartable(panel)
        assert "Connect failed" in panel._recommendation_label.text()
        assert str(E_ACCESSDENIED) in panel._recommendation_label.text()
        assert len(_warnings(caplog, str(E_ACCESSDENIED))) == 1
        assert sdk.handles == []

        panel._start_button.setChecked(True)  # the device is free now
        try:
            assert panel._stream is not None
            assert panel._start_button.text() == "Stop stream"
            assert not sdk.handles[-1].closed
        finally:
            panel.stop()

    def test_a_failing_stream_start_releases_the_camera(
        self, qapp: object, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        panel, _, sdk = _panel(monkeypatch)
        _fail_next(
            monkeypatch, StreamController, "start_stream", RuntimeError("can't start new thread")
        )

        with caplog.at_level("WARNING"):
            panel._start_button.setChecked(True)

        _assert_idle_and_restartable(panel)
        assert "can't start new thread" in panel._recommendation_label.text()
        assert sdk.handles[-1].closed  # no half-open SDK handle (one handle per device, #45)
        assert len(_warnings(caplog, "can't start new thread")) == 1

        panel._start_button.setChecked(True)
        try:
            assert panel._stream is not None
        finally:
            panel.stop()


class TestStopAndReleaseNeverRaise:
    def test_a_failing_stream_stop_still_returns_the_panel_to_idle(
        self, qapp: object, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        panel, adapter, _ = _panel(monkeypatch)
        panel._start_button.setChecked(True)
        _fail_next(monkeypatch, adapter, "abort_capture", OSError("USB transfer aborted"))

        try:
            with caplog.at_level("WARNING"):
                panel._start_button.setChecked(False)  # must not raise

            _assert_idle_and_restartable(panel)
            assert len(_warnings(caplog, "USB transfer aborted")) == 1
        finally:
            panel.stop()

    def test_a_failing_stream_stop_on_quit_still_releases_the_camera(
        self, qapp: object, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        panel, adapter, sdk = _panel(monkeypatch)
        panel._start_button.setChecked(True)
        _fail_next(monkeypatch, adapter, "abort_capture", OSError("USB transfer aborted"))

        with caplog.at_level("WARNING"):
            panel.stop()  # must not raise

        assert sdk.handles[-1].closed
        assert len(_warnings(caplog, "USB transfer aborted")) == 1

    def test_a_failing_release_on_quit_does_not_raise(
        self, qapp: object, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        panel, adapter, _ = _panel(monkeypatch)
        panel._start_button.setChecked(True)
        panel._start_button.setChecked(False)
        _fail_next(monkeypatch, adapter, "disconnect", OSError("device gone"))

        with caplog.at_level("WARNING"):
            panel.stop()  # must not raise

        assert len(_warnings(caplog, "device gone")) == 1


class TestInternalErrorsReachTheIncidentHook:
    """S6.2 review: before S6.2 an exception in the stream start escaped to sys.excepthook (the
    #10 incident capture). A programming error must still get there -- once -- and say so."""

    def test_an_internal_error_on_stream_start(
        self, qapp: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        hook: list[type[BaseException]] = []
        monkeypatch.setattr(sys, "excepthook", lambda t, _v, _tb: hook.append(t))
        panel, adapter, _ = _panel(monkeypatch)
        _fail_next(monkeypatch, adapter, "connect", AttributeError("'NoneType' has no 'Open'"))

        panel._start_button.setChecked(True)

        _assert_idle_and_restartable(panel)
        assert "Internal error" in panel._recommendation_label.text()
        assert hook == [AttributeError]
        panel.stop()
