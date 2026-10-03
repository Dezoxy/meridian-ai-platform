"""Keep personal identifiers out of every log record the process builds (S047,
T-03).

``install_log_redaction`` wraps the logging module's record factory: once a
record is built, its message, each of its arguments and its exception text are
run through the guardrails' ``redact``. The record keeps the shape it had, so a
handler or formatter that unpacks the arguments (uvicorn's access log reads
five) still works; only the values that held an identifier change.

Why a record factory and not a filter: a filter on a logger misses the records
of its children, and a filter on handlers misses the handlers added after
start (uvicorn's, the test harness's, the collector's). The factory sees every
record of the process once, whoever handles it.

The message and the arguments are redacted one by one, not the formatted line,
because the line is built later, by the handler. An identifier cut in two by
the template (``"%s@example.com"``) is not found; log the whole value as one
argument. A record that cannot be redacted is withheld: its message becomes a
fixed text with no arguments and no exception, because the logging module
prints a record it cannot format to stderr with its arguments as they are, so
the factory formats the record once after redacting it and withholds it when
that fails (a template that cuts a value in two does). Numbers, bools and
``None`` are left as they are. The factory never raises.
"""

import logging
from collections.abc import Mapping

from meridian.platform.guardrails import redact

_MARK = "_meridian_log_redaction"
WITHHELD = "log record withheld: it could not be redacted"


def _redacted_value(value: object) -> object:
    """``value`` itself when nothing in its text is found, else the redacted
    text: a string is redacted as it is, any other object by its ``str()``. A
    number, a bool and ``None`` stay as they are: they hold no address or IBAN,
    and ``%d`` needs a number."""
    if value is None or isinstance(value, int | float):  # bool is an int
        return value
    text = value if isinstance(value, str) else str(value)
    redacted = redact(text)
    return redacted.text if redacted.found else value


def _redacted_args(args: object) -> object:
    """The arguments with the same type and shape; the same object when no
    value changed."""
    if isinstance(args, tuple):
        items = tuple(_redacted_value(item) for item in args)
        unchanged = all(new is old for new, old in zip(items, args, strict=True))
        return args if unchanged else items
    if isinstance(args, Mapping):
        values = {key: _redacted_value(item) for key, item in args.items()}
        unchanged = all(values[key] is item for key, item in args.items())
        return args if unchanged else values
    return args


def _redact_record(record: logging.LogRecord) -> None:
    record.msg = _redacted_value(record.msg)
    record.args = _redacted_args(record.args)  # type: ignore[assignment]
    if record.exc_info:
        # The handler's formatter reuses ``exc_text`` instead of formatting.
        traceback = logging.Formatter().formatException(record.exc_info)
        record.exc_text = redact(traceback).text
    # A template that cuts a value in two ("%s@example.com") is redacted piece by
    # piece and cannot be formatted; the handler would then print the arguments
    # as they are. Formatting here, inside the guard, withholds that record.
    record.getMessage()


def _withhold(record: logging.LogRecord) -> None:
    record.msg = WITHHELD
    record.args = ()
    record.exc_info = None
    record.exc_text = None


def install_log_redaction() -> None:
    """Install the redacting record factory, once per process. A second call
    does nothing."""
    inner = logging.getLogRecordFactory()
    if getattr(inner, _MARK, False):
        return

    def factory(*args: object, **kwargs: object) -> logging.LogRecord:
        record = inner(*args, **kwargs)  # type: ignore[arg-type]
        try:
            _redact_record(record)
        except Exception:
            # Nothing of the cause: it can quote the text that was being redacted.
            _withhold(record)
        return record

    setattr(factory, _MARK, True)
    logging.setLogRecordFactory(factory)
