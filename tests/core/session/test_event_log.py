import json
from pathlib import Path
from typing import Any

from astrotool_core.session.event_log import EventLogger
from astrotool_core.session.session_context import SessionContext


def _last_log_line(log_path: Path) -> dict[str, Any]:
    lines = log_path.read_text().strip().splitlines()
    # Section log lines are "<prefix> {json}" — the JSON payload starts at the first "{".
    json_part = lines[-1][lines[-1].index("{") :]
    result: dict[str, Any] = json.loads(json_part)
    return result


def test_successful_event_is_logged_as_ok(tmp_path: Path) -> None:
    session = SessionContext(log_dir=str(tmp_path), session_id="eventtest")
    logger = EventLogger(session)

    with logger.event("collimation", "CollimationAdvisor", request_payload={"x": 1}) as ev:
        ev.set_response({"status": "ok"})

    record = _last_log_line(tmp_path / "eventtes" / "collimation.log")
    assert record["status"] == "ok"
    assert record["event_name"] == "CollimationAdvisor"
    assert record["request_payload"] == {"x": 1}
    assert record["response_payload"] == {"status": "ok"}
    session.close()


def test_unhandled_exception_is_logged_as_failed(tmp_path: Path) -> None:
    session = SessionContext(log_dir=str(tmp_path), session_id="eventfail")
    logger = EventLogger(session)

    try:
        with logger.event("guide", "GuideCorrection"):
            raise ValueError("boom")
    except ValueError:
        pass

    record = _last_log_line(tmp_path / "eventfai" / "guide.log")
    assert record["status"] == "failed"
    assert "boom" in record["error_if_any"]
    session.close()


def test_explicit_set_error_marks_failed_without_raising(tmp_path: Path) -> None:
    session = SessionContext(log_dir=str(tmp_path), session_id="explicit")
    logger = EventLogger(session)

    with logger.event("guide", "GuideCorrection") as ev:
        ev.set_error("could not connect")

    record = _last_log_line(tmp_path / "explicit" / "guide.log")
    assert record["status"] == "failed"
    assert record["error_if_any"] == "could not connect"
    session.close()


def test_mark_cancelled_marks_cancelled(tmp_path: Path) -> None:
    session = SessionContext(log_dir=str(tmp_path), session_id="cancelled")
    logger = EventLogger(session)

    with logger.event("guide", "GuideCorrection") as ev:
        ev.mark_cancelled()

    record = _last_log_line(tmp_path / "cancelle" / "guide.log")
    assert record["status"] == "cancelled"
    session.close()


def test_run_id_defaults_to_a_generated_value(tmp_path: Path) -> None:
    session = SessionContext(log_dir=str(tmp_path), session_id="runidgen")
    logger = EventLogger(session)

    with logger.event("guide", "GuideCorrection"):
        pass

    record = _last_log_line(tmp_path / "runidgen" / "guide.log")
    assert record["run_id"]
    session.close()
