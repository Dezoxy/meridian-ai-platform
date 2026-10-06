"""One log format for every service: one JSON object per line (S064, T-03).

``configure_logging`` is called in each service's factory and in the sweep's
``main``, right after ``install_log_redaction`` (which stays the record
factory: this module adds a handler and a formatter, never a second factory).
A node agent reads each pod's output and ships it to Loki, so the output has to
be worth reading and safe to keep:

- every record of the process, uvicorn's own included, is written by one
  handler to standard output as one JSON object: ``time`` (UTC, ISO 8601),
  ``level``, ``logger``, ``service``, ``message`` and, when there is one,
  ``exception`` (the traceback the redacting record factory left). The encoder
  escapes a newline, a quote and a control character, so a record is one line
  whatever claimant text it holds (T-03). Nothing of a record's ``extra`` is
  written: the redaction does not cover it;
- uvicorn applies its own logging configuration before it imports the app
  with ``--factory``, so the factory runs second and takes uvicorn's loggers
  over: their handlers go, they propagate to the root's one handler. They keep
  a handler through propagation on purpose: uvicorn reads
  ``access_logger.hasHandlers()`` once per connection and switches the access
  log off when it is false;
- the access record is built from fields, not uvicorn's text. uvicorn logs it
  with five arguments (client address, method, the path WITH its query
  string, HTTP version, status) in its h11 and httptools protocols alike; the
  line keeps ``method``, ``path`` (cut at the first ``?``), ``http_version``
  and ``status``, and nothing of the client's address, which behind an edge is
  a person's. A record of ``uvicorn.access`` whose arguments are not that
  shape is written without them;
- a 200 on ``GET /healthz`` is not written: the kubelet's probe would be most
  of the output. A ``/healthz`` that is not a 200 is;
- the two HTTP client libraries log each request's URL at INFO, so their
  loggers are held at WARNING (``HELD_AT_WARNING``).

Not JSON, whatever this module does: what the process prints before the
factory ran, and a crash of the interpreter or of the factory itself (a
settings error is uvicorn's traceback on standard error).
"""

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

from meridian.platform.common.logredaction import WITHHELD

ACCESS_LOGGER = "uvicorn.access"
UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", ACCESS_LOGGER)
# Loggers that say too much at INFO, held at WARNING: httpx and httpx2 log the
# request's URL. The measurement in ``test_logformat_services.py`` found no
# other logger that writes at INFO in the six services or the sweep.
HELD_AT_WARNING = ("httpx", "httpx2")
HEALTH_PATH = "/healthz"
ACCESS_ARGUMENTS = 5


def _access_fields(record: logging.LogRecord) -> dict[str, Any] | None:
    """The fields of an access record made the way uvicorn makes it, or ``None``
    for any other shape. The address (argument 0) is never read."""
    args = record.args
    if not (isinstance(args, tuple) and len(args) == ACCESS_ARGUMENTS):
        return None
    _address, method, target, version, status = args
    if not (
        isinstance(method, str)
        and isinstance(target, str)
        and isinstance(version, str)
        and isinstance(status, int)
        and not isinstance(status, bool)
    ):
        return None
    return {
        "method": method,
        "path": target.partition("?")[0],
        "http_version": version,
        "status": status,
    }


class JsonFormatter(logging.Formatter):
    """A record as one JSON object on one line."""

    def __init__(self, service: str) -> None:
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        fields: dict[str, Any] = {
            "time": datetime.fromtimestamp(record.created, UTC).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname,
            "logger": record.name,
            "service": self.service,
            "message": self._message(record),
        }
        if record.name == ACCESS_LOGGER:
            fields.update(_access_fields(record) or {})
        if record.exc_text:
            fields["exception"] = record.exc_text
        elif record.exc_info:
            fields["exception"] = self.formatException(record.exc_info)
        return json.dumps(fields)

    @staticmethod
    def _message(record: logging.LogRecord) -> str:
        if record.name == ACCESS_LOGGER:
            # Never ``getMessage()``: the arguments hold the address and the
            # query. A record of another shape is written as its template.
            fields = _access_fields(record)
            if fields is None:
                return str(record.msg)
            return (
                f"{fields['method']} {fields['path']} "
                f"HTTP/{fields['http_version']} {fields['status']}"
            )
        try:
            return record.getMessage()
        except Exception:
            # Without the redaction a template and its arguments can disagree;
            # nothing of the cause, it can quote the arguments.
            return WITHHELD


class _HealthProbeFilter(logging.Filter):
    """Drop the access record of a 200 on ``GET /healthz``."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name != ACCESS_LOGGER:
            return True
        fields = _access_fields(record)
        return not (
            fields is not None
            and fields["method"] == "GET"
            and fields["path"] == HEALTH_PATH
            and fields["status"] == 200
        )


class _StdoutHandler(logging.StreamHandler):  # type: ignore[type-arg]
    """A stream handler on whatever ``sys.stdout`` is when a record is written
    (as the logging module's own last-resort handler does for standard error),
    so a stream replaced after the factory ran, a test's capture, is followed."""

    def __init__(self) -> None:
        logging.Handler.__init__(self)

    @property
    def stream(self) -> Any:
        return sys.stdout

    @stream.setter
    def stream(self, _value: Any) -> None:
        """The stream is always ``sys.stdout``."""


def configure_logging(service: str) -> None:
    """Write every record of the process as JSON to standard output, as
    ``service``, and take uvicorn's loggers over. Safe to call again: the one
    handler is found and kept, and takes the new service name."""
    root = logging.getLogger()
    handler = next((h for h in root.handlers if isinstance(h, _StdoutHandler)), None)
    if handler is None:
        handler = _StdoutHandler()
        handler.addFilter(_HealthProbeFilter())
        root.addHandler(handler)
    handler.setFormatter(JsonFormatter(service))
    root.setLevel(logging.INFO)
    for name in UVICORN_LOGGERS:
        uvicorn_logger = logging.getLogger(name)
        for own in uvicorn_logger.handlers[:]:
            uvicorn_logger.removeHandler(own)
        uvicorn_logger.propagate = True
    for name in HELD_AT_WARNING:
        logging.getLogger(name).setLevel(logging.WARNING)
