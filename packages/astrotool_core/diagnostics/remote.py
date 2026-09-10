"""Pull a diagnostic bundle from the Pi to the local dev tree (issue #6).

A reproduced field failure is captured on the rig itself, under the Pi's
``~/.CollimationGuideTool/diagnostics/<uuid>/``. ``find_bundle`` and
``scripts/regression_dataset.py`` only look at the *local* dev tree, so the
"UUID -> saved frames -> expected result -> permanent regression test"
workflow could only start from a bug reproduced on the dev box. ``pull_bundle``
copies the real bundle over first, by UUID (full or unambiguous prefix), so the
workflow starts from the frames that actually exposed the defect.

Transport is plain ``ssh`` + ``scp`` against a host from the user's own
``~/.ssh/config`` (default ``rasppi3`` -> ``astro@rasppiserver3.fritz.box``).
The subprocess calls go through an injectable ``runner`` so the resolution and
orchestration logic is unit-tested without a live Pi.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Iterable
from pathlib import Path

from astrotool_core.diagnostics.service import DEFAULT_DIAGNOSTICS_DIR, find_bundle

#: SSH host alias for the rig's Pi. ``rasppi3`` is defined in the user's
#: ``~/.ssh/config`` (``HostName rasppiserver3.fritz.box``, ``User astro``);
#: override with ``--host`` when the alias differs.
DEFAULT_PI_HOST = "rasppi3"

#: The Pi's own diagnostics dir. ``~`` is left for the remote shell / scp to
#: expand against ``astro``'s home (``DiagnosticService`` uses the same
#: ``~/.CollimationGuideTool/diagnostics`` convention on every platform).
DEFAULT_REMOTE_DIAGNOSTICS_DIR = "~/.CollimationGuideTool/diagnostics"

#: ``(argv) -> CompletedProcess``. Defaults to a real ``subprocess.run`` with
#: captured text output; injected in tests.
CommandRunner = Callable[[list[str]], "subprocess.CompletedProcess[str]"]


class RemoteBundleError(RuntimeError):
    """A diagnostic bundle could not be pulled from the Pi."""


class RemoteBundleNotFoundError(RemoteBundleError):
    """No remote bundle directory matched the given UUID / prefix."""


class AmbiguousRemoteBundleError(RemoteBundleError):
    """A UUID prefix matched more than one remote bundle directory."""


def resolve_remote_bundle(incident_id: str, remote_names: Iterable[str]) -> str:
    """Resolve *incident_id* against a remote directory listing.

    Mirrors ``find_bundle``: an exact directory name wins outright, otherwise a
    single ``startswith`` match resolves to that full name. Raises
    ``RemoteBundleNotFoundError`` when nothing matches and
    ``AmbiguousRemoteBundleError`` (listing the candidates) when a prefix
    matches several.
    """
    names = [name for name in remote_names if name]
    if incident_id in names:
        return incident_id
    matches = sorted(name for name in names if name.startswith(incident_id))
    if not matches:
        raise RemoteBundleNotFoundError(
            f"no remote diagnostic bundle matches {incident_id!r}"
        )
    if len(matches) > 1:
        raise AmbiguousRemoteBundleError(
            f"{incident_id!r} matches several remote bundles: {', '.join(matches)}"
        )
    return matches[0]


def _default_runner(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True, check=False)


def pull_bundle(
    incident_id: str,
    *,
    host: str = DEFAULT_PI_HOST,
    remote_dir: str = DEFAULT_REMOTE_DIAGNOSTICS_DIR,
    local_dir: Path | str = DEFAULT_DIAGNOSTICS_DIR,
    force: bool = False,
    runner: CommandRunner | None = None,
) -> Path:
    """Copy the ``incident_id`` bundle from *host* into *local_dir*.

    Resolves *incident_id* (full UUID or unambiguous prefix) against the Pi's
    ``remote_dir`` listing, then ``scp -r`` the matching directory down.
    Returns the local bundle path. When a matching bundle is already present
    locally the copy is skipped unless *force* is set. Raises
    ``RemoteBundleError`` (or its ``RemoteBundleNotFoundError`` /
    ``AmbiguousRemoteBundleError`` subclasses) on any failure.
    """
    run = runner or _default_runner
    local_root = Path(local_dir)

    if not force:
        existing = find_bundle(incident_id, diagnostics_dir=local_root)
        if existing is not None:
            return existing

    listing = _list_remote(run, host, remote_dir)
    full_name = resolve_remote_bundle(incident_id, listing)
    target = local_root / full_name
    if target.is_dir() and not force:
        return target

    local_root.mkdir(parents=True, exist_ok=True)
    result = run(
        ["scp", "-r", "-p", f"{host}:{remote_dir}/{full_name}", str(local_root)]
    )
    if result.returncode != 0:
        raise RemoteBundleError(
            f"scp of {full_name} from {host} failed "
            f"(exit {result.returncode}): {result.stderr.strip()}"
        )
    if not (target / "incident.json").is_file():
        raise RemoteBundleError(
            f"transfer of {full_name} left no incident.json under {target} "
            "— the bundle copy is incomplete"
        )
    return target


def _list_remote(run: CommandRunner, host: str, remote_dir: str) -> list[str]:
    result = run(["ssh", host, f"ls -1 {remote_dir}"])
    if result.returncode != 0:
        raise RemoteBundleError(
            f"could not list {remote_dir} on {host} "
            f"(exit {result.returncode}): {result.stderr.strip()}"
        )
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]
