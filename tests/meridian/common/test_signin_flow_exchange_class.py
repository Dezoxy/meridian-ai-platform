"""The line the flow logs when the code exchange fails names the exception's
class and never its text (S021, review L2): ``exchange-failed`` is one word for a
certificate error, a refused connection and a bad answer, and an operator needs
the class to tell them apart. The text can quote the address or the credential
the exchange was working with."""

import logging
import ssl

import httpx
import pytest
from signinflowsupport import NOW, FlowKit, answer

LOGGER = "meridian.platform.common.signinflow"
LATER = NOW + 5
MARK = "exchange-text-mark-77d1"


def failing_with(error: Exception) -> object:
    def raise_it(request: httpx.Request) -> httpx.Response:
        raise error

    return raise_it


def lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == LOGGER]


@pytest.mark.parametrize(
    ("error", "named"),
    [
        (httpx.ConnectError(f"cannot reach {MARK}"), "ConnectError"),
        (ssl.SSLError(f"bad certificate {MARK}"), "SSLError"),
        (RuntimeError(f"anything {MARK}"), "RuntimeError"),
    ],
)
def test_a_failed_exchange_names_the_class_of_what_failed_and_not_its_text(
    flow_kit: FlowKit,
    caplog: pytest.LogCaptureFixture,
    error: Exception,
    named: str,
) -> None:
    started = flow_kit.begin()
    flow_kit.endpoint.override = failing_with(error)

    with caplog.at_level(logging.DEBUG):
        outcome = flow_kit.flow.finish(
            flow_kit.callback(started), started.cookie, LATER
        )

    assert outcome.reason is not None
    assert outcome.reason.value == "exchange-failed"
    [line] = lines(caplog)
    assert line == f"sign-in callback refused: exchange-failed ({named})"
    assert MARK not in caplog.text
    assert MARK not in repr([r.args for r in caplog.records])


def test_the_class_line_is_still_one_per_window_with_the_count_carried(
    flow_kit: FlowKit, signin_clock: object, caplog: pytest.LogCaptureFixture
) -> None:
    started = flow_kit.begin()
    flow_kit.endpoint.override = failing_with(httpx.ConnectError("down"))

    with caplog.at_level(logging.INFO, logger=LOGGER):
        for _ in range(3):
            flow_kit.flow.finish(flow_kit.callback(started), started.cookie, LATER)
        signin_clock.advance(61)  # type: ignore[attr-defined]
        flow_kit.flow.finish(flow_kit.callback(started), started.cookie, LATER)

    assert lines(caplog) == [
        "sign-in callback refused: exchange-failed (ConnectError)",
        "sign-in callback refused: exchange-failed (ConnectError)"
        " (and 2 more since the last line)",
    ]


def test_another_refusal_has_no_class_in_its_line(
    flow_kit: FlowKit, caplog: pytest.LogCaptureFixture
) -> None:
    started = flow_kit.begin()

    with caplog.at_level(logging.INFO, logger=LOGGER):
        flow_kit.flow.finish(flow_kit.callback(started), None, LATER)

    assert lines(caplog) == ["sign-in callback refused: no-transaction"]


def test_an_answer_the_endpoint_refuses_is_named_by_its_class_too(
    flow_kit: FlowKit, caplog: pytest.LogCaptureFixture
) -> None:
    started = flow_kit.begin()
    flow_kit.endpoint.override = lambda _: answer(
        400, {"error": "invalid_grant", "error_description": MARK}
    )

    with caplog.at_level(logging.INFO, logger=LOGGER):
        flow_kit.flow.finish(flow_kit.callback(started), started.cookie, LATER)

    [line] = lines(caplog)
    assert line.startswith("sign-in callback refused: exchange-failed (")
    assert MARK not in caplog.text
