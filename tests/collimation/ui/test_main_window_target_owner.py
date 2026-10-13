"""S6.6b (S6.4 finding M05): "is the target an artificial star?" has ONE owner, the
fine-collimation panel's `CollimationTargetMode`.

Before, it was decided in three places: the target-mode selector, the FOV registration's
"Artificial Star" button and the autofocus mode. Mount Align's sizing distance
(`_calibration_distance_m`) read only the registration button, so an artificial-star TARGET
chosen in the fine-collimation panel still sized the first calibration move for a 10 km
target instead of ~30 m."""

from __future__ import annotations

import numpy as np
from astrotool_core.camera.replay_camera import ReplayCamera
from astrotool_core.focus.fake_focuser import FakeFocuser
from astrotool_core.mount.operating_mode import OperatingMode
from astrotool_core.testing.fake_mount import FakeMountAdapter
from astrotool_core.testing.fake_mount_park import FakeMountPark
from collimation_tool.domain.target_mode import CollimationTargetMode
from collimation_tool.ui.main_window import MainWindow


def _window() -> MainWindow:
    image = np.full((60, 80), 100.0, dtype=np.float32)
    return MainWindow(
        ReplayCamera.from_arrays([image], cycle=True),
        device_lister=lambda: [],
        focuser=FakeFocuser(),
        mount=FakeMountPark(start_parked=False),
        pulse_mount=FakeMountAdapter(),
    )


def _target(window: MainWindow) -> CollimationTargetMode:
    return window._fine_collimation_panel.target_mode  # read fresh (no mypy narrowing)


def _mode(window: MainWindow) -> OperatingMode:
    return window._tracking_enforcer.mode


def test_an_artificial_star_target_sizes_mount_align_for_a_near_target(qapp: object) -> None:
    window = _window()

    window._fine_collimation_panel.set_target_mode(CollimationTargetMode.ARTIFICIAL_STAR)

    assert window._calibration_distance_m("terrestrial") == 30.0
    window._fine_collimation_panel.set_target_mode(CollimationTargetMode.NATURAL_STAR)
    assert window._calibration_distance_m("terrestrial") == 10_000.0
    assert window._calibration_distance_m("star") is None
    window.close()


def test_the_registration_choice_follows_the_target_owner_both_ways(qapp: object) -> None:
    window = _window()

    window._artificial_star_mode_button.click()
    assert window._fine_collimation_panel.target_mode is CollimationTargetMode.ARTIFICIAL_STAR

    window._fine_collimation_panel.set_target_mode(CollimationTargetMode.NATURAL_STAR)
    assert not window._artificial_star_mode_button.isChecked()

    window._fine_collimation_panel.set_target_mode(CollimationTargetMode.ARTIFICIAL_STAR)
    assert window._artificial_star_mode_button.isChecked()
    window.close()


def test_choosing_another_matcher_makes_the_target_a_natural_star_again(qapp: object) -> None:
    """Review C4: leaving the artificial-star matcher on the registration side resets the one
    owner -- an artificial-star target with an NCC matcher must not exist."""
    window = _window()
    window._artificial_star_mode_button.click()
    assert _target(window) is CollimationTargetMode.ARTIFICIAL_STAR

    window._terrestrial_mode_button.click()

    assert _target(window) is CollimationTargetMode.NATURAL_STAR
    assert not window._artificial_star_mode_button.isChecked()
    assert window._calibration_distance_m("terrestrial") == 10_000.0
    window.close()


def test_user_decision_a_still_holds(qapp: object) -> None:
    """S6.4 decision A: an artificial star implies Terrestrial; choosing Astronomical returns
    the target to a natural star (and the registration choice follows)."""
    window = _window()
    window._operating_astronomical_button.click()
    assert _mode(window) is OperatingMode.ASTRONOMICAL

    window._fine_collimation_panel.set_target_mode(CollimationTargetMode.ARTIFICIAL_STAR)
    assert _mode(window) is OperatingMode.TERRESTRIAL
    assert window._artificial_star_mode_button.isChecked()

    window._operating_astronomical_button.click()
    assert _target(window) is CollimationTargetMode.NATURAL_STAR
    assert window._star_field_mode_button.isChecked()
    assert not window._artificial_star_mode_button.isChecked()
    window.close()
