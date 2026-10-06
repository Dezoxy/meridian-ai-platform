"""One JSON object per log line, and an access line with no query and no
address (S064, T-03).

The records are made by the real loggers (``uvicorn.access`` is logged the way
uvicorn logs it, with the five arguments of its h11 and httptools protocols)
and read back from standard output. The conftest puts the root's and uvicorn's
handlers back after every test.
"""

import asyncio
import copy
import json
import logging
import logging.config
import sys
import uuid
from typing import Any

import httpx
import httpx2
import pytest
import uvicorn.config
from servicesupport import GATEWAY_REPLY

from meridian.platform.common import logformat
from meridian.platform.common.logformat import (
    HELD_AT_WARNING,
    UVICORN_LOGGERS,
    configure_logging,
)
from meridian.platform.common.logredaction import WITHHELD, install_log_redaction
from meridian.runtime.model_client import ModelClient

SERVICE = "model-gateway"
ADDRESS = "203.0.113.77"
CLIENT = f"{ADDRESS}:51234"
CANARY_QUERY = "canary-query-7f3a"
EMAIL = "ana.kovacs@example.com"
# Text a pattern does not find: a name and a street pass the redaction.
NAME = "Anna Szabo"
STREET = "Teszt utca 12, Budapest"
CANARY_MESSAGE = "canary-message-4c9e"
CANARY_ERROR = "canary-error-8d12"
PATH_CUT = 256
UVICORN_TEMPLATE = '%s - "%s %s HTTP/%s" %d'
FIELDS = ("time", "level", "logger", "service", "message")


def _lines(capsys: pytest.CaptureFixture[str]) -> list[dict[str, Any]]:
    """Every line written to standard output, each parsed as one JSON object."""
    out = capsys.readouterr().out
    return [json.loads(line) for line in out.splitlines()]


def _access(
    client: object = CLIENT,
    method: object = "GET",
    target: object = f"/claims/CLM-0001?q={CANARY_QUERY}",
    version: object = "1.1",
    status: object = 200,
) -> None:
    logging.getLogger("uvicorn.access").info(
        UVICORN_TEMPLATE, client, method, target, version, status
    )


@pytest.fixture(autouse=True)
def configured() -> None:
    install_log_redaction()
    configure_logging(SERVICE)


def test_a_record_is_one_json_object_with_the_fixed_fields(
    capsys: pytest.CaptureFixture[str],
) -> None:
    logging.getLogger("meridian.test").info(
        "claim %s moved to %s", "CLM-0001", "review"
    )

    (line,) = _lines(capsys)

    assert tuple(line) == FIELDS
    assert line["level"] == "INFO"
    assert line["logger"] == "meridian.test"
    assert line["service"] == SERVICE
    assert line["message"] == "claim CLM-0001 moved to review"
    assert line["time"].endswith("+00:00")


def test_a_message_with_a_newline_a_quote_and_a_control_character_is_one_line(
    capsys: pytest.CaptureFixture[str],
) -> None:
    forged = 'first\n{"level": "CRITICAL", "message": "forged"}\x1b[31m\x00"end'

    logging.getLogger("meridian.test").warning("claimant wrote %s", forged)

    out = capsys.readouterr().out
    assert out.count("\n") == 1
    line = json.loads(out)
    assert line["level"] == "WARNING"
    assert line["message"] == f"claimant wrote {forged}"


def test_an_exception_is_one_field_holding_its_frames_and_its_class_and_no_message(
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail() -> None:
        raise ValueError(f"claimant {NAME}, {STREET}; {CANARY_MESSAGE}")

    try:
        fail()
    except ValueError:
        logging.getLogger("meridian.test").exception("failed")

    out = capsys.readouterr().out
    assert out.count("\n") == 1
    line = json.loads(out)
    assert line["message"] == "failed"
    text = line["exception"]
    assert text.startswith("Traceback (most recent call last):\n")
    assert 'test_logformat.py", line ' in text
    assert "in fail\n" in text
    assert "raise ValueError(" in text
    assert text.splitlines()[-1] == "ValueError"
    for withheld in (NAME, STREET, CANARY_MESSAGE):
        assert withheld not in out


def test_a_chained_pair_of_exceptions_is_written_as_two_links_with_no_message(
    capsys: pytest.CaptureFixture[str],
) -> None:
    def explicit() -> None:
        try:
            raise KeyError(f"{NAME} {CANARY_MESSAGE}")
        except KeyError as cause:
            raise ValueError(f"{STREET} {CANARY_MESSAGE}") from cause

    def implicit() -> None:
        try:
            raise TypeError(f"{NAME} {CANARY_MESSAGE}")
        except TypeError:
            raise OSError(f"{STREET} {CANARY_MESSAGE}") from None

    def during() -> None:
        try:
            raise TypeError(f"{NAME} {CANARY_MESSAGE}")
        except TypeError:
            raise RuntimeError(f"{STREET} {CANARY_MESSAGE}")  # noqa: B904

    texts = []
    for raiser in (explicit, implicit, during):
        try:
            raiser()
        except Exception:
            logging.getLogger("meridian.test").exception("failed")
        texts.append(json.loads(capsys.readouterr().out)["exception"])

    cause, suppressed, context = texts
    assert cause.index("\nKeyError\n") < cause.index(
        "The above exception was the direct cause of the following exception:"
    )
    assert cause.splitlines()[-1] == "ValueError"
    assert "TypeError" not in suppressed
    assert suppressed.splitlines()[-1] == "OSError"
    assert context.index("\nTypeError\n") < context.index(
        "During handling of the above exception, another exception occurred:"
    )
    assert context.splitlines()[-1] == "RuntimeError"
    for text in texts:
        for withheld in (NAME, STREET, CANARY_MESSAGE):
            assert withheld not in text


def test_an_exception_group_is_written_with_its_members_and_no_message(
    capsys: pytest.CaptureFixture[str],
) -> None:
    try:
        raise ExceptionGroup(
            f"group {CANARY_MESSAGE}",
            [ValueError(f"{NAME}"), TypeError(f"{STREET}")],
        )
    except ExceptionGroup:
        logging.getLogger("meridian.test").exception("failed")

    out = capsys.readouterr().out
    text = json.loads(out)["exception"]
    assert out.count("\n") == 1
    assert "ExceptionGroup" in text
    assert "ValueError" in text
    assert "TypeError" in text
    for withheld in (NAME, STREET, CANARY_MESSAGE):
        assert withheld not in out


def test_an_exception_whose_text_cannot_be_made_is_still_written_by_its_class(
    capsys: pytest.CaptureFixture[str],
) -> None:
    class Unprintable(Exception):
        def __str__(self) -> str:
            raise RuntimeError(CANARY_MESSAGE)

    try:
        raise Unprintable(NAME)
    except Unprintable:
        logging.getLogger("meridian.test").exception("failed")

    out = capsys.readouterr()
    text = json.loads(out.out)["exception"]
    assert text.splitlines()[-1].endswith("Unprintable")
    assert NAME not in out.out + out.err
    assert CANARY_MESSAGE not in out.out + out.err
    assert out.err == ""


def test_a_record_with_only_the_text_of_an_exception_gets_the_fixed_word(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # A record another factory made: its text is a traceback nobody redacted.
    record = logging.LogRecord(
        "meridian.test", logging.ERROR, __file__, 1, "failed", (), None
    )
    record.exc_text = f"Traceback (most recent call last):\nValueError: {NAME}"

    line = json.loads(logformat.JsonFormatter(SERVICE).format(record))

    assert line["exception"] == WITHHELD
    assert NAME not in json.dumps(line)


def test_a_record_the_factory_did_not_build_is_written_by_its_class_only() -> None:
    try:
        raise ValueError(f"{NAME} {CANARY_MESSAGE}")
    except ValueError:
        exc_info = sys.exc_info()
    record = logging.LogRecord(
        "meridian.test", logging.ERROR, __file__, 1, "failed", (), exc_info
    )

    line = json.loads(logformat.JsonFormatter(SERVICE).format(record))

    assert line["exception"].splitlines()[-1] == "ValueError"
    assert NAME not in json.dumps(line)
    assert CANARY_MESSAGE not in json.dumps(line)


def test_an_exception_that_cannot_be_written_is_the_fixed_word_not_a_lost_record(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*args: object) -> None:
        raise RuntimeError(CANARY_MESSAGE)

    monkeypatch.setattr(logformat, "_chain_lines", broken)

    try:
        raise ValueError(NAME)
    except ValueError:
        logging.getLogger("meridian.test").exception("failed")

    out = capsys.readouterr()
    assert json.loads(out.out)["exception"] == WITHHELD
    assert out.err == ""


def test_logging_an_exception_outside_a_handler_writes_no_exception_field(
    capsys: pytest.CaptureFixture[str],
) -> None:
    logging.getLogger("meridian.test").info("nothing raised", exc_info=True)

    (line,) = _lines(capsys)

    assert "exception" not in line


def test_a_record_without_an_exception_has_no_exception_field(
    capsys: pytest.CaptureFixture[str],
) -> None:
    logging.getLogger("meridian.test").info("plain")

    (line,) = _lines(capsys)

    assert "exception" not in line


def test_an_address_in_a_message_is_redacted_in_the_line(
    capsys: pytest.CaptureFixture[str],
) -> None:
    logging.getLogger("meridian.test").info("claimant %s wrote", EMAIL)

    (line,) = _lines(capsys)

    assert line["message"] == "claimant [email] wrote"


def test_a_record_the_redaction_withholds_is_written_as_its_fixed_word(
    capsys: pytest.CaptureFixture[str],
) -> None:
    logging.getLogger("meridian.test").info(
        "contact %s@example.com now", "kovacs.peter"
    )

    out = capsys.readouterr()

    assert json.loads(out.out)["message"] == WITHHELD
    assert "kovacs.peter" not in out.out + out.err


def test_a_record_whose_formatting_fails_without_the_redaction_is_withheld(
    capsys: pytest.CaptureFixture[str],
) -> None:
    record = logging.LogRecord(
        "meridian.test", logging.INFO, __file__, 1, "value %d", ("text",), None
    )

    line = json.loads(logformat.JsonFormatter(SERVICE).format(record))

    assert line["message"] == WITHHELD
    assert capsys.readouterr().err == ""


def test_what_a_record_carries_in_extra_is_not_written(
    capsys: pytest.CaptureFixture[str],
) -> None:
    logging.getLogger("meridian.test").info("plain", extra={"claimant": EMAIL})

    out = capsys.readouterr().out

    assert EMAIL not in out
    assert "claimant" not in json.loads(out)


# ── a write that fails ──────────────────────────────────────────────────────
class _BrokenStream:
    """A stream whose every write fails, with a message that quotes a canary."""

    def write(self, text: str) -> int:
        raise BrokenPipeError(CANARY_ERROR)

    def flush(self) -> None:
        raise BrokenPipeError(CANARY_ERROR)


def _fixed_line(logger: str, level: str, error: str) -> str:
    return f"log record not written: logger={logger} level={level} error={error}\n"


def test_a_failed_write_prints_one_fixed_line_and_never_the_record(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "stdout", _BrokenStream())

    _access(target=f"/claims/CLM-0001?q={CANARY_QUERY}")
    logging.getLogger("meridian.test").warning("claimant %s wrote", NAME)

    err = capsys.readouterr().err
    assert err == (
        _fixed_line("uvicorn.access", "INFO", "BrokenPipeError")
        + _fixed_line("meridian.test", "WARNING", "BrokenPipeError")
    )
    for withheld in (ADDRESS, "51234", CANARY_QUERY, NAME, CANARY_ERROR, "CLM-0001"):
        assert withheld not in err


def test_a_failed_write_with_a_broken_standard_error_raises_nothing(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "stdout", _BrokenStream())
    monkeypatch.setattr(sys, "stderr", _BrokenStream())

    logging.getLogger("meridian.test").warning("claimant %s wrote", NAME)

    monkeypatch.undo()
    logging.getLogger("meridian.test").info("still logging")
    (line,) = _lines(capsys)
    assert line["message"] == "still logging"


# ── the access line ─────────────────────────────────────────────────────────
def test_an_access_record_is_fields_with_the_path_cut_before_the_query(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _access()

    (line,) = _lines(capsys)

    assert line["logger"] == "uvicorn.access"
    assert line["method"] == "GET"
    assert line["path"] == "/claims/CLM-0001"
    assert line["status"] == 200
    assert line["http_version"] == "1.1"
    assert line["message"] == "GET /claims/CLM-0001 HTTP/1.1 200"


def test_an_access_record_holds_neither_the_query_nor_the_address(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _access()

    out = capsys.readouterr().out

    assert CANARY_QUERY not in out
    assert ADDRESS not in out
    assert "51234" not in out
    assert "?" not in out


@pytest.mark.parametrize(
    "args",
    [
        (CLIENT,),
        (CLIENT, "GET", f"/x?q={CANARY_QUERY}", "1.1"),
        (CLIENT, "GET", f"/x?q={CANARY_QUERY}", "1.1", "200"),
        (CLIENT, "GET", f"/x?q={CANARY_QUERY}", "1.1", True),
        (CLIENT, 7, f"/x?q={CANARY_QUERY}", "1.1", 200),
        (CLIENT, "GET", f"/x?q={CANARY_QUERY}", "1.1", 200, "extra"),
        {"client": CLIENT, "target": f"/x?q={CANARY_QUERY}"},
    ],
)
def test_an_access_record_of_another_shape_is_written_without_its_arguments(
    capsys: pytest.CaptureFixture[str], args: Any
) -> None:
    template = UVICORN_TEMPLATE if isinstance(args, tuple) else "%(client)s %(target)s"
    logging.getLogger("uvicorn.access").info(
        template, *args if isinstance(args, tuple) else (args,)
    )

    out = capsys.readouterr().out

    assert CANARY_QUERY not in out
    assert ADDRESS not in out
    line = json.loads(out)
    assert line["logger"] == "uvicorn.access"
    assert "path" not in line
    assert "status" not in line
    assert line["unparsed"] is True


def test_an_access_record_of_the_known_shape_is_not_marked_unparsed(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _access()
    logging.getLogger("meridian.test").info(UVICORN_TEMPLATE, 1, "a", "b", "c", 2)

    lines = _lines(capsys)

    assert [line["logger"] for line in lines] == ["uvicorn.access", "meridian.test"]
    assert all("unparsed" not in line for line in lines)


def test_an_access_record_of_another_shape_is_written_as_its_template_only(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # A WebSocket record: two arguments, as uvicorn writes it.
    logging.getLogger("uvicorn.access").info(
        '%s - "WebSocket %s" [accepted]', CLIENT, f"/ws?q={CANARY_QUERY}"
    )

    (line,) = _lines(capsys)

    assert line["message"] == '%s - "WebSocket %s" [accepted]'
    assert line["unparsed"] is True


# ── the path: decoded, redacted, bounded ────────────────────────────────────
@pytest.mark.parametrize(
    ("target", "path"),
    [
        ("/u/anna.kovacs%40example.com", "/u/[email]"),
        ("/p/%2B36301234567", "/p/[phone]"),
        (
            "http%3A//user%3Apass%40host.example/path?x=1",
            "http://user:[email]/path",
        ),
    ],
)
def test_an_identifier_percent_encoded_in_the_path_is_redacted_after_decoding(
    capsys: pytest.CaptureFixture[str], target: str, path: str
) -> None:
    _access(target=target)

    out = capsys.readouterr().out

    line = json.loads(out)
    assert line["path"] == path
    assert line["message"] == f"GET {path} HTTP/1.1 200"
    for withheld in ("anna.kovacs", "36301234567", "pass", "x=1"):
        assert withheld not in out


def test_a_decoded_newline_quote_and_question_mark_stay_in_one_line(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _access(target=f"/a%0Ab%22c%3Fd%0D%1B?q={CANARY_QUERY}")

    out = capsys.readouterr().out

    assert out.count("\n") == 1
    line = json.loads(out)
    # The query is cut at the first real "?", before the decoding; the decoded
    # "?" is a character of the path.
    assert line["path"] == '/a\nb"c?d\r\x1b'
    assert line["message"] == 'GET /a\nb"c?d\r\x1b HTTP/1.1 200'
    assert CANARY_QUERY not in out


def test_a_long_path_is_cut_at_the_bound_with_a_marker(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _access(target="/" + "a" * 30_000)

    out = capsys.readouterr().out

    line = json.loads(out)
    assert line["path"] == "/" + "a" * (PATH_CUT - 1) + logformat.PATH_CUT_MARKER
    assert line["message"] == f"GET {line['path']} HTTP/1.1 200"
    assert len(out) < 1_000


def test_a_path_of_exactly_the_bound_is_written_whole_and_one_more_is_cut(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _access(target="/" + "a" * (PATH_CUT - 1))
    _access(target="/" + "a" * PATH_CUT)

    whole, cut = _lines(capsys)

    assert whole["path"] == "/" + "a" * (PATH_CUT - 1)
    assert cut["path"] == "/" + "a" * (PATH_CUT - 1) + logformat.PATH_CUT_MARKER


def test_an_address_that_crosses_the_bound_is_redacted_before_the_cut(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _access(target="/" + "a" * (PATH_CUT - 5) + f"/{EMAIL}")

    out = capsys.readouterr().out

    # Cut first, the e-mail address would be left as "ana.kovacs@exam".
    assert "ana" not in out
    assert "kovacs" not in out
    assert json.loads(out)["path"].endswith("[em" + logformat.PATH_CUT_MARKER)


def test_the_health_probe_is_found_by_its_decoded_path(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _access(target="/%68ealthz", status=200)

    assert capsys.readouterr().out == ""


def test_a_healthy_probe_of_the_health_path_is_not_written(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _access(target="/healthz", status=200)
    _access(target=f"/healthz?q={CANARY_QUERY}", status=200)

    assert capsys.readouterr().out == ""


def test_a_health_path_that_is_not_a_200_is_written(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _access(target="/healthz", status=503)

    (line,) = _lines(capsys)

    assert (line["path"], line["status"]) == ("/healthz", 503)


@pytest.mark.parametrize(
    ("method", "target"),
    [("GET", "/healthz/deep"), ("GET", "/healthzz"), ("POST", "/healthz")],
)
def test_a_200_on_another_path_or_by_another_method_is_written(
    capsys: pytest.CaptureFixture[str], method: str, target: str
) -> None:
    _access(method=method, target=target, status=200)

    (line,) = _lines(capsys)

    assert line["status"] == 200


def test_the_health_filter_leaves_another_logger_alone(
    capsys: pytest.CaptureFixture[str],
) -> None:
    logging.getLogger("meridian.test").info(
        UVICORN_TEMPLATE, CLIENT, "GET", "/healthz", "1.1", 200
    )

    (line,) = _lines(capsys)

    assert line["logger"] == "meridian.test"


# ── what the function does to the loggers ───────────────────────────────────
def _json_handlers(logger: logging.Logger) -> list[logging.Handler]:
    return [
        h for h in logger.handlers if isinstance(h.formatter, logformat.JsonFormatter)
    ]


def test_calling_it_twice_adds_no_second_handler_and_takes_the_new_service(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging("agent-runtime")

    logging.getLogger("meridian.test").info("once")

    (line,) = _lines(capsys)
    assert line["service"] == "agent-runtime"
    assert len(_json_handlers(logging.getLogger())) == 1


def test_it_takes_over_the_loggers_uvicorn_configured_before_it() -> None:
    logging.config.dictConfig(copy.deepcopy(uvicorn.config.LOGGING_CONFIG))
    assert logging.getLogger("uvicorn.access").handlers  # uvicorn's own

    configure_logging(SERVICE)

    root = logging.getLogger()
    assert len(_json_handlers(root)) == 1
    for name in UVICORN_LOGGERS:
        logger = logging.getLogger(name)
        assert logger.handlers == []
        assert logger.propagate is True
    # uvicorn's protocols read this once per connection: with no handler the
    # access log would be switched off.
    assert logging.getLogger("uvicorn.access").hasHandlers() is True
    assert root.level == logging.INFO


def test_an_access_log_uvicorn_switched_off_stays_off(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # What ``uvicorn --no-access-log`` leaves: no handler and no propagation.
    uvicorn.Config("meridian.test:app", access_log=False)
    access = logging.getLogger("uvicorn.access")
    assert (access.handlers, access.propagate) == ([], False)

    configure_logging(SERVICE)

    assert (access.handlers, access.propagate) == ([], False)
    assert access.hasHandlers() is False
    _access()
    assert capsys.readouterr().out == ""
    # The rest of uvicorn's loggers are taken over as before.
    for name in ("uvicorn", "uvicorn.error"):
        assert logging.getLogger(name).handlers == []
        assert logging.getLogger(name).propagate is True
    logging.getLogger("uvicorn.error").info("still written")
    (line,) = _lines(capsys)
    assert line["message"] == "still written"


def test_an_access_log_uvicorn_left_on_is_written_after_the_takeover(
    capsys: pytest.CaptureFixture[str],
) -> None:
    uvicorn.Config("meridian.test:app", access_log=True)
    access = logging.getLogger("uvicorn.access")
    assert access.handlers  # uvicorn's own

    configure_logging(SERVICE)

    assert (access.handlers, access.propagate) == ([], True)
    _access()
    (line,) = _lines(capsys)
    assert line["logger"] == "uvicorn.access"


def test_an_access_logger_nobody_configured_is_written_and_stays_so_on_a_second_call(
    capsys: pytest.CaptureFixture[str],
) -> None:
    access = logging.getLogger("uvicorn.access")
    access.handlers = []
    access.propagate = True

    configure_logging(SERVICE)
    configure_logging(SERVICE)

    assert (access.handlers, access.propagate) == ([], True)
    _access()
    assert len(_lines(capsys)) == 1


def test_uvicorns_own_records_are_json_after_it_took_over(
    capsys: pytest.CaptureFixture[str],
) -> None:
    logging.config.dictConfig(copy.deepcopy(uvicorn.config.LOGGING_CONFIG))
    configure_logging(SERVICE)

    logging.getLogger("uvicorn.error").info("Started server process [%d]", 1)
    logging.getLogger("uvicorn").info("Waiting for application startup.")

    lines = _lines(capsys)
    assert [(x["logger"], x["message"]) for x in lines] == [
        ("uvicorn.error", "Started server process [1]"),
        ("uvicorn", "Waiting for application startup."),
    ]


def test_each_client_library_is_held_at_warning() -> None:
    for name in ("httpx", "httpx2"):
        assert name in HELD_AT_WARNING
        assert logging.getLogger(name).level == logging.WARNING


def test_the_runtimes_model_client_call_writes_no_url(
    capsys: pytest.CaptureFixture[str],
) -> None:
    http = httpx.Client(
        base_url="https://gateway.invalid",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=GATEWAY_REPLY)
        ),
    )
    model = ModelClient(
        http,
        tenant="claims-triage",
        agent="claims-triage",
        run_id=uuid.UUID("00000000-0000-4000-8000-0000000000aa"),
        max_calls=1,
    )

    model.chat([{"role": "user", "content": "hi"}])

    assert capsys.readouterr().out == ""


def test_a_call_of_the_tool_clients_kind_writes_no_url(
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def call() -> int:
        async with httpx2.AsyncClient(
            transport=httpx2.MockTransport(lambda request: httpx2.Response(200))
        ) as client:
            reply = await client.post(f"https://tool.invalid/mcp?q={CANARY_QUERY}")
            return reply.status_code

    assert asyncio.run(call()) == 200

    out = capsys.readouterr().out
    assert CANARY_QUERY not in out
    assert out == ""


def test_the_same_calls_wrote_their_url_before_the_hold(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # The control of the two tests above: with the holds lifted, each library
    # logs the request's URL at INFO, so the tests could fail.
    for name in HELD_AT_WARNING:
        logging.getLogger(name).setLevel(logging.INFO)

    http = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200))
    )
    http.get(f"https://gateway.invalid/v1/chat?q={CANARY_QUERY}")

    assert CANARY_QUERY in capsys.readouterr().out
