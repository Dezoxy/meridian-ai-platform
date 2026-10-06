"""Why a run failed, as a fixed word (S014).

A failed run used to log its exception's class name, and its ``run.failed``
audit row said nothing: ``ValueError`` stood for six different causes in the
triage graph, and a run stopped by a call limit could not be told from any other.
``failure_reason`` gives every failure one word from a closed set, so the log and
the audit row can say why without holding what the exception says: an exception's
text can quote claim text or a tool result (T-03, T-25).
"""

import re

from meridian.runtime.model_client import (
    ModelCallError,
    ModelCallFilteredError,
    ModelCallLimitError,
    ModelCallTimeoutError,
)
from meridian.runtime.tool_client import (
    ToolCallLimit,
    ToolNotAllowed,
    ToolRefused,
    ToolUnavailable,
)

# Lower-case words joined by single hyphens, so a code reads as a reason and
# cannot hold a sentence, a number or an identifier.
CODE_PATTERN = re.compile(r"[a-z]+(-[a-z]+)*")
MAX_CODE_CHARS = 40
UNEXPECTED = "unexpected"


class GraphFailure(Exception):
    """A graph's own contract violation, raised by its code.

    ``code`` is a fixed word of the graph's source, never data: it is logged and
    stored in an audit row as it is. The message is the code, and the error for
    a code that does not fit does not repeat it.
    """

    def __init__(self, code: str) -> None:
        # fullmatch, and not $: $ also matches before a trailing newline.
        if len(code) > MAX_CODE_CHARS or CODE_PATTERN.fullmatch(code) is None:
            raise ValueError(
                "a failure code is lower-case words joined by hyphens, "
                f"at most {MAX_CODE_CHARS} characters"
            )
        super().__init__(code)
        self.code = code


def failure_reason(error: BaseException) -> str:
    """The reason word of the exception that failed a run. A subclass is checked
    before its base: the call limit, the timeout and the content filter are model
    call errors."""
    if isinstance(error, ModelCallLimitError):
        return "model-call-limit"
    if isinstance(error, ToolCallLimit):
        return "tool-call-limit"
    if isinstance(error, ToolNotAllowed):
        return error.reason
    if isinstance(error, ToolRefused):
        return "tool-refused"
    if isinstance(error, ToolUnavailable):
        return "tool-unavailable"
    if isinstance(error, ModelCallTimeoutError):
        return "model-timeout"
    if isinstance(error, ModelCallFilteredError):
        return "model-filtered"
    if isinstance(error, ModelCallError):
        return "model-error"
    if isinstance(error, GraphFailure):
        return error.code
    return UNEXPECTED
