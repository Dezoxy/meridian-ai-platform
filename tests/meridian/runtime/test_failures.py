"""Why a run failed: the reason word of an exception, and ``GraphFailure``."""

import pytest

from meridian.runtime.failures import GraphFailure, failure_reason
from meridian.runtime.model_client import (
    ModelCallError,
    ModelCallFilteredError,
    ModelCallLimitError,
    ModelCallTimeoutError,
)
from meridian.runtime.tool_client import (
    ToolCallLimit,
    ToolError,
    ToolNotAllowed,
    ToolRefused,
    ToolUnavailable,
)

MAX_CODE = "a" * 40


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (ModelCallLimitError(), "model-call-limit"),
        (ToolCallLimit("wording_search"), "tool-call-limit"),
        (ToolNotAllowed(None), "tool-not-allowed"),
        (ToolRefused("policy_lookup", "outside-claim"), "tool-refused"),
        (ToolRefused("policy_lookup", "unknown"), "tool-refused"),
        (ToolUnavailable("policy_lookup"), "tool-unavailable"),
        (ModelCallTimeoutError(), "model-timeout"),
        (ModelCallFilteredError(), "model-filtered"),
        (ModelCallError(400), "model-error"),
        (ModelCallError(502), "model-error"),
        (ModelCallError(0), "model-error"),
        (GraphFailure("missing-policy"), "missing-policy"),
        (ValueError("boom"), "unexpected"),
        (RuntimeError("boom"), "unexpected"),
        (ToolError("policy_lookup", "a tool error of no known kind"), "unexpected"),
    ],
    ids=lambda value: type(value).__name__ if isinstance(value, Exception) else value,
)
def test_each_failure_has_its_reason_word(error: Exception, reason: str) -> None:
    assert failure_reason(error) == reason


def test_the_limit_and_the_timeout_are_not_called_a_plain_model_error() -> None:
    # Both are subclasses of ModelCallError: a check in the wrong order would
    # give them its word.
    assert failure_reason(ModelCallLimitError()) != failure_reason(ModelCallError(0))
    assert failure_reason(ModelCallTimeoutError()) != failure_reason(ModelCallError(0))
    assert failure_reason(ModelCallFilteredError()) != failure_reason(ModelCallError(0))


def test_a_graph_failure_message_is_its_code() -> None:
    failure = GraphFailure("other-wording")

    assert (failure.code, str(failure)) == ("other-wording", "other-wording")


@pytest.mark.parametrize("code", ["a", "missing-policy", "a-b-c", MAX_CODE])
def test_a_code_of_lower_case_words_up_to_40_characters_is_accepted(code: str) -> None:
    assert GraphFailure(code).code == code


@pytest.mark.parametrize(
    "code",
    [
        "has space",
        "Upper",
        "a" * 41,
        "",
        "-leading",
        "trailing-",
        "double--hyphen",
        "under_score",
        "digit1",
        "line\nbreak",
        "trailing-newline\n",
    ],
    ids=[
        "space",
        "upper-case",
        "41-characters",
        "empty",
        "leading-hyphen",
        "trailing-hyphen",
        "double-hyphen",
        "underscore",
        "digit",
        "newline",
        "trailing-newline",
    ],
)
def test_a_code_that_is_not_a_fixed_word_is_refused(code: str) -> None:
    with pytest.raises(ValueError, match="failure code"):
        GraphFailure(code)


def test_the_refusal_of_a_code_does_not_repeat_it() -> None:
    # A code that is data could be claim text.
    with pytest.raises(ValueError, match="failure code") as raised:
        GraphFailure("Claimant Secret Text")

    assert "Secret" not in str(raised.value)
