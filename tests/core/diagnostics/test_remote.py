"""Pi -> dev diagnostic-bundle transfer (issue #6 reliability).

A field failure is captured as a bundle under the Pi's own
``~/.CollimationGuideTool/diagnostics/<uuid>/``; ``scripts/regression_dataset.py``
and ``find_bundle`` only see the *local* dev tree. ``pull_bundle`` closes that
gap so the UUID -> frames -> expected.json -> permanent test workflow starts
from a bug reproduced on the real rig, not only from one reproduced on the dev
box.

The ``ssh`` / ``scp`` calls go through an injected ``runner`` so the resolution
and orchestration logic is tested against real behaviour, not a live Pi.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from astrotool_core.diagnostics.remote import (
    AmbiguousRemoteBundleError,
    RemoteBundleError,
    RemoteBundleNotFoundError,
    pull_bundle,
    resolve_remote_bundle,
)


class TestResolveRemoteBundle:
    _LISTING = [
        "af7d27b7-7216-4421-a993-a63cbd111822",
        "08d264f8-2068-404b-acf3-ed93d777233d",
        "08d264f8-9999-0000-0000-000000000000",
    ]

    def test_exact_name_resolves_to_itself(self) -> None:
        assert (
            resolve_remote_bundle(
                "af7d27b7-7216-4421-a993-a63cbd111822", self._LISTING
            )
            == "af7d27b7-7216-4421-a993-a63cbd111822"
        )

    def test_unambiguous_prefix_resolves_to_the_full_name(self) -> None:
        assert (
            resolve_remote_bundle("af7d27b7", self._LISTING)
            == "af7d27b7-7216-4421-a993-a63cbd111822"
        )

    def test_no_match_raises_not_found(self) -> None:
        with pytest.raises(RemoteBundleNotFoundError):
            resolve_remote_bundle("deadbeef", self._LISTING)

    def test_ambiguous_prefix_raises_ambiguous_with_the_candidates(self) -> None:
        with pytest.raises(AmbiguousRemoteBundleError) as excinfo:
            resolve_remote_bundle("08d264f8", self._LISTING)
        message = str(excinfo.value)
        assert "08d264f8-2068-404b-acf3-ed93d777233d" in message
        assert "08d264f8-9999-0000-0000-000000000000" in message

    def test_ambiguous_is_a_remote_bundle_error(self) -> None:
        assert issubclass(AmbiguousRemoteBundleError, RemoteBundleError)
        assert issubclass(RemoteBundleNotFoundError, RemoteBundleError)


def _completed(
    args: list[str], *, stdout: str = "", stderr: str = "", returncode: int = 0
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args, returncode, stdout=stdout, stderr=stderr)


_UUID = "af7d27b7-7216-4421-a993-a63cbd111822"
_OTHER = "08d264f8-2068-404b-acf3-ed93d777233d"


class _FakeRunner:
    """Answers ``ssh ... ls`` with a canned listing and simulates ``scp`` by
    materialising the destination bundle. Records every argv it was handed."""

    def __init__(self, *, listing: list[str], scp_writes_incident: bool = True) -> None:
        self.listing = listing
        self.scp_writes_incident = scp_writes_incident
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(args)
        if args[0] == "ssh":
            return _completed(args, stdout="\n".join(self.listing) + "\n")
        if args[0] == "scp":
            remote_spec, dest = args[-2], args[-1]
            name = remote_spec.rsplit("/", 1)[-1]
            bundle = Path(dest) / name
            (bundle / "frames").mkdir(parents=True, exist_ok=True)
            if self.scp_writes_incident:
                (bundle / "incident.json").write_text('{"uuid": "x"}', encoding="utf-8")
            return _completed(args)
        raise AssertionError(f"unexpected command: {args}")


class TestPullBundle:
    def test_pulls_a_bundle_absent_locally(self, tmp_path: Path) -> None:
        local = tmp_path / "diagnostics"
        runner = _FakeRunner(listing=[_UUID, _OTHER])

        result = pull_bundle(_UUID, local_dir=local, runner=runner, host="rasppi3")

        assert result == local / _UUID
        assert (result / "incident.json").is_file()
        assert runner.calls[0][0] == "ssh"
        assert runner.calls[0][1] == "rasppi3"
        assert runner.calls[1][0] == "scp"

    def test_unambiguous_prefix_transfers_the_full_uuid(self, tmp_path: Path) -> None:
        local = tmp_path / "diagnostics"
        runner = _FakeRunner(listing=[_UUID, _OTHER])

        result = pull_bundle("af7d27b7", local_dir=local, runner=runner)

        assert result == local / _UUID
        scp_call = next(call for call in runner.calls if call[0] == "scp")
        assert scp_call[-2].endswith(f"/{_UUID}")

    def test_existing_local_bundle_is_not_re_pulled(self, tmp_path: Path) -> None:
        local = tmp_path / "diagnostics"
        (local / _UUID).mkdir(parents=True)
        (local / _UUID / "incident.json").write_text("{}", encoding="utf-8")
        runner = _FakeRunner(listing=[_UUID])

        result = pull_bundle(_UUID, local_dir=local, runner=runner)

        assert result == local / _UUID
        assert runner.calls == []

    def test_force_re_pulls_even_when_present(self, tmp_path: Path) -> None:
        local = tmp_path / "diagnostics"
        (local / _UUID).mkdir(parents=True)
        (local / _UUID / "incident.json").write_text("{}", encoding="utf-8")
        runner = _FakeRunner(listing=[_UUID])

        pull_bundle(_UUID, local_dir=local, runner=runner, force=True)

        assert any(call[0] == "scp" for call in runner.calls)

    def test_ssh_listing_failure_raises_with_stderr(self, tmp_path: Path) -> None:
        def runner(args: list[str]) -> subprocess.CompletedProcess[str]:
            return _completed(
                args, returncode=255, stderr="ssh: connect to host ... timed out"
            )

        with pytest.raises(RemoteBundleError, match="timed out"):
            pull_bundle(_UUID, local_dir=tmp_path / "d", runner=runner)

    def test_no_remote_match_raises_not_found(self, tmp_path: Path) -> None:
        runner = _FakeRunner(listing=[_OTHER])

        with pytest.raises(RemoteBundleNotFoundError):
            pull_bundle(_UUID, local_dir=tmp_path / "d", runner=runner)

    def test_transfer_leaving_no_incident_json_raises(self, tmp_path: Path) -> None:
        runner = _FakeRunner(listing=[_UUID], scp_writes_incident=False)

        with pytest.raises(RemoteBundleError, match="incomplete|incident.json"):
            pull_bundle(_UUID, local_dir=tmp_path / "d", runner=runner)
