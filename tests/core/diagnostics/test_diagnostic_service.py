from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from astropy.io import fits
from astrotool_core.diagnostics.service import DiagnosticService, find_bundle
from astrotool_core.frames.frame import Frame


def _frame(value: float = 1.0) -> Frame:
    pixels = np.full((4, 4), value, dtype=np.float32)
    return Frame(pixels=pixels, header=fits.Header(), exposure_seconds=0.1)


def _read_incident(bundle_dir: Path) -> dict[str, Any]:
    text = (bundle_dir / "incident.json").read_text(encoding="utf-8")
    incident: dict[str, Any] = json.loads(text)
    return incident


def _backdate_incident(bundle_dir: Path, *, days_ago: float) -> None:
    """Rewrite a bundle's incident.json timestamp, since retention reads
    that (not filesystem mtime) to decide a bundle's age."""
    incident_path = bundle_dir / "incident.json"
    incident = _read_incident(bundle_dir)
    backdated = datetime.fromtimestamp(time.time() - days_ago * 86400, tz=UTC)
    incident["timestamp"] = backdated.isoformat()
    incident_path.write_text(json.dumps(incident), encoding="utf-8")


class _Direction(Enum):
    CW = "cw"
    CCW = "ccw"


@dataclass(frozen=True)
class _Recommendation:
    direction: _Direction
    confidence: float


class TestUuidAndBundleCreation:
    def test_capture_manual_generates_a_uuid_and_returns_a_bundle(self, tmp_path: Path) -> None:
        service = DiagnosticService(app_name="CollimationTool", diagnostics_dir=tmp_path)
        bundle = service.capture_manual(reason="looks wrong")
        assert bundle is not None
        # Bundle id parses as a real UUID and names an existing directory.
        assert uuid.UUID(bundle.incident_id)
        assert bundle.path == tmp_path / bundle.incident_id
        assert bundle.path.is_dir()

    def test_two_captures_get_different_uuids(self, tmp_path: Path) -> None:
        service = DiagnosticService(app_name="CollimationTool", diagnostics_dir=tmp_path)
        first = service.capture_manual(reason="a")
        second = service.capture_manual(reason="b")
        assert first is not None
        assert second is not None
        assert first.incident_id != second.incident_id

    def test_bundle_contains_incident_json_and_application_log(self, tmp_path: Path) -> None:
        service = DiagnosticService(app_name="GuideTool", diagnostics_dir=tmp_path)
        bundle = service.capture_manual(reason="tracking looked odd")
        assert bundle is not None
        assert (bundle.path / "incident.json").is_file()
        assert (bundle.path / "application.log").is_file()

    def test_incident_json_has_expected_top_level_fields(self, tmp_path: Path) -> None:
        service = DiagnosticService(app_name="GuideTool", diagnostics_dir=tmp_path)
        bundle = service.capture_manual(reason="star not recentered")
        assert bundle is not None
        incident = _read_incident(bundle.path)
        assert incident["uuid"] == bundle.incident_id
        assert incident["app"] == "GuideTool"
        assert incident["trigger"] == "manual"
        assert incident["reason"] == "star not recentered"
        assert incident["exception"] is None
        assert "timestamp" in incident
        assert "version" in incident
        assert "git_commit" in incident


class TestVersionAndGitCommit:
    """`git_commit` exists precisely because the static package version
    doesn't change per commit in this project's dev workflow — a bundle
    with only "0.1.0" can't say whether a given fix was actually deployed
    yet. See _detect_git_commit()'s docstring for the incident that
    prompted this."""

    def test_explicit_version_and_git_commit_are_used_verbatim(self, tmp_path: Path) -> None:
        service = DiagnosticService(
            app_name="CollimationTool",
            diagnostics_dir=tmp_path,
            version="9.9.9",
            git_commit="deadbeef0000",
        )
        bundle = service.capture_manual(reason="pin the build info")
        assert bundle is not None
        incident = _read_incident(bundle.path)
        assert incident["version"] == "9.9.9"
        assert incident["git_commit"] == "deadbeef0000"

    def test_git_commit_is_auto_detected_from_this_real_checkout(self, tmp_path: Path) -> None:
        """No explicit git_commit given — this test itself runs from a
        real git checkout, so auto-detection should find something."""
        service = DiagnosticService(app_name="CollimationTool", diagnostics_dir=tmp_path)
        bundle = service.capture_manual(reason="auto-detect")
        assert bundle is not None
        incident = _read_incident(bundle.path)
        assert isinstance(incident["git_commit"], str)
        assert len(incident["git_commit"]) > 0

    def test_git_commit_is_none_when_detection_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import astrotool_core.diagnostics.service as service_module

        monkeypatch.setattr(service_module, "_detect_git_commit", lambda: None)
        service = DiagnosticService(app_name="CollimationTool", diagnostics_dir=tmp_path)
        bundle = service.capture_manual(reason="detection unavailable")
        assert bundle is not None
        incident = _read_incident(bundle.path)
        assert incident["git_commit"] is None


class TestExceptionCapture:
    def test_capture_exception_preserves_type_message_and_traceback(
        self, tmp_path: Path
    ) -> None:
        service = DiagnosticService(app_name="CollimationTool", diagnostics_dir=tmp_path)
        try:
            raise ValueError("boom")
        except ValueError as exc:
            bundle = service.capture_exception(exc)
        assert bundle is not None
        incident = _read_incident(bundle.path)
        assert incident["trigger"] == "exception"
        assert incident["exception"]["type"] == "ValueError"
        assert incident["exception"]["message"] == "boom"
        assert "ValueError: boom" in incident["exception"]["traceback"]

    def test_capture_exception_uses_registered_context_and_frame_providers(
        self, tmp_path: Path
    ) -> None:
        service = DiagnosticService(app_name="CollimationTool", diagnostics_dir=tmp_path)
        service.set_context_provider(lambda: {"donut_error_px": 3.5})
        service.set_frame_provider(lambda: [_frame()])
        bundle = service.capture_exception(ValueError("boom"))
        assert bundle is not None
        incident = _read_incident(bundle.path)
        assert incident["context"]["donut_error_px"] == 3.5
        assert (bundle.path / "frames" / "frame_0.fits").is_file()

    def test_a_broken_context_provider_does_not_prevent_capture(self, tmp_path: Path) -> None:
        service = DiagnosticService(app_name="CollimationTool", diagnostics_dir=tmp_path)

        def _broken() -> dict[str, Any]:
            raise RuntimeError("provider exploded")

        service.set_context_provider(_broken)
        bundle = service.capture_exception(ValueError("boom"))
        assert bundle is not None
        incident = _read_incident(bundle.path)
        assert incident["context"] == {}


class TestManualCapture:
    def test_manual_and_automatic_share_the_same_bundle_shape(self, tmp_path: Path) -> None:
        service = DiagnosticService(app_name="CollimationTool", diagnostics_dir=tmp_path)
        manual = service.capture_manual(reason="wrong result")
        exception = service.capture_exception(ValueError("boom"))
        assert manual is not None
        assert exception is not None
        manual_files = {p.name for p in manual.path.iterdir()}
        exception_files = {p.name for p in exception.path.iterdir()}
        assert manual_files == exception_files

    def test_explicit_context_overrides_the_registered_provider(self, tmp_path: Path) -> None:
        service = DiagnosticService(app_name="GuideTool", diagnostics_dir=tmp_path)
        service.set_context_provider(lambda: {"rms_px": 1.0, "state": "running"})
        bundle = service.capture_manual(reason="drift", context={"rms_px": 9.9})
        assert bundle is not None
        incident = _read_incident(bundle.path)
        # Explicit context wins for the overlapping key, provider fills the rest.
        assert incident["context"]["rms_px"] == 9.9
        assert incident["context"]["state"] == "running"

    def test_manual_capture_saves_explicitly_passed_frames(self, tmp_path: Path) -> None:
        service = DiagnosticService(app_name="CollimationTool", diagnostics_dir=tmp_path)
        bundle = service.capture_manual(reason="bad donut", frames=[_frame(1.0), _frame(2.0)])
        assert bundle is not None
        assert (bundle.path / "frames" / "frame_0.fits").is_file()
        assert (bundle.path / "frames" / "frame_1.fits").is_file()


class TestImageCapture:
    """See the real-world question this was added for: "Are you stored
    the frames display and the calibration result as well?" — the
    answer was no, only raw unstretched sensor data (`frames/`) and
    per-camera settings; this adds a place for whatever's actually
    *displayed* (stretched, demosaiced, with overlays drawn)."""

    def test_manual_capture_saves_explicitly_passed_images(self, tmp_path: Path) -> None:
        service = DiagnosticService(app_name="CollimationTool", diagnostics_dir=tmp_path)
        bundle = service.capture_manual(
            reason="wrong overlay", images={"left_display.png": b"fake-png-bytes"}
        )
        assert bundle is not None
        saved = bundle.path / "images" / "left_display.png"
        assert saved.is_file()
        assert saved.read_bytes() == b"fake-png-bytes"

    def test_capture_exception_uses_the_registered_image_provider(self, tmp_path: Path) -> None:
        service = DiagnosticService(app_name="CollimationTool", diagnostics_dir=tmp_path)
        service.set_image_provider(lambda: {"right_display.png": b"guide-panel-bytes"})
        bundle = service.capture_exception(ValueError("boom"))
        assert bundle is not None
        saved = bundle.path / "images" / "right_display.png"
        assert saved.is_file()
        assert saved.read_bytes() == b"guide-panel-bytes"

    def test_explicit_images_override_the_registered_provider(self, tmp_path: Path) -> None:
        service = DiagnosticService(app_name="CollimationTool", diagnostics_dir=tmp_path)
        service.set_image_provider(lambda: {"left_display.png": b"from-provider"})
        bundle = service.capture_manual(
            reason="explicit wins", images={"left_display.png": b"from-call-site"}
        )
        assert bundle is not None
        saved = bundle.path / "images" / "left_display.png"
        assert saved.read_bytes() == b"from-call-site"

    def test_a_broken_image_provider_does_not_prevent_capture(self, tmp_path: Path) -> None:
        service = DiagnosticService(app_name="CollimationTool", diagnostics_dir=tmp_path)

        def _broken() -> dict[str, bytes]:
            raise RuntimeError("provider exploded")

        service.set_image_provider(_broken)
        bundle = service.capture_manual(reason="should not crash")
        assert bundle is not None
        assert not (bundle.path / "images").exists()

    def test_no_images_means_no_images_directory(self, tmp_path: Path) -> None:
        service = DiagnosticService(app_name="CollimationTool", diagnostics_dir=tmp_path)
        bundle = service.capture_manual(reason="nothing to show")
        assert bundle is not None
        assert not (bundle.path / "images").exists()


class TestContextSerialization:
    def test_dataclasses_enums_and_numpy_values_serialize_without_error(
        self, tmp_path: Path
    ) -> None:
        service = DiagnosticService(app_name="CollimationTool", diagnostics_dir=tmp_path)
        context = {
            "recommendation": _Recommendation(direction=_Direction.CW, confidence=0.8),
            "gain": np.int32(120),
            "frame_shape": np.zeros((240, 240), dtype=np.float32),
        }
        bundle = service.capture_manual(reason="check serialization", context=context)
        assert bundle is not None
        incident = _read_incident(bundle.path)
        assert incident["context"]["recommendation"]["direction"] == "CW"
        assert incident["context"]["recommendation"]["confidence"] == 0.8
        assert incident["context"]["gain"] == 120
        assert incident["context"]["frame_shape"]["shape"] == [240, 240]

    def test_sensitive_keys_nested_inside_a_dataclass_are_also_redacted(
        self, tmp_path: Path
    ) -> None:
        @dataclass(frozen=True)
        class _MountCalibration:
            password: str
            baud_rate: int

        service = DiagnosticService(app_name="GuideTool", diagnostics_dir=tmp_path)
        bundle = service.capture_manual(
            reason="check nested redaction",
            context={"mount": _MountCalibration(password="hunter2", baud_rate=9600)},
        )
        assert bundle is not None
        raw_text = (bundle.path / "incident.json").read_text(encoding="utf-8")
        assert "hunter2" not in raw_text
        incident = _read_incident(bundle.path)
        assert incident["context"]["mount"]["baud_rate"] == 9600

    def test_sensitive_looking_keys_are_redacted(self, tmp_path: Path) -> None:
        service = DiagnosticService(app_name="CollimationTool", diagnostics_dir=tmp_path)
        bundle = service.capture_manual(
            reason="check redaction",
            context={"api_key": "sk-super-secret", "mount_password": "hunter2", "gain": 50},
        )
        assert bundle is not None
        raw_text = (bundle.path / "incident.json").read_text(encoding="utf-8")
        assert "sk-super-secret" not in raw_text
        assert "hunter2" not in raw_text
        incident = _read_incident(bundle.path)
        assert incident["context"]["gain"] == 50


class TestMissingOrUnavailableData:
    def test_capture_with_no_context_or_frames_still_produces_a_bundle(
        self, tmp_path: Path
    ) -> None:
        service = DiagnosticService(app_name="GuideTool", diagnostics_dir=tmp_path)
        bundle = service.capture_manual(reason="no extra data available")
        assert bundle is not None
        incident = _read_incident(bundle.path)
        assert incident["context"] == {}
        assert not (bundle.path / "frames").exists()

    def test_capture_failure_itself_is_best_effort_and_returns_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        service = DiagnosticService(app_name="GuideTool", diagnostics_dir=tmp_path)

        def _broken_mkdir(self: Path, *args: object, **kwargs: object) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(Path, "mkdir", _broken_mkdir)
        bundle = service.capture_manual(reason="should not crash")
        assert bundle is None


class TestRetention:
    def test_oldest_bundles_beyond_max_bundles_are_pruned(self, tmp_path: Path) -> None:
        service = DiagnosticService(
            app_name="CollimationTool", diagnostics_dir=tmp_path, max_bundles=2
        )
        first = service.capture_manual(reason="1")
        second = service.capture_manual(reason="2")
        third = service.capture_manual(reason="3")
        assert first is not None
        assert second is not None
        assert third is not None
        remaining = {p.name for p in tmp_path.iterdir()}
        assert first.incident_id not in remaining
        assert second.incident_id in remaining
        assert third.incident_id in remaining

    def test_bundles_older_than_max_age_are_pruned(self, tmp_path: Path) -> None:
        service = DiagnosticService(
            app_name="CollimationTool", diagnostics_dir=tmp_path, max_age_days=7, max_bundles=100
        )
        old = service.capture_manual(reason="old")
        assert old is not None
        _backdate_incident(old.path, days_ago=8)

        new = service.capture_manual(reason="new")
        assert new is not None
        remaining = {p.name for p in tmp_path.iterdir()}
        assert old.incident_id not in remaining
        assert new.incident_id in remaining

    def test_recent_bundles_within_retention_are_kept(self, tmp_path: Path) -> None:
        service = DiagnosticService(
            app_name="CollimationTool", diagnostics_dir=tmp_path, max_age_days=7, max_bundles=100
        )
        bundle = service.capture_manual(reason="fresh")
        assert bundle is not None
        assert bundle.path.is_dir()


class TestFindBundle:
    def test_exact_uuid_resolves_to_its_directory(self, tmp_path: Path) -> None:
        service = DiagnosticService(app_name="CollimationTool", diagnostics_dir=tmp_path)
        bundle = service.capture_manual(reason="findable")
        assert bundle is not None
        found = find_bundle(bundle.incident_id, diagnostics_dir=tmp_path)
        assert found == bundle.path

    def test_unambiguous_prefix_resolves(self, tmp_path: Path) -> None:
        service = DiagnosticService(app_name="CollimationTool", diagnostics_dir=tmp_path)
        bundle = service.capture_manual(reason="findable by prefix")
        assert bundle is not None
        prefix = bundle.incident_id[:8]
        found = find_bundle(prefix, diagnostics_dir=tmp_path)
        assert found == bundle.path

    def test_unknown_uuid_returns_none(self, tmp_path: Path) -> None:
        assert find_bundle(str(uuid.uuid4()), diagnostics_dir=tmp_path) is None

    def test_missing_diagnostics_dir_returns_none(self, tmp_path: Path) -> None:
        missing = tmp_path / "does-not-exist"
        assert find_bundle("anything", diagnostics_dir=missing) is None

    def test_service_find_bundle_uses_its_own_directory(self, tmp_path: Path) -> None:
        service = DiagnosticService(app_name="CollimationTool", diagnostics_dir=tmp_path)
        bundle = service.capture_manual(reason="via service")
        assert bundle is not None
        assert service.find_bundle(bundle.incident_id) == bundle.path
