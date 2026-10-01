"""Keep the MCP SDK's log text out of the application's logs (T-03, T-25).

The SDK logs whole messages at DEBUG, the Host and Origin header it refused at
WARNING and, at ERROR, an exception whose text can quote a response body. A
level on its logger removes only the first; the others are content too. The
SDK's records therefore stop at one handler, which says that something
happened and nothing about what it held.
"""

import logging

SDK_LOGGER = "mcp"

logger = logging.getLogger(__name__)


class _ContentFreeHandler(logging.Handler):
    """Re-log a WARNING or above as a line of fixed shape: the SDK logger's
    name, the level and the class of the exception the record carried. Never
    the message, its arguments or a traceback."""

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)

    def emit(self, record: logging.LogRecord) -> None:
        exc_type = record.exc_info[0] if record.exc_info else None
        logger.log(
            record.levelno,
            "mcp sdk log: logger=%s level=%s exception=%s",
            record.name,
            record.levelname,
            exc_type.__name__ if exc_type is not None else "none",
        )


def quiet_sdk_logging() -> None:
    """Route the SDK's records to the content-free handler and to nothing else.
    Safe to call again."""
    sdk = logging.getLogger(SDK_LOGGER)
    if not any(isinstance(h, _ContentFreeHandler) for h in sdk.handlers):
        sdk.addHandler(_ContentFreeHandler())
    sdk.propagate = False
