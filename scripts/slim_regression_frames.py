"""Copy a diagnostic bundle's frames into the git-ignored ``local_test_data/`` tree as
lossless uint16 FITS (issue #56).

Diagnostic bundles store camera frames as float32 FITS; for a 16-bit (or smaller) camera
every value is an integer in 0..65535, so uint16 halves the size with no loss. This script
refuses any frame that is not exactly representable and verifies the round trip bit for bit.
Large regression frames are never committed: a dataset's ``expected.json`` points at
``local_test_data/<dataset>/frames/frame_<n>.fits`` and its tests skip when they are absent
(same convention as ``datasets/regressions/28/``).

Usage (after ``python scripts/pull_diagnostic_bundle.py --uuid <uuid>`` if the bundle is on
the Pi):

    python scripts/slim_regression_frames.py --uuid 402eabde --dataset 56 --frames 0 1 2
    python scripts/slim_regression_frames.py --uuid f0bc314b --dataset 56-dusk --frames 0 1 2

Never touches the network, git, or anything outside ``local_test_data/<dataset>/frames/``.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
from astropy.io import fits
from astrotool_core.diagnostics import find_bundle

REPO_ROOT = Path(__file__).resolve().parents[1]


def slim_frame(source: Path, target: Path) -> None:
    """Write `source`'s primary HDU to `target` as uint16; raise unless bit-exact."""
    with fits.open(source) as hdul:
        data = np.asarray(hdul[0].data)
        header = hdul[0].header.copy()
    if not (np.all(np.isfinite(data)) and np.array_equal(data, np.round(data))):
        raise ValueError(f"{source}: not integer-valued -- uint16 would lose data")
    if data.min() < 0 or data.max() > 65535:
        raise ValueError(f"{source}: values {data.min()}..{data.max()} outside uint16")
    for key in ("BITPIX", "BZERO", "BSCALE"):
        header.remove(key, ignore_missing=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    fits.PrimaryHDU(data=data.astype(np.uint16), header=header).writeto(target, overwrite=True)
    back = np.asarray(fits.getdata(target))
    if not np.array_equal(back.astype(np.float64), data.astype(np.float64)):
        raise ValueError(f"{target}: round trip is not bit-exact")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--uuid", required=True, help="diagnostic bundle UUID (or prefix)")
    parser.add_argument("--dataset", required=True, help="dataset name, e.g. 56 or 56-dusk")
    parser.add_argument("--frames", type=int, nargs="+", required=True, help="bundle frame indices")
    args = parser.parse_args(argv)

    bundle = find_bundle(args.uuid)
    if bundle is None:
        parser.error(f"no diagnostic bundle matches {args.uuid!r} (pull it first)")
    out_dir = REPO_ROOT / "local_test_data" / args.dataset / "frames"
    for index in args.frames:
        source = bundle / "frames" / f"frame_{index}.fits"
        target = out_dir / f"frame_{index}.fits"
        slim_frame(source, target)
        print(f"{source} -> {target} ({target.stat().st_size} bytes, bit-exact)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
