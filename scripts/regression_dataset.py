"""Scaffold a ``datasets/regressions/<issue-id>/`` case from a diagnostic bundle.

Issue #6: turn a reproduced field failure — already captured as a
``~/.CollimationGuideTool/diagnostics/<uuid>/`` bundle — into the skeleton of a
permanent, gated regression dataset. This copies the real frames verbatim and
pre-fills provenance from ``incident.json``; a human then authors each case's
expected result and ``rationale`` from an *independent* source (never from the
implementation under test), and — if the bug's boundary is new — registers a
runner in ``tests/regressions/_loader.py``'s ``BOUNDARIES``.

Usage:
    python scripts/regression_dataset.py --uuid <diagnostic-uuid> --issue 34
    python scripts/regression_dataset.py --uuid ef49ecb1 --slug saturated-guide
    python scripts/regression_dataset.py --uuid <uuid>            # -> diag-<uuid8>/

Never touches the mount, the network, or an existing committed dataset (refuses
an existing target unless ``--force``). Does not downsample — frames are copied
at full resolution.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from astrotool_core.diagnostics import find_bundle

REPO_ROOT = Path(__file__).resolve().parent.parent
REGRESSIONS_DIR = REPO_ROOT / "datasets" / "regressions"

_README_TEMPLATE = """\
# {leaf}

Real captured frames from diagnostic `{uuid}` (git commit `{git_commit}`).

> {user_report}

## Provenance

- Originating issue: {issue_line}
- Diagnostic UUID(s): {uuid}
- Git commit at capture: {git_commit}
- Optical setup: {optical_setup}
- Raw bundle `incident.json` / `application.log`: copied under `provenance/`.

## Root cause

TODO: what actually went wrong (link the fix commit once it lands).

## Fix commit

TODO

## How the expected values were derived

TODO: every case in `expected.json` must state, in its `rationale`, how its
expected value was obtained *independently of the implementation under test* —
hand measurement, a known software transform, a physical ground truth, or
"must reject, by inspection". Do not paste `measure_translation_offset()` or
the calibrator's own output.
"""


@dataclass(frozen=True)
class _Args:
    uuid: str
    issue: int | None
    slug: str | None
    force: bool


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    bundle = find_bundle(args.uuid)
    if bundle is None:
        diagnostics = Path.home() / ".CollimationGuideTool" / "diagnostics"
        print(
            f"error: no diagnostic bundle matches {args.uuid!r} (looked under {diagnostics})\n"
            f"  if the failure was reproduced on the Pi, pull it first:\n"
            f"    python scripts/pull_diagnostic_bundle.py --uuid {args.uuid}",
            file=sys.stderr,
        )
        return 2
    incident = _read_incident(bundle)

    leaf = _leaf_name(args)
    target = REGRESSIONS_DIR / leaf
    if target.exists() and not args.force:
        print(f"error: {target} already exists (pass --force to overwrite)", file=sys.stderr)
        return 2

    _scaffold(target, bundle, incident, issue=args.issue, uuid=args.uuid, leaf=leaf)
    _print_next_steps(target)
    return 0


def _parse_args(argv: list[str] | None) -> _Args:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--uuid", required=True, help="diagnostic bundle UUID (full or unambiguous prefix)"
    )
    parser.add_argument(
        "--issue", type=int, default=None, help="GitHub issue number this bug is tracked under"
    )
    parser.add_argument("--slug", default=None, help="override the dataset directory leaf name")
    parser.add_argument(
        "--force", action="store_true", help="overwrite an existing target directory"
    )
    ns = parser.parse_args(argv)
    return _Args(uuid=ns.uuid, issue=ns.issue, slug=ns.slug, force=ns.force)


def _leaf_name(args: _Args) -> str:
    if args.slug:
        return args.slug
    if args.issue is not None:
        return str(args.issue)
    return f"diag-{args.uuid.replace('-', '')[:8]}"


def _read_incident(bundle: Path) -> dict[str, Any]:
    try:
        parsed: Any = json.loads((bundle / "incident.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"warning: could not read {bundle / 'incident.json'}: {exc}", file=sys.stderr)
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _scaffold(
    target: Path,
    bundle: Path,
    incident: dict[str, Any],
    *,
    issue: int | None,
    uuid: str,
    leaf: str,
) -> None:
    (target / "frames").mkdir(parents=True, exist_ok=True)
    (target / "provenance").mkdir(parents=True, exist_ok=True)

    copied = _copy_frames(bundle / "frames", target / "frames")
    if not copied:
        print(f"warning: no .fits frames found under {bundle / 'frames'}", file=sys.stderr)

    for name in ("incident.json", "application.log"):
        source = bundle / name
        if source.is_file():
            shutil.copy2(source, target / "provenance" / name)

    skeleton = _expected_skeleton(incident, issue=issue, uuid=uuid, frame_names=copied)
    (target / "expected.json").write_text(
        json.dumps(skeleton, indent=2) + "\n", encoding="utf-8"
    )

    (target / "README.md").write_text(
        _README_TEMPLATE.format(
            leaf=leaf,
            uuid=uuid,
            git_commit=incident.get("git_commit") or "TODO",
            user_report=incident.get("reason") or "TODO",
            issue_line=f"#{issue}" if issue is not None else "none — keyed by diagnostic UUID",
            optical_setup=_optical_setup(incident),
        ),
        encoding="utf-8",
    )


def _copy_frames(source_dir: Path, dest_dir: Path) -> list[str]:
    if not source_dir.is_dir():
        return []
    names: list[str] = []
    for fits_path in sorted(source_dir.glob("*.fits")):
        shutil.copy2(fits_path, dest_dir / fits_path.name)
        names.append(fits_path.name)
    return names


def _expected_skeleton(
    incident: dict[str, Any], *, issue: int | None, uuid: str, frame_names: list[str]
) -> dict[str, Any]:
    before = f"frames/{frame_names[0]}" if frame_names else "frames/TODO_before.fits"
    after = f"frames/{frame_names[1]}" if len(frame_names) > 1 else "frames/TODO_after.fits"
    return {
        "issue": issue,
        "diagnostic_uuids": [str(incident.get("uuid") or uuid)],
        "git_commit_at_capture": incident.get("git_commit") or "TODO",
        "user_report": incident.get("reason") or "TODO",
        "optical_setup": _optical_setup(incident),
        "boundary": "TODO",
        "cases": [
            {
                "name": "TODO_rename_this_case",
                "inputs": {"before": before, "after": after},
                "expect": {"match": False},
                "tolerances": {},
                "rationale": (
                    "TODO: independently derive — DO NOT copy the implementation's output"
                ),
            }
        ],
    }


def _optical_setup(incident: dict[str, Any]) -> str:
    context = incident.get("context")
    if not isinstance(context, dict):
        return "TODO"
    parts: list[str] = []
    for side in ("left", "right"):
        label = _camera_label(context.get(side))
        if label:
            parts.append(f"{side}: {label}")
    return "; ".join(parts) if parts else "TODO"


def _camera_label(node: object) -> str | None:
    if not isinstance(node, dict):
        return None
    for key in ("model", "name", "descriptor", "camera", "id"):
        value = node.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _print_next_steps(target: Path) -> None:
    try:
        rel: Path = target.relative_to(REPO_ROOT)
    except ValueError:
        rel = target
    print(f"scaffolded {rel}/")
    print("next:")
    print(f"  1. point each case's inputs at the real frame pair in {rel}/expected.json")
    print("  2. set 'boundary' (add a runner to tests/regressions/_loader.py BOUNDARIES if new)")
    print("  3. author each case's 'expect' + 'rationale' from an INDEPENDENT source")
    print(f"  4. fill the TODO sections in {rel}/README.md")
    print("  5. run: pytest tests/regressions")


if __name__ == "__main__":
    raise SystemExit(main())
