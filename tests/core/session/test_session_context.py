from pathlib import Path

from astrotool_core.session.session_context import SessionContext


def test_session_id_is_generated_when_not_given() -> None:
    session = SessionContext()
    assert len(session.session_id) > 0
    assert session.session_slug == session.session_id[:8]


def test_explicit_session_id_is_kept() -> None:
    session = SessionContext(session_id="abcdefgh-1234")
    assert session.session_id == "abcdefgh-1234"
    assert session.session_slug == "abcdefgh"


def test_get_logger_returns_the_same_adapter_for_the_same_section() -> None:
    session = SessionContext()
    first = session.get_logger("camera")
    second = session.get_logger("camera")
    assert first is second


def test_get_logger_for_different_sections_returns_different_adapters() -> None:
    session = SessionContext()
    camera_logger = session.get_logger("camera")
    mount_logger = session.get_logger("mount")
    assert camera_logger is not mount_logger


def test_no_log_dir_means_no_file_paths() -> None:
    session = SessionContext()
    session.get_logger("camera")
    assert session.get_paths() == {}


def test_log_dir_creates_a_file_path_per_section(tmp_path: Path) -> None:
    session = SessionContext(log_dir=str(tmp_path), session_id="12345678-full")
    session.get_logger("camera").info("hello")
    paths = session.get_paths()
    assert "camera" in paths
    log_path = tmp_path / "12345678" / "camera.log"
    assert log_path.exists()
    assert "hello" in log_path.read_text()
    session.close()


def test_close_removes_file_handlers(tmp_path: Path) -> None:
    session = SessionContext(log_dir=str(tmp_path), session_id="closeme")
    logger = session.get_logger("camera")
    assert len(logger.logger.handlers) == 1
    session.close()
    assert len(logger.logger.handlers) == 0
