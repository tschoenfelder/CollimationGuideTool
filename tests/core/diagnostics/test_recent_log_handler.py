import logging

from astrotool_core.diagnostics.recent_log_handler import RecentLogHandler


def _make_record(message: str) -> logging.LogRecord:
    return logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=(),
        exc_info=None,
    )


def test_lines_starts_empty() -> None:
    handler = RecentLogHandler(capacity=5)
    assert handler.lines() == []


def test_emit_appends_formatted_lines() -> None:
    handler = RecentLogHandler(capacity=5)
    handler.emit(_make_record("hello"))
    lines = handler.lines()
    assert len(lines) == 1
    assert "hello" in lines[0]


def test_capacity_bounds_the_buffer() -> None:
    handler = RecentLogHandler(capacity=3)
    for i in range(10):
        handler.emit(_make_record(f"line-{i}"))
    lines = handler.lines()
    assert len(lines) == 3
    # Oldest entries dropped; only the most recent 3 remain.
    assert "line-9" in lines[-1]
    assert "line-0" not in "".join(lines)


def test_attached_to_a_real_logger_captures_its_records() -> None:
    handler = RecentLogHandler(capacity=5)
    logger = logging.getLogger("test.recent_log_handler")
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        logger.info("captured via real logger")
    finally:
        logger.removeHandler(handler)
    assert any("captured via real logger" in line for line in handler.lines())


def test_emit_never_raises_on_a_bad_record() -> None:
    handler = RecentLogHandler(capacity=5)
    # %s with no args raises inside logging's formatting machinery.
    bad_record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="%s %s",
        args=("only-one",),
        exc_info=None,
    )
    handler.emit(bad_record)  # must not raise
