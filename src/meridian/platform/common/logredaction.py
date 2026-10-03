"""Keep personal identifiers out of every log record the process builds (S047,
T-03).

``install_log_redaction`` wraps the logging module's record factory: once a
record is built, its message is formatted, run through the guardrails'
``redact`` and, only when something was found, stored back as the record's
message with no arguments.

Why a record factory and not a filter: a filter on a logger misses the records
of its children, and a filter on handlers misses the handlers added after
start (uvicorn's, the test harness's, the collector's). The factory sees every
record of the process once, whoever handles it.

Not covered: the text of an exception in ``exc_info`` is formatted by the
handler, after the factory has run. The services' error middleware already
keeps exception messages out of logs (T-03).
"""

import logging

from meridian.platform.guardrails import redact

_MARK = "_meridian_log_redaction"


def install_log_redaction() -> None:
    """Install the redacting record factory, once per process. A second call
    does nothing."""
    inner = logging.getLogRecordFactory()
    if getattr(inner, _MARK, False):
        return

    def factory(*args: object, **kwargs: object) -> logging.LogRecord:
        record = inner(*args, **kwargs)  # type: ignore[arg-type]
        try:
            message = record.getMessage()
        except Exception:
            # Leave the record as it is: the logging module reports its own
            # formatting errors when a handler formats it.
            return record
        redacted = redact(message)
        if redacted.found:
            record.msg = redacted.text
            record.args = ()
        return record

    setattr(factory, _MARK, True)
    logging.setLogRecordFactory(factory)
