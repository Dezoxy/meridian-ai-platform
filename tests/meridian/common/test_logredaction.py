"""The process's log records carry no personal identifier (S047, T-03).

Every test restores the record factory it found, so no other test sees the one
installed here.
"""

import importlib
import inspect
import io
import logging
import sys
from collections.abc import Iterator

import pytest
from uvicorn.logging import AccessFormatter

from meridian.platform.common import logredaction
from meridian.platform.common.logredaction import install_log_redaction

EMAIL = "ana.kovacs@example.com"
FACTORY_MODULES = (
    "meridian.platform.gateway.app",
    "meridian.runtime.app",
    "meridian.workloads.claims_triage.app",
    "meridian.platform.policy_mcp.app",
    "meridian.platform.knowledge_mcp.app",
    "meridian.workloads.claims_triage.mcp_server.app",
)


@pytest.fixture(autouse=True)
def restore_record_factory() -> Iterator[None]:
    saved = logging.getLogRecordFactory()
    try:
        yield
    finally:
        logging.setLogRecordFactory(saved)


def _build(msg: object, args: tuple[object, ...]) -> logging.LogRecord:
    return logging.getLogRecordFactory()(
        "meridian.test", logging.WARNING, __file__, 1, msg, args, None
    )


def test_a_formatted_address_is_replaced_in_the_message(
    caplog: pytest.LogCaptureFixture,
) -> None:
    install_log_redaction()

    with caplog.at_level(logging.WARNING):
        logging.getLogger("meridian.test").warning("claimant %s wrote", EMAIL)

    assert [r.getMessage() for r in caplog.records] == ["claimant [email] wrote"]
    assert EMAIL not in caplog.text


def test_a_child_logger_record_is_redacted_too(
    caplog: pytest.LogCaptureFixture,
) -> None:
    install_log_redaction()

    with caplog.at_level(logging.WARNING):
        logging.getLogger("meridian.x.y").warning("mail %s", EMAIL)

    assert [r.getMessage() for r in caplog.records] == ["mail [email]"]


def test_a_redacted_record_keeps_the_shape_of_its_arguments() -> None:
    install_log_redaction()

    record = _build("claimant %s wrote %d times", (EMAIL, 3))

    assert record.msg == "claimant %s wrote %d times"
    assert record.args == ("[email]", 3)
    assert record.getMessage() == "claimant [email] wrote 3 times"


def test_an_address_in_the_message_itself_is_replaced() -> None:
    install_log_redaction()

    record = _build(f"mail from {EMAIL}", ())

    assert record.msg == "mail from [email]"
    assert record.args == ()


def test_a_record_with_nothing_to_redact_is_untouched() -> None:
    install_log_redaction()
    msg = "claim %s moved to %s"
    args = ("c-1", "review")

    record = _build(msg, args)

    assert record.msg is msg
    assert record.args is args
    assert record.getMessage() == "claim c-1 moved to review"


def _handler_output(logger_name: str) -> tuple[io.StringIO, logging.Logger]:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    logger = logging.getLogger(logger_name)
    logger.addHandler(handler)
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    return stream, logger


def test_a_record_whose_formatting_fails_leaks_nothing_to_the_handler_or_stderr(
    capsys: pytest.CaptureFixture[str],
) -> None:
    install_log_redaction()
    stream, logger = _handler_output("meridian.test.fails")

    # Built in two parts: the traceback stderr gets quotes the line of this call.
    leaking = "anna@" + "example.com"

    logger.error("value %d", leaking)  # %d cannot format text

    captured = capsys.readouterr()
    assert "anna@" not in stream.getvalue()
    assert "anna@" not in captured.err
    assert "anna@" not in captured.out
    # The factory formats the record, so it is withheld whole and the handler
    # has no failure to print.
    assert stream.getvalue() == "log record withheld: it could not be redacted\n"


def test_a_mapping_of_arguments_keeps_its_keys() -> None:
    install_log_redaction()
    stream, logger = _handler_output("meridian.test.mapping")

    logger.warning("claimant %(who)s wrote %(n)d times", {"who": EMAIL, "n": 3})

    assert stream.getvalue() == "claimant [email] wrote 3 times\n"


def test_an_object_whose_text_holds_an_address_is_replaced_by_the_redacted_text() -> (
    None
):
    install_log_redaction()

    class Holder:
        def __str__(self) -> str:
            return f"holder of {EMAIL}"

    record = _build("got %s", (Holder(),))

    assert record.args == ("holder of [email]",)


def test_an_object_with_nothing_to_redact_stays_the_object_it_was() -> None:
    install_log_redaction()
    number = 200
    marker = object()

    record = _build("%d %s", (number, marker))

    assert record.args[0] is number
    assert record.args[1] is marker


def test_the_access_log_of_uvicorn_still_formats_and_carries_no_address() -> None:
    install_log_redaction()
    args = ("127.0.0.1:51234", "GET", f"/claims/{EMAIL}", "1.1", 200)
    record = logging.getLogRecordFactory()(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        args,
        None,
    )

    line = AccessFormatter(use_colors=False).format(record)

    assert line == '127.0.0.1:51234 - "GET /claims/[email] HTTP/1.1" 200'


def test_a_logged_exception_writes_its_traceback_redacted() -> None:
    install_log_redaction()
    stream, logger = _handler_output("meridian.test.exception")

    try:
        raise ValueError(f"cannot reach {EMAIL}")
    except ValueError:
        logger.exception("failed")

    written = stream.getvalue()
    assert "Traceback (most recent call last)" in written
    assert "ValueError: cannot reach [email]" in written
    assert "ana.kovacs" not in written


def test_the_redacted_traceback_is_what_the_record_carries() -> None:
    install_log_redaction()

    try:
        raise ValueError(f"cannot reach {EMAIL}")
    except ValueError:
        record = logging.getLogRecordFactory()(
            "meridian.test",
            logging.ERROR,
            __file__,
            1,
            "failed",
            (),
            sys.exc_info(),
        )

    assert record.exc_text is not None
    assert "ValueError: cannot reach [email]" in record.exc_text
    assert "ana.kovacs" not in record.exc_text


def test_a_record_without_exception_info_gets_no_exception_text() -> None:
    install_log_redaction()

    record = _build("plain", ())

    assert record.exc_text is None


def test_a_redaction_that_fails_withholds_the_record_and_does_not_raise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_log_redaction()

    def explode(text: str) -> object:
        raise RuntimeError(f"redaction broke on {text}")

    monkeypatch.setattr(logredaction, "redact", explode)

    try:
        raise ValueError(f"cannot reach {EMAIL}")
    except ValueError:
        record = _build_with_exception_info(f"claimant {EMAIL}", (EMAIL,))

    assert record.msg == "log record withheld: it could not be redacted"
    assert record.args == ()
    assert record.exc_info is None
    assert record.exc_text is None
    assert EMAIL not in record.getMessage()


def _build_with_exception_info(
    msg: object, args: tuple[object, ...]
) -> logging.LogRecord:
    return logging.getLogRecordFactory()(
        "meridian.test", logging.ERROR, __file__, 1, msg, args, sys.exc_info()
    )


def test_an_argument_whose_text_cannot_be_read_withholds_the_record() -> None:
    install_log_redaction()

    class Unreadable:
        def __str__(self) -> str:
            raise RuntimeError("no text")

    record = _build("got %s", (Unreadable(),))

    assert record.msg == "log record withheld: it could not be redacted"
    assert record.args == ()


def test_a_template_that_cuts_the_value_in_two_withholds_the_record(
    capsys: pytest.CaptureFixture[str],
) -> None:
    install_log_redaction()
    stream, logger = _handler_output("meridian.test.cut")

    logger.info("contact %s@example.com now", "kovacs.peter")

    captured = capsys.readouterr()
    assert "kovacs.peter" not in stream.getvalue()
    assert "kovacs.peter" not in captured.err
    assert "kovacs.peter" not in captured.out
    assert stream.getvalue() == "log record withheld: it could not be redacted\n"


def test_a_mapping_template_that_cuts_the_value_in_two_withholds_the_record(
    capsys: pytest.CaptureFixture[str],
) -> None:
    install_log_redaction()
    stream, logger = _handler_output("meridian.test.cutmap")

    logger.info("contact %(u)s@example.com now", {"u": "kovacs.peter"})

    captured = capsys.readouterr()
    assert "kovacs.peter" not in stream.getvalue()
    assert "kovacs.peter" not in captured.err
    assert "kovacs.peter" not in captured.out
    assert stream.getvalue() == "log record withheld: it could not be redacted\n"


def test_a_record_that_cannot_be_formatted_is_withheld_with_no_arguments() -> None:
    install_log_redaction()

    record = _build("contact %s@example.com now", ("kovacs.peter",))

    assert record.msg == logredaction.WITHHELD
    assert record.args == ()
    assert record.getMessage() == logredaction.WITHHELD


def test_a_number_that_looks_like_a_card_is_logged_as_the_number_it_is(
    capsys: pytest.CaptureFixture[str],
) -> None:
    install_log_redaction()
    stream, logger = _handler_output("meridian.test.number")

    logger.info("took %d ns", 4111111111111111)

    assert stream.getvalue() == "took 4111111111111111 ns\n"
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("value", [4111111111111111, 1.5, True, None])
def test_a_number_a_bool_and_none_stay_the_objects_they_were(value: object) -> None:
    install_log_redaction()

    record = _build("got %s", (value,))

    assert record.args[0] is value


def test_installing_twice_wraps_once() -> None:
    install_log_redaction()
    after_one = logging.getLogRecordFactory()

    install_log_redaction()

    assert logging.getLogRecordFactory() is after_one


def test_the_factory_it_replaced_still_builds_the_record() -> None:
    marker = "built-by-the-previous-factory"
    previous = logging.getLogRecordFactory()

    def factory(*args: object, **kwargs: object) -> logging.LogRecord:
        record = previous(*args, **kwargs)  # type: ignore[arg-type]
        record.marker = marker  # type: ignore[attr-defined]
        return record

    logging.setLogRecordFactory(factory)
    install_log_redaction()

    record = _build("mail %s", (EMAIL,))

    assert record.marker == marker  # type: ignore[attr-defined]
    assert record.getMessage() == "mail [email]"


@pytest.mark.parametrize("module_name", FACTORY_MODULES)
def test_each_service_factory_installs_it_before_reading_its_settings(
    module_name: str,
) -> None:
    # The factories read their settings from the environment, so they are not
    # called here: the source of each is checked instead.
    module = importlib.import_module(module_name)
    source = inspect.getsource(module.create_app_from_env)

    assert "install_log_redaction()" in source
    assert source.index("install_log_redaction()") < source.index(".from_env()")
    assert module.install_log_redaction is install_log_redaction
