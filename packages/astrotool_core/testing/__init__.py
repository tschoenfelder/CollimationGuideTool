"""Shared test doubles and fixtures usable by both apps' test suites.

Synthetic frame factory, replay dataset loader, fake camera/mount, and the
issue #51 hardware-boundary scenario simulators (`sim_*` modules: OnStep
mount/focuser, INDI filter wheel, ToupTek SDK, explicit-timestamp frames) --
see CONTRIBUTING.md "Reproducing a hardware bug locally".
"""

from astrotool_core.testing.fake_indi_server import FakeIndiServer
from astrotool_core.testing.fake_mount import FakeMountAdapter
from astrotool_core.testing.fake_mount_park import FakeMountPark
from astrotool_core.testing.fake_touptek import FakeTouptekCamera
from astrotool_core.testing.frame_factory import (
    StarSpec,
    bayer_star_field_image,
    donut_image,
    make_frame,
    single_star_image,
    star_field_image,
    with_hot_pixels,
    with_shadow,
)
from astrotool_core.testing.replay_dataset import (
    discover_fits_paths,
    load_expected,
    load_frames,
)
from astrotool_core.testing.shift_grid import (
    CI_SHIFT_GRID_SUBSET,
    KNOWN_SHIFT_GRID,
    ShiftCase,
)
from astrotool_core.testing.sim_frames import (
    CameraFrameSource,
    FrameSpec,
    FrameTimeline,
    MotionWindow,
    frames_from_replay,
)
from astrotool_core.testing.sim_indi_filter_wheel import (
    FilterWheelScenario,
    SimulatedIndiClient,
    SimulatedIndiFilterWheel,
    make_simulated_filter_wheel_adapter,
)
from astrotool_core.testing.sim_onstep import (
    CompoundOperationMonitor,
    ConnectFailure,
    FocuserScenario,
    GuidePulseScenario,
    ObservableRLock,
    OnStepScenario,
    SimulatedIndiFocuser,
    SimulatedOnStepIndiClient,
    install_observable_operation_lock,
    install_onstep_adapter_050_exports,
    make_simulated_onstep_connection,
)
from astrotool_core.testing.sim_touptek import (
    CAMERA_MODELS,
    CameraModel,
    SimulatedToupcam,
    SimulatedToupcamSdk,
    install_simulated_toupcam,
)

__all__ = [
    "CAMERA_MODELS",
    "CI_SHIFT_GRID_SUBSET",
    "CameraFrameSource",
    "CameraModel",
    "CompoundOperationMonitor",
    "ConnectFailure",
    "FilterWheelScenario",
    "FocuserScenario",
    "GuidePulseScenario",
    "FrameSpec",
    "FrameTimeline",
    "MotionWindow",
    "ObservableRLock",
    "OnStepScenario",
    "SimulatedIndiClient",
    "SimulatedIndiFilterWheel",
    "SimulatedIndiFocuser",
    "SimulatedOnStepIndiClient",
    "SimulatedToupcam",
    "SimulatedToupcamSdk",
    "frames_from_replay",
    "install_observable_operation_lock",
    "install_onstep_adapter_050_exports",
    "install_simulated_toupcam",
    "make_simulated_filter_wheel_adapter",
    "make_simulated_onstep_connection",
    "FakeIndiServer",
    "FakeMountAdapter",
    "FakeMountPark",
    "FakeTouptekCamera",
    "KNOWN_SHIFT_GRID",
    "ShiftCase",
    "StarSpec",
    "bayer_star_field_image",
    "discover_fits_paths",
    "donut_image",
    "load_expected",
    "load_frames",
    "make_frame",
    "single_star_image",
    "star_field_image",
    "with_hot_pixels",
    "with_shadow",
]
