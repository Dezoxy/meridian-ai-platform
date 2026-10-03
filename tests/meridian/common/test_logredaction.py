"""The process's log records carry no personal identifier (S047, T-03).

Every test restores the record factory it found, so no other test sees the one
installed here.
"""

import importlib
import inspect
import logging
from collections.abc import Iterator

import pytest

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


def test_a_redacted_record_has_no_arguments_left() -> None:
    install_log_redaction()

    record = _build("claimant %s wrote", (EMAIL,))

    assert record.msg == "claimant [email] wrote"
    assert record.args == ()


def test_a_record_with_nothing_to_redact_is_untouched() -> None:
    install_log_redaction()
    msg = "claim %s moved to %s"
    args = ("c-1", "review")

    record = _build(msg, args)

    assert record.msg is msg
    assert record.args is args
    assert record.getMessage() == "claim c-1 moved to review"


def test_a_record_whose_formatting_fails_is_left_as_it_is() -> None:
    install_log_redaction()
    msg = "count %d"
    args = (EMAIL,)

    record = _build(msg, args)

    assert record.msg is msg
    assert record.args is args
    with pytest.raises(TypeError):
        record.getMessage()


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
