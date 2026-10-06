"""One log format for every service: one JSON object per line (S064, T-03).

``configure_logging`` is called in each service's factory and in the sweep's
``main``, right after ``install_log_redaction`` (which stays the record
factory: this module adds a handler and a formatter, never a second factory).
A node agent reads each pod's output and ships it to Loki, so the output has to
be worth reading and safe to keep:

- every record of the process, uvicorn's own included, is written by one
  handler to standard output as one JSON object: ``time`` (UTC, ISO 8601),
  ``level``, ``logger``, ``service``, ``message`` and, when there is one,
  ``exception``. The encoder escapes a newline, a quote and a control
  character, so a record is one line whatever claimant text it holds (T-03).
  Nothing of a record's ``extra`` is written: the redaction does not cover it;
- ``exception`` is each exception's frames (file, line, function and the
  source line, as ``traceback`` prints them) and its class's qualified name,
  for every exception of the chain (``__cause__``, ``__context__``) and every
  member of an exception group, and NO message and no notes: a message can
  quote claimant text, and the redaction finds patterns (an address, an IBAN),
  not a name or a street. What the field still shows is source lines, of this
  code and of the libraries it calls, pattern-redacted: a literal in a source
  line (a message in a ``raise``, a value in an assignment) goes out as
  written apart from what a pattern finds, so no source line may hold a name
  or a street. The joined text goes through ``redact`` (imported at the top of
  this module from the guardrails, as the path's is; no import cycle, the
  guardrails do not import this module) and is cut at ``EXCEPTION_MAX_CHARS``
  with ``PATH_CUT_MARKER``, redaction first. It is built from
  ``record.exc_info``. A record that holds only ``exc_text`` (made by another
  record factory) gets the fixed word ``WITHHELD``: that text was never made
  without a message. An exception that cannot be walked is ``WITHHELD`` as
  well, never a lost record;
- uvicorn applies its own logging configuration before it imports the app
  with ``--factory``, so the factory runs second and takes uvicorn's loggers
  over: their handlers go, they propagate to the root's one handler. They keep
  a handler through propagation on purpose: uvicorn reads
  ``access_logger.hasHandlers()`` once per connection and switches the access
  log off when it is false;
- the access record is built from fields, not uvicorn's text. uvicorn logs it
  with five arguments (client address, method, the path WITH its query
  string, HTTP version, status) in its h11 and httptools protocols alike; the
  line keeps ``method``, ``path``, ``http_version`` and ``status``, and nothing
  of the client's address, which behind an edge is a person's. The path is cut
  at the first ``?`` of the raw target, the userinfo of an absolute-form target
  is cut, then it is decoded until it stops changing, the userinfo cut again
  after each round (uvicorn percent-encodes it, so an address in it would pass
  the redaction encoded; at most ``PATH_UNQUOTE_ROUNDS`` rounds, and a path
  that a further round would still change is ``PATH_OVER_ENCODED``), redacted and cut
  at ``PATH_MAX_CHARS`` with ``PATH_CUT_MARKER``; a decoded newline, quote or
  control character is escaped by the encoder like any other text. A record of
  ``uvicorn.access`` whose arguments are not that shape is written as its bare
  template, never its arguments, with ``"unparsed": true``, so a uvicorn that
  changes its access record shows up in Loki instead of as a quiet gap;
- ``uvicorn --no-access-log`` stays off: uvicorn switches the access log off by
  leaving ``uvicorn.access`` with no handler and no propagation, and the
  takeover leaves the logger in that state when it finds it so (the chart
  passes no such flag; the rule is that an operator's choice is not undone);
- a 200 on ``GET /healthz`` is not written: the kubelet's probe would be most
  of the output. A ``/healthz`` that is not a 200 is;
- the two HTTP client libraries log each request's URL at INFO, so their
  loggers are held at WARNING (``HELD_AT_WARNING``);
- a write that fails (the node agent's pipe closed, the disk full) prints one
  fixed line to standard error and never the record: the logging module's own
  ``handleError`` prints the message template and the arguments, which for an
  access record are the client's address and the query string, on a stream the
  node agent ships too. The line names the logger, the level and the class of
  the error, never its text, and ``handleError`` never raises.

Not JSON, whatever this module does: what the process prints before the
factory ran, the fixed line above, and a crash of the interpreter or of the
factory itself (a settings error is uvicorn's traceback on standard error).
"""

import json
import logging
import sys
import traceback
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any
from urllib.parse import unquote

from meridian.platform.common.logredaction import WITHHELD
from meridian.platform.guardrails import EMAIL_PLACEHOLDER, redact

ACCESS_LOGGER = "uvicorn.access"
UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", ACCESS_LOGGER)
# Loggers that say too much at INFO, held at WARNING: httpx and httpx2 log the
# request's URL, and the second agent framework (a name held as a string, not
# imported: ADR 2) writes about a dozen lines a leg of a workflow (supersteps,
# checkpoint IDs, the step IDs of a dead-end check). The measurement in
# ``test_logformat_services.py`` put the root at INFO and sent three requests
# (the probe, an unknown path and a post) through each of the six services with
# the app's lifespan, and the sweep's ``main`` against an empty database, and
# found no other logger that writes at INFO. It did not cover what ``psycopg``
# and ``langgraph`` write at INFO during a real run of the runtime or the sweep
# with data, nor the gateway's live providers: a logger found there later is
# held here.
HELD_AT_WARNING = ("httpx", "httpx2", "agent_framework")
HEALTH_PATH = "/healthz"
ACCESS_ARGUMENTS = 5
# The path of an access line, after decoding and redaction, is cut here: a
# request can send a path of any length, and a line is read in Loki.
PATH_MAX_CHARS = 256
PATH_CUT_MARKER = "[cut]"
# A path is unquoted until it stops changing, at most this many times.
PATH_UNQUOTE_ROUNDS = 3
# What the field holds for a path that a further round would still change: a
# short bracketed word like the redaction's placeholders, and nothing of the path.
PATH_OVER_ENCODED = "[encoded]"
# The ``exception`` field, after redaction, is cut here with the same marker: a
# deeply nested exception group gave 166 KB of indentation and frames.
EXCEPTION_MAX_CHARS = 8192
CAUSE_LINE = "The above exception was the direct cause of the following exception:"
CONTEXT_LINE = "During handling of the above exception, another exception occurred:"


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
        "path": _path_field(target),
        "http_version": version,
        "status": status,
    }


def _without_userinfo(target: str) -> str:
    """``target`` without the ``user:word@`` of an absolute-form request target
    (``scheme://user:word@host/path``), which names a person; any other target
    is returned as it is. The userinfo ends at the last ``@`` of the authority,
    which ends at the first ``/``.

    The record's arguments were redacted when the record was made, so a
    ``word@host.example`` in the authority may already be ``[email]``, with the
    user name before it: the authority then starts at the placeholder."""
    scheme, separator, rest = target.partition("://")
    if not (separator and scheme.isascii() and scheme.isalpha()):
        return target
    authority, slash, path = rest.partition("/")
    if "@" in authority:
        authority = authority.rpartition("@")[2]
    elif EMAIL_PLACEHOLDER in authority:
        authority = EMAIL_PLACEHOLDER + authority.partition(EMAIL_PLACEHOLDER)[2]
    return f"{scheme}{separator}{authority}{slash}{path}"


def _path_field(target: str) -> str:
    """The request target without its query, decoded, redacted and bounded.

    The query is cut at the first ``?`` BEFORE decoding: a decoded ``%3F`` is a
    character of the path. Redacting comes before the cut, so an address that
    crosses the bound is not left in pieces.

    The userinfo of an absolute-form target is cut from the raw text first and
    again after each round of decoding (a scheme encoded hides it from the
    first cut). The decoding repeats until the text stops changing, at most
    ``PATH_UNQUOTE_ROUNDS`` times (an address encoded twice is redacted as one
    encoded once is); a text a further round would still change is
    ``PATH_OVER_ENCODED`` and nothing of it."""
    path = _without_userinfo(target.partition("?")[0])
    for _ in range(PATH_UNQUOTE_ROUNDS):
        decoded = _without_userinfo(unquote(path))
        if decoded == path:
            break
        path = decoded
    else:
        if unquote(path) != path:
            return PATH_OVER_ENCODED
    path = redact(path).text
    if len(path) > PATH_MAX_CHARS:
        return path[:PATH_MAX_CHARS] + PATH_CUT_MARKER
    return path


def _chain(top: traceback.TracebackException) -> list[traceback.TracebackException]:
    """``top`` and the exceptions it came from, newest first, the way the
    ``traceback`` module walks them: the cause, else the context unless it was
    suppressed."""
    chain = [top]
    link = top
    while True:
        if link.__cause__ is not None:
            link = link.__cause__
        elif link.__context__ is not None and not link.__suppress_context__:
            link = link.__context__
        else:
            return chain
        chain.append(link)


def _chain_lines(top: traceback.TracebackException) -> list[str]:
    """The lines of ``top``'s chain, oldest exception first."""
    lines: list[str] = []
    chain = _chain(top)
    for index in range(len(chain) - 1, -1, -1):
        link = chain[index]
        lines.extend(_link_lines(link))
        if index:
            newer = chain[index - 1]
            connector = CAUSE_LINE if newer.__cause__ is not None else CONTEXT_LINE
            lines.extend(["", connector, ""])
    return lines


def _link_lines(link: traceback.TracebackException) -> list[str]:
    """One exception: its frames, its class and, for a group, its members. The
    message (``link``'s own text) and the notes are never read."""
    lines: list[str] = []
    if link.stack:
        lines.append("Traceback (most recent call last):")
        lines.extend("".join(link.stack.format()).splitlines())
    lines.append(link.exc_type_str)
    for number, member in enumerate(link.exceptions or (), start=1):
        lines.append(f"  member {number}:")
        lines.extend(f"    {line}" for line in _chain_lines(member))
    return lines


def _exception_field(record: logging.LogRecord) -> str | None:
    """The ``exception`` field of a record, or ``None`` when it has none."""
    exc_info = record.exc_info
    if isinstance(exc_info, tuple):
        if exc_info[1] is None:  # ``exc_info=True`` outside a handler
            return None
        try:
            top = traceback.TracebackException(*exc_info)
            # Redacted before the cut, as the path is: an address across the
            # bound is not left in pieces.
            text = redact("\n".join(_chain_lines(top))).text
            if len(text) > EXCEPTION_MAX_CHARS:
                return text[:EXCEPTION_MAX_CHARS] + PATH_CUT_MARKER
            return text
        except Exception:
            # Nothing of the cause: it can quote the exception's text.
            return WITHHELD
    # Only text: made by a factory that kept the message, which nobody cut.
    return WITHHELD if record.exc_text else None


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
            access = _access_fields(record)
            fields.update(access if access is not None else {"unparsed": True})
        exception = _exception_field(record)
        if exception is not None:
            fields["exception"] = exception
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

    def handleError(self, record: logging.LogRecord) -> None:
        """One fixed line on standard error for a record that could not be
        written, in place of the logging module's, which prints the record's
        template and arguments (an access record's client address and query).
        Nothing of the record's text and nothing of the error's: its class
        only. Called inside ``emit``'s ``except``, so ``sys.exception()`` is the
        error. Never raises: standard error may be broken too."""
        with suppress(Exception):  # logging it would be this method again
            error = type(sys.exception()).__name__
            sys.stderr.write(
                f"log record not written: logger={record.name} "
                f"level={record.levelname} error={error}\n"
            )


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
        if name == ACCESS_LOGGER and not (
            uvicorn_logger.handlers or uvicorn_logger.propagate
        ):
            continue  # ``--no-access-log``: uvicorn switched it off, so it stays
        for own in uvicorn_logger.handlers[:]:
            uvicorn_logger.removeHandler(own)
        uvicorn_logger.propagate = True
    for name in HELD_AT_WARNING:
        logging.getLogger(name).setLevel(logging.WARNING)
