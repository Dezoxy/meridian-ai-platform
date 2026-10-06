"""The tool servers' metrics: calls by tool, outcome and reason (S064).

One counter on the ``meridian.toolserver`` meter, ``meridian.toolserver.calls``,
which reaches Prometheus as ``meridian_toolserver_calls_total`` under the
server's own ``job`` (``policy-mcp``, ``claims-mcp``, ``knowledge-mcp``). It is
added once for every ``tools/call`` that reaches the kit, where the call's span
gets its attributes (``server._span_attributes``): the one place every end of a
call passes, a refusal, a failure and a handler's own words alike.

Every label is a registry ID or a word of a closed set the code defines (T-03,
T-49, T-86):

- ``meridian.tool``: a tool of this server's registry entry. A name the caller
  sent that is not one is never a label (the call is counted without it).
- ``meridian.outcome``: ``Outcome``, the four words the kit ends a call in.
- ``meridian.reason``: for a refused call a word of ``RefusalReason``, for a
  failed one a word of ``FailureReason``. A handler may pass any text in a
  ``Refused`` or a ``ToolFailed`` (the types are not enforced at run time), so
  a word outside the set of its outcome is the one word ``unlisted``. A call
  that completed has no reason.
- ``meridian.tenant`` and ``meridian.agent``: the run row's, once the kit has
  read it, and only when the registry holds the ID (a refusal before the run
  was read has neither).

Not labels: the worker (on the span and the audit row), an argument, the run's
ID, the idempotency key.
"""

from collections.abc import Collection
from typing import Any, get_args

from opentelemetry.sdk.metrics import MeterProvider

from meridian.platform.common.metrics import metric_attributes
from meridian.platform.registry import Registry
from meridian.platform.toolserver.pipeline import FailureReason, Finished, Outcome
from meridian.platform.toolserver.wire import RefusalReason

METER_NAME = "meridian.toolserver"
CALLS = "meridian.toolserver.calls"
# The one word for a reason a handler made up: text outside the closed set of
# its call's outcome.
UNLISTED = "unlisted"


def _words(alias: Any) -> frozenset[str]:
    """The words of a ``Literal``, or of a union of them."""
    return frozenset(
        word for part in get_args(alias) for word in (get_args(part) or (part,))
    )


OUTCOMES = _words(Outcome)
REFUSAL_REASONS = _words(RefusalReason)
FAILURE_REASONS = _words(FailureReason)


def _reason(finished: Finished) -> str | None:
    """The reason's label: the word itself when it is one of the set of the
    call's outcome, ``unlisted`` for any other text, None for a call that
    completed."""
    if finished.outcome == "refused":
        words = REFUSAL_REASONS
    elif finished.outcome == "failed":
        words = FAILURE_REASONS
    else:
        return None
    return finished.reason if finished.reason in words else UNLISTED


class ToolServerMeters:
    """The counter of one tool server, on the meter provider of that server.

    ``tools`` are the registry tools this server serves: the only values the
    tool label takes."""

    def __init__(
        self, meter_provider: MeterProvider, registry: Registry, tools: Collection[str]
    ) -> None:
        self._registry = registry
        self._tools = frozenset(tools)
        self._calls = meter_provider.get_meter(METER_NAME).create_counter(
            CALLS,
            unit="{call}",
            description=(
                "Tool calls, once each when the call ends, by tool, outcome "
                "(completed, replayed, refused or failed) and, for a refusal or "
                "a failure, its reason word."
            ),
        )

    def call_ended(self, finished: Finished) -> None:
        call = finished.call
        attributes = {"meridian.outcome": finished.outcome}
        if call.tool in self._tools:
            attributes["meridian.tool"] = str(call.tool)
        if (reason := _reason(finished)) is not None:
            attributes["meridian.reason"] = reason
        # The run row's values, labels only when the registry holds the ID: the
        # row is read from a database the registry does not own.
        if call.tenant and self._registry.tenant(call.tenant):
            attributes["meridian.tenant"] = call.tenant
        if call.agent and self._registry.agent(call.agent):
            attributes["meridian.agent"] = call.agent
        self._calls.add(1, metric_attributes(attributes))
