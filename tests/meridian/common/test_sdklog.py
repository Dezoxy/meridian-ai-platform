"""The MCP SDK's logging reaches the application's handlers content-free only.

The SDK logs a refused Host header at WARNING and, at ERROR, an exception whose
text can quote a response body; a logger level does not remove those lines.
"""

import logging
from collections.abc import Iterator

import pytest

from meridian.platform.common.sdklog import quiet_sdk_logging

SDK_LOGGER = "mcp"
SDK_CHILD = "mcp.server.transport_security"
CANARY = "CANARY-7d1f-holder-name"


@pytest.fixture
def pristine() -> Iterator[logging.Logger]:
    """The ``mcp`` logger as the test found it, restored afterwards."""
    sdk = logging.getLogger(SDK_LOGGER)
    handlers, propagate, level = list(sdk.handlers), sdk.propagate, sdk.level
    sdk.handlers.clear()
    sdk.propagate = True
    try:
        yield sdk
    finally:
        sdk.handlers[:] = handlers
        sdk.propagate = propagate
        sdk.setLevel(level)


def kept(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name.startswith("meridian")]


def test_it_is_idempotent(pristine: logging.Logger) -> None:
    quiet_sdk_logging()
    quiet_sdk_logging()

    assert len(pristine.handlers) == 1
    assert pristine.propagate is False


@pytest.mark.usefixtures("pristine")
def test_no_sdk_record_reaches_the_root_logger(
    caplog: pytest.LogCaptureFixture,
) -> None:
    quiet_sdk_logging()
    caplog.set_level(logging.DEBUG)

    logging.getLogger(SDK_CHILD).warning("Invalid Host header: %s", CANARY)
    logging.getLogger(SDK_CHILD).debug("message %s", CANARY)

    assert not [r for r in caplog.records if r.name.startswith(SDK_LOGGER)]
    assert CANARY not in caplog.text


@pytest.mark.usefixtures("pristine")
def test_a_warning_is_re_logged_with_the_logger_name_and_the_level_only(
    caplog: pytest.LogCaptureFixture,
) -> None:
    quiet_sdk_logging()
    caplog.set_level(logging.DEBUG)

    logging.getLogger(SDK_CHILD).warning("Invalid Host header: %s", CANARY)

    (record,) = kept(caplog)
    assert record.levelno == logging.WARNING
    assert SDK_CHILD in record.getMessage()
    assert "WARNING" in record.getMessage()
    assert CANARY not in record.getMessage()
    assert CANARY not in repr(record.__dict__)


@pytest.mark.usefixtures("pristine")
def test_an_error_with_an_exception_names_its_class_and_nothing_else(
    caplog: pytest.LogCaptureFixture,
) -> None:
    quiet_sdk_logging()
    caplog.set_level(logging.DEBUG)

    try:
        raise ValueError(f"response body {CANARY}")
    except ValueError:
        logging.getLogger("mcp.client.streamable_http").exception("failed: %s", CANARY)

    (record,) = kept(caplog)
    assert record.levelno == logging.ERROR
    assert "ValueError" in record.getMessage()
    assert "mcp.client.streamable_http" in record.getMessage()
    assert record.exc_info is None
    assert CANARY not in caplog.text
    assert CANARY not in repr(record.__dict__)


@pytest.mark.usefixtures("pristine")
@pytest.mark.parametrize("level", [logging.DEBUG, logging.INFO])
def test_a_record_below_warning_is_dropped(
    caplog: pytest.LogCaptureFixture, level: int
) -> None:
    quiet_sdk_logging()
    caplog.set_level(logging.DEBUG)

    logging.getLogger(SDK_CHILD).log(level, "message %s", CANARY)

    assert kept(caplog) == []
