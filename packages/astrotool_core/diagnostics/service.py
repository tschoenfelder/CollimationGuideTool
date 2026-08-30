"""DiagnosticService — lightweight, local, UUID-identified diagnostic bundles.

See GitHub issue #10. Deliberately a plain directory/file mechanism (no
database, no remote telemetry): each incident becomes one
self-contained directory under ``diagnostics_dir`` named by its UUID,
containing structured metadata, a bounded recent-log tail, and any
frames the caller supplies.

One ``DiagnosticService`` instance is meant to be shared per app between
two capture paths that must produce the exact same bundle format:

- automatic capture at the app's unhandled-exception boundary
  (``sys.excepthook`` — see each app's ``main.py``);
- the manual "Capture diagnostics" UI action (see each app's
  ``MainWindow``).

Both call :meth:`capture_exception` / :meth:`capture_manual`, which are
thin wrappers around the shared, best-effort :meth:`_capture`. A
context/frame *provider* callback can be registered once (typically by
the UI, which knows the latest measurement/state and holds a small
recent-frame buffer) so automatic capture — which by definition has no
call-site context of its own — still gets a useful snapshot.

Every public capture method is best-effort: a failure while capturing
diagnostics is logged and swallowed rather than raised, so it can never
itself crash the app or mask the original failure it was trying to
record (issue #10's "must not cause a secondary application crash").
"""

from __future__ import annotations

import importlib.metadata
import itertools
import json
import logging
import shutil
import subprocess
import time
import traceback
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any

import numpy as np

from astrotool_core.frames.frame import Frame

_log = logging.getLogger(__name__)

#: Default bundle location, per the architecture's convention of keeping
#: hardware/session state outside the git-managed repo.
DEFAULT_DIAGNOSTICS_DIR = Path.home() / ".CollimationGuideTool" / "diagnostics"
DEFAULT_MAX_BUNDLES = 20
#: Retention window (2026-08-29 project decision — see issue #10).
DEFAULT_MAX_AGE_DAYS = 7.0

_SENSITIVE_KEY_MARKERS = ("password", "secret", "token", "apikey", "api_key", "credential")
_REDACTED = "***REDACTED***"


@dataclass(frozen=True)
class DiagnosticBundle:
    """A successfully written diagnostic bundle."""

    incident_id: str
    path: Path


def _is_sensitive_key(key: object) -> bool:
    text = str(key).lower()
    return any(marker in text for marker in _SENSITIVE_KEY_MARKERS)


def _redact(value: Any) -> Any:  # noqa: ANN401 — recursive over arbitrary domain objects
    """Replace values whose dict key looks sensitive, recursively."""
    if isinstance(value, dict):
        return {
            key: (_REDACTED if _is_sensitive_key(key) else _redact(val))
            for key, val in value.items()
        }
    if isinstance(value, (list, tuple, set)):
        return [_redact(item) for item in value]
    return value


def _jsonable(value: Any) -> Any:  # noqa: ANN401 — recursive over arbitrary domain objects
    """Best-effort conversion of arbitrary domain objects into JSON-safe values.

    Handles the shapes this codebase actually produces: dataclasses (most
    measurement/recommendation/status types), enums (``TurnDirection``,
    ``AdjustmentSize``, ...), NamedTuples with ``_asdict`` (``Point2D``),
    numpy scalars/arrays, and plain containers. Anything else falls back
    to ``str()`` rather than failing the whole capture.
    """
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, Enum):
        return value.name
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, np.ndarray):
        # Never inline raw pixel data into JSON — frames go through
        # capture's separate `frames/` handling instead.
        return {"shape": list(value.shape), "dtype": str(value.dtype)}
    if isinstance(value, tuple) and hasattr(value, "_asdict"):
        return _jsonable(value._asdict())
    if isinstance(value, dict):
        return {str(key): _jsonable(val) for key, val in value.items()}
    if isinstance(value, list | tuple | set):
        return [_jsonable(item) for item in value]
    dataclass_fields = getattr(value, "__dataclass_fields__", None)
    if dataclass_fields is not None:
        return {name: _jsonable(getattr(value, name)) for name in dataclass_fields}
    return str(value)


def _exception_payload(exc: BaseException | None) -> dict[str, Any] | None:
    if exc is None:
        return None
    return {
        "type": type(exc).__name__,
        "message": str(exc),
        "traceback": "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
    }


def _detect_version() -> str | None:
    try:
        return importlib.metadata.version("astro-tools")
    except importlib.metadata.PackageNotFoundError:
        return None


def _detect_git_commit() -> str | None:
    """Best-effort short git commit hash of the running checkout.

    This project doesn't bump ``[project.version]`` per commit — every
    bundle otherwise reports the same static "0.1.0" regardless of which
    fix was actually deployed when it was captured, making "was this
    already fixed?" unanswerable from the bundle alone (see the incident
    that prompted this: a bug reported again minutes after a fix had been
    pushed turned out to be a not-yet-restarted process still running the
    old code — obvious in hindsight, invisible from the bundle at the
    time). An editable install (``pip install -e .``, this project's only
    supported install path — see install.md) means the running package's
    own files live inside the git working tree, so `git rev-parse` run
    from there reports exactly what's checked out. Returns ``None`` on
    any failure (git not installed, not actually a git checkout, etc.) —
    this is a diagnostics nicety, never worth failing a capture over.
    """
    repo_root = Path(__file__).resolve().parents[3]
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short=12", "HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=2.0,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    commit = result.stdout.strip()
    return commit or None


def find_bundle(
    incident_id: str, *, diagnostics_dir: Path | str = DEFAULT_DIAGNOSTICS_DIR
) -> Path | None:
    """Resolve a UUID (full, or an unambiguous prefix) to its bundle directory.

    Lets an authorized local developer/agent go straight from an incident
    ID mentioned in a bug report to its evidence, without searching
    unrelated log files by hand (issue #10's "agent/debugging use case").
    Returns ``None`` when there's no match, or the prefix is ambiguous.
    """
    root = Path(diagnostics_dir)
    exact = root / incident_id
    if exact.is_dir():
        return exact
    if not root.is_dir():
        return None
    matches = [p for p in root.iterdir() if p.is_dir() and p.name.startswith(incident_id)]
    if len(matches) == 1:
        return matches[0]
    return None


class DiagnosticService:
    """Creates and prunes local diagnostic bundles. See module docstring."""

    def __init__(
        self,
        *,
        app_name: str,
        diagnostics_dir: Path | str = DEFAULT_DIAGNOSTICS_DIR,
        max_bundles: int = DEFAULT_MAX_BUNDLES,
        max_age_days: float = DEFAULT_MAX_AGE_DAYS,
        version: str | None = None,
        git_commit: str | None = None,
        recent_logs: Callable[[], list[str]] | None = None,
    ) -> None:
        self._app_name = app_name
        self._diagnostics_dir = Path(diagnostics_dir)
        self._max_bundles = max_bundles
        self._max_age_days = max_age_days
        self._version = version if version is not None else _detect_version()
        self._git_commit = git_commit if git_commit is not None else _detect_git_commit()
        self._recent_logs = recent_logs
        self._context_provider: Callable[[], dict[str, Any]] | None = None
        self._frame_provider: Callable[[], Sequence[Frame]] | None = None
        self._image_provider: Callable[[], dict[str, bytes]] | None = None
        # Monotonic per-instance tie-breaker for retention ordering: wall-clock
        # timestamps alone can collide at Windows' clock resolution when
        # several bundles are captured in a tight loop (e.g. tests).
        self._sequence = itertools.count()

    def set_context_provider(self, provider: Callable[[], dict[str, Any]] | None) -> None:
        """Register a callback the service falls back to for app state.

        Lets the UI supply "what was happening" for automatic captures
        (which have no call-site context of their own) without every
        capture call needing to pass it explicitly.
        """
        self._context_provider = provider

    def set_frame_provider(self, provider: Callable[[], Sequence[Frame]] | None) -> None:
        """Register a callback returning the current small recent-frame buffer.

        The service does not own frame capture/retention itself (issue
        #10's "small bounded in-memory/recent-frame buffer" is the
        caller's responsibility) — it only persists what it's given.
        """
        self._frame_provider = provider

    def set_image_provider(self, provider: Callable[[], dict[str, bytes]] | None) -> None:
        """Register a callback returning named image byte blobs (PNG,
        typically) to save alongside a captured bundle's raw frames.

        Distinct from the frame provider: a raw ``Frame`` is unstretched
        sensor data with no demosaicing, measurement overlay, or FOV
        overlay applied — none of what a report like "wrong position
        picked" needs to actually see. This is deliberately UI-agnostic
        (plain ``bytes`` in, written verbatim to a file) so this core
        module never needs to import a UI toolkit; the caller (typically
        a PySide6 window) does the pixmap-to-PNG-bytes conversion itself.
        """
        self._image_provider = provider

    def capture_exception(
        self,
        exc: BaseException,
        *,
        context: dict[str, Any] | None = None,
        frames: Sequence[Frame] | None = None,
        images: dict[str, bytes] | None = None,
    ) -> DiagnosticBundle | None:
        """Capture a bundle for *exc*. Never raises — see module docstring."""
        resolved_context = (
            context if context is not None else self._safe_provider(self._context_provider, {})
        )
        resolved_frames = (
            frames if frames is not None else self._safe_provider(self._frame_provider, [])
        )
        resolved_images = (
            images if images is not None else self._safe_provider(self._image_provider, {})
        )
        return self._capture(
            trigger="exception",
            exception=exc,
            reason=None,
            context=resolved_context,
            frames=resolved_frames,
            images=resolved_images,
        )

    def capture_manual(
        self,
        *,
        reason: str,
        context: dict[str, Any] | None = None,
        frames: Sequence[Frame] | None = None,
        images: dict[str, bytes] | None = None,
    ) -> DiagnosticBundle | None:
        """Capture a bundle for a manual "Capture diagnostics" action.

        *reason* is the user's free-text description of what looked
        wrong. The registered context provider's state (if any) is
        merged in as a base layer, with *context* overriding it.
        """
        base_context = self._safe_provider(self._context_provider, {})
        merged_context = {**base_context, **(context or {})}
        resolved_frames = (
            frames if frames is not None else self._safe_provider(self._frame_provider, [])
        )
        resolved_images = (
            images if images is not None else self._safe_provider(self._image_provider, {})
        )
        return self._capture(
            trigger="manual",
            exception=None,
            reason=reason,
            context=merged_context,
            frames=resolved_frames,
            images=resolved_images,
        )

    def find_bundle(self, incident_id: str) -> Path | None:
        return find_bundle(incident_id, diagnostics_dir=self._diagnostics_dir)

    @staticmethod
    def _safe_provider(
        provider: Callable[[], Any] | None, default: Any  # noqa: ANN401
    ) -> Any:  # noqa: ANN401
        if provider is None:
            return default
        try:
            return provider()
        except Exception:
            _log.warning("Diagnostics: provider callback failed; continuing", exc_info=True)
            return default

    def _capture(
        self,
        *,
        trigger: str,
        exception: BaseException | None,
        reason: str | None,
        context: dict[str, Any],
        frames: Sequence[Frame],
        images: dict[str, bytes] | None = None,
    ) -> DiagnosticBundle | None:
        try:
            incident_id = str(uuid.uuid4())
            bundle_dir = self._diagnostics_dir / incident_id
            bundle_dir.mkdir(parents=True, exist_ok=True)

            incident = {
                "uuid": incident_id,
                "timestamp": datetime.now(UTC).isoformat(),
                "sequence": next(self._sequence),
                "app": self._app_name,
                "version": self._version,
                "git_commit": self._git_commit,
                "trigger": trigger,
                "reason": reason,
                "exception": _exception_payload(exception),
                # jsonable-ify before redacting: dataclass fields (e.g. a
                # hypothetical calibration.password) only become dict keys
                # `_redact` can see once flattened — redacting first would
                # miss anything not already a plain dict/list.
                "context": _redact(_jsonable(context)),
            }
            (bundle_dir / "incident.json").write_text(
                json.dumps(incident, indent=2, default=str), encoding="utf-8"
            )

            log_lines = self._safe_provider(self._recent_logs, [])
            (bundle_dir / "application.log").write_text("\n".join(log_lines), encoding="utf-8")

            self._save_frames(bundle_dir, frames)
            self._save_images(bundle_dir, images or {})

            _log.error("Diagnostic incident %s captured at %s", incident_id, bundle_dir)
            self._prune_old_bundles()
            return DiagnosticBundle(incident_id=incident_id, path=bundle_dir)
        except Exception:
            _log.exception("Diagnostic capture failed (trigger=%s); continuing", trigger)
            return None

    @staticmethod
    def _save_frames(bundle_dir: Path, frames: Sequence[Frame]) -> None:
        if not frames:
            return
        frames_dir = bundle_dir / "frames"
        frames_dir.mkdir(exist_ok=True)
        for index, frame in enumerate(frames):
            try:
                (frames_dir / f"frame_{index}.fits").write_bytes(frame.to_fits_bytes())
            except Exception:
                _log.warning("Diagnostics: failed to save frame %d", index, exc_info=True)

    @staticmethod
    def _save_images(bundle_dir: Path, images: dict[str, bytes]) -> None:
        if not images:
            return
        images_dir = bundle_dir / "images"
        images_dir.mkdir(exist_ok=True)
        for name, data in images.items():
            try:
                (images_dir / name).write_bytes(data)
            except Exception:
                _log.warning("Diagnostics: failed to save image %s", name, exc_info=True)

    @staticmethod
    def _bundle_sort_key(bundle_dir: Path) -> tuple[float, int]:
        """(timestamp, sequence) for ordering bundles oldest-first.

        Reads both from incident.json when available — wall-clock time
        alone can tie at Windows' clock resolution for bundles captured in
        a tight loop, so `sequence` (a per-service-instance counter)
        breaks ties deterministically. Falls back to filesystem mtime for
        a bundle with no readable incident.json.
        """
        try:
            incident = json.loads((bundle_dir / "incident.json").read_text(encoding="utf-8"))
            timestamp = datetime.fromisoformat(incident["timestamp"]).timestamp()
            sequence = int(incident.get("sequence", 0))
            return (timestamp, sequence)
        except Exception:
            return (bundle_dir.stat().st_mtime, 0)

    def _prune_old_bundles(self) -> None:
        try:
            if not self._diagnostics_dir.is_dir():
                return
            cutoff = time.time() - self._max_age_days * 86400
            survivors = []
            for bundle in self._diagnostics_dir.iterdir():
                if not bundle.is_dir():
                    continue
                timestamp, _sequence = self._bundle_sort_key(bundle)
                if timestamp < cutoff:
                    shutil.rmtree(bundle, ignore_errors=True)
                else:
                    survivors.append(bundle)

            survivors.sort(key=self._bundle_sort_key)
            excess = len(survivors) - self._max_bundles
            for bundle in survivors[: max(excess, 0)]:
                shutil.rmtree(bundle, ignore_errors=True)
        except Exception:
            _log.warning("Diagnostics: retention cleanup failed", exc_info=True)
