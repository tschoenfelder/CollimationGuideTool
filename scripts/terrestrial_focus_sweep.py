"""Sweep the focuser over a bounded range around its current (or given)
position, capturing real terrestrial frames and the current terrestrial-
focus metric's own value at each position -- issue #35's real-field
follow-up investigation tool.

Focuser + camera only: never imports a mount port at all, so it
structurally cannot touch the mount. Always attempts to return the
focuser to its starting position when done, even on a partial failure.

The camera side (`TouptekCameraAdapter`) talks to the vendor SDK
directly over USB, and the focuser goes through OnStepAdapter's INDI-backed
transport (>= 0.4.0), so this must run on the machine both are attached to
(the Pi), with indiserver's `indi_lx200_OnStep` driver already running.

Usage (run on the Pi, where the camera is attached):
    python scripts/terrestrial_focus_sweep.py --out-dir ~/sweep_output
"""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
from astropy.io import fits
from astrotool_core.camera.touptek_adapter import TouptekCameraAdapter
from astrotool_core.focus.port import FocuserPort
from astrotool_core.focus.terrestrial_focus_metric import measure_terrestrial_focus
from astrotool_core.onstep import OnStepConnection, OnStepFocuserAdapter, load_onstep_indi_config

_DEFAULT_RANGE_STEPS = 500
_DEFAULT_STEP = 50
_DEFAULT_EXPOSURE_MS = 5.501
_DEFAULT_GAIN = 100
_DEFAULT_SAMPLE_COUNT = 3
#: Extra margin beyond the driver's own reported settle, matching the
#: same "generous ceiling against a wedged connection" reasoning
#: BoundedFocusSearcher's own move-settle wait uses.
_SETTLE_BUFFER_S = 0.3
_MOVE_SETTLE_TIMEOUT_S = 10.0


@dataclass(frozen=True)
class SweepSample:
    position: int
    sharpness: float | None
    confidence: float
    #: Independent, deliberately differently-coded sharpness proxy --
    #: NOT reusing measure_terrestrial_focus's own tiling/rejection
    #: logic, so it can act as a genuine cross-check rather than just
    #: re-deriving the same number a second time.
    gradient_energy: float


def _gradient_energy(image: np.ndarray) -> float:
    data = image.astype(np.float64)
    gx = np.diff(data, axis=1)
    gy = np.diff(data, axis=0)
    return float(np.mean(gx[:-1, :] ** 2) + np.mean(gy[:, :-1] ** 2))


def _wait_for_settle(focuser: FocuserPort) -> None:
    deadline = time.monotonic() + _MOVE_SETTLE_TIMEOUT_S
    while focuser.is_moving() and time.monotonic() < deadline:
        time.sleep(0.05)
    time.sleep(_SETTLE_BUFFER_S)


def run_sweep(
    *,
    camera_name: str,
    center: int | None,
    range_steps: int,
    step: int,
    exposure_ms: float,
    gain: int,
    sample_count: int,
    out_dir: Path,
) -> list[SweepSample]:
    out_dir.mkdir(parents=True, exist_ok=True)

    focuser = OnStepFocuserAdapter(OnStepConnection(load_onstep_indi_config()))
    focuser.connect()
    if not focuser.is_available:
        raise RuntimeError("focuser connected but not available -- no hardware detected")

    start_position = center if center is not None else focuser.get_position()
    print(f"start_position={start_position}")

    camera = TouptekCameraAdapter(name=camera_name)
    camera.connect()
    camera.set_exposure_ms(exposure_ms)
    camera.set_gain(gain)

    samples: list[SweepSample] = []
    try:
        positions = range(start_position - range_steps, start_position + range_steps + 1, step)
        for position in positions:
            result = focuser.move_absolute(position)
            if not result.accepted:
                print(f"position={position}: move REJECTED, skipping")
                continue
            _wait_for_settle(focuser)

            frames = [camera.capture(exposure_ms / 1000.0).pixels for _ in range(sample_count)]
            fits.PrimaryHDU(data=frames[0].astype(np.float32)).writeto(
                out_dir / f"sweep_pos_{position}.fits", overwrite=True
            )
            measurement = measure_terrestrial_focus(frames)
            cross_check = float(np.mean([_gradient_energy(f) for f in frames]))
            sample = SweepSample(
                position=position,
                sharpness=measurement.sharpness,
                confidence=measurement.confidence,
                gradient_energy=cross_check,
            )
            samples.append(sample)
            print(
                f"position={position}: sharpness={measurement.sharpness} "
                f"confidence={measurement.confidence:.2f} gradient_energy={cross_check:.2f}"
            )
    finally:
        # Always try to return to the starting position, even if a
        # capture/move failed partway through -- leave the rig as found.
        try:
            focuser.move_absolute(start_position)
            _wait_for_settle(focuser)
            print(f"returned focuser to start_position={start_position}")
        except Exception as exc:  # noqa: BLE001 - best-effort recovery, never mask the real error
            print(f"WARNING: failed to return focuser to start_position={start_position}: {exc}")
        camera.disconnect()
        focuser.disconnect()

    summary_path = out_dir / "sweep_summary.json"
    summary_path.write_text(
        json.dumps(
            {
                "start_position": start_position,
                "range_steps": range_steps,
                "step": step,
                "samples": [asdict(s) for s in samples],
            },
            indent=2,
        )
    )
    print(f"summary written to {summary_path}")
    return samples


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--camera-name", default="ATR585M")
    parser.add_argument(
        "--center", type=int, default=None, help="defaults to the focuser's current live position"
    )
    parser.add_argument("--range-steps", type=int, default=_DEFAULT_RANGE_STEPS)
    parser.add_argument("--step", type=int, default=_DEFAULT_STEP)
    parser.add_argument("--exposure-ms", type=float, default=_DEFAULT_EXPOSURE_MS)
    parser.add_argument("--gain", type=int, default=_DEFAULT_GAIN)
    parser.add_argument("--sample-count", type=int, default=_DEFAULT_SAMPLE_COUNT)
    parser.add_argument("--out-dir", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    run_sweep(
        camera_name=args.camera_name,
        center=args.center,
        range_steps=args.range_steps,
        step=args.step,
        exposure_ms=args.exposure_ms,
        gain=args.gain,
        sample_count=args.sample_count,
        out_dir=args.out_dir,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
