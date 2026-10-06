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


def test_an_exception_is_one_field_holding_the_redacted_traceback(
    capsys: pytest.CaptureFixture[str],
) -> None:
    try:
        raise ValueError(f"cannot reach {EMAIL}")
    except ValueError:
        logging.getLogger("meridian.test").exception("failed")

    out = capsys.readouterr().out
    assert out.count("\n") == 1
    line = json.loads(out)
    assert line["message"] == "failed"
    assert "Traceback (most recent call last)" in line["exception"]
    assert "ValueError: cannot reach [email]" in line["exception"]
    assert "ana.kovacs" not in out


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
