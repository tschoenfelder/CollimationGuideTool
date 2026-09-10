"""Pull a diagnostic bundle from the Pi into the local dev tree (issue #6).

Front half of the ``UUID -> saved frames -> expected result -> permanent
regression test`` workflow: a field failure is captured on the rig, so its
bundle lives under the Pi's ``~/.CollimationGuideTool/diagnostics/<uuid>/``.
This copies it down by UUID (full or unambiguous prefix), after which
``scripts/regression_dataset.py --uuid <uuid> --issue <n>`` scaffolds the
gated dataset from it.

Usage:
    python scripts/pull_diagnostic_bundle.py --uuid af7d27b7
    python scripts/pull_diagnostic_bundle.py --uuid <uuid> --host myrig --force

Uses plain ``ssh`` + ``scp`` against a host from your ``~/.ssh/config``
(default ``rasppi3``). This is the one piece of the regression-intake tooling
that touches the network; ``regression_dataset.py`` itself never does.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from astrotool_core.diagnostics import (
    DEFAULT_DIAGNOSTICS_DIR,
    DEFAULT_PI_HOST,
    DEFAULT_REMOTE_DIAGNOSTICS_DIR,
    CommandRunner,
    RemoteBundleError,
    pull_bundle,
)


def main(argv: list[str] | None = None, *, runner: CommandRunner | None = None) -> int:
    args = _parse_args(argv)
    try:
        local_path = pull_bundle(
            args.uuid,
            host=args.host,
            remote_dir=args.remote_dir,
            local_dir=args.local_dir,
            force=args.force,
            runner=runner,
        )
    except RemoteBundleError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(local_path)
    print(
        f"next: python scripts/regression_dataset.py --uuid {args.uuid} --issue <n>",
        file=sys.stderr,
    )
    return 0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--uuid", required=True, help="diagnostic bundle UUID (full or unambiguous prefix)"
    )
    parser.add_argument(
        "--host",
        default=DEFAULT_PI_HOST,
        help=f"ssh host with the bundle (default: {DEFAULT_PI_HOST})",
    )
    parser.add_argument(
        "--remote-dir",
        default=DEFAULT_REMOTE_DIAGNOSTICS_DIR,
        help=f"the Pi's diagnostics dir (default: {DEFAULT_REMOTE_DIAGNOSTICS_DIR})",
    )
    parser.add_argument(
        "--local-dir",
        type=Path,
        default=DEFAULT_DIAGNOSTICS_DIR,
        help=f"local diagnostics dir to copy into (default: {DEFAULT_DIAGNOSTICS_DIR})",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="re-copy even if the bundle is already present locally",
    )
    return parser.parse_args(argv)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
