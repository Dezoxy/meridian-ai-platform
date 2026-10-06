"""The Agent Runtime's metrics: runs and model calls (S064).

Two counters on the ``meridian.runtime`` meter, which reach Prometheus as
``meridian_runtime_runs_total`` and ``meridian_runtime_model_calls_total``.

``meridian.runtime.runs`` counts each leg of a run once, when the leg ends: a
start and each resume are legs. A request refused before a run exists (a caller
the identity check refuses, a tenant that may not run the agent, an agent that
has no graph) counts nothing here; the audit row of the refusal is where those
are seen. A resumed leg that fails and leaves the run paused again is one
``failed`` leg, the run's own status notwithstanding: the leg is what is counted.
The leg's own end is counted, not the stored status that a sweep may have
written over it.

``meridian.runtime.model_calls`` counts each call a graph asks of the model
client once, by outcome and, for a failure, a reason that tells a gateway that
could not be reached from one that timed out and from one that answered an error
(the run's failure word, ``model-error``, covers the first and the last).

Every label is a registry identifier or a word of a closed set the code defines
(T-03, T-49). A ``GraphFailure``'s code is text the workload chose, so it is
never a label: it is the one word ``graph-failure`` (the code stays in the audit
row and the log line).
"""

from typing import get_args

from opentelemetry.sdk.metrics import MeterProvider

from meridian.platform.common.metrics import metric_attributes
from meridian.runtime.failures import UNEXPECTED, GraphFailure, failure_reason
from meridian.runtime.model_client import CallObserver, CallOutcome, CallReason
from meridian.runtime.runs import RunIdentity, RunOutcome
from meridian.runtime.tool_client import ClientRefusal

METER_NAME = "meridian.runtime"
GRAPH_FAILURE = "graph-failure"
# The words ``failure_reason`` gives for what the runtime itself raises, and the
# one word for every ``GraphFailure``. A new failure word is not a label until it
# is listed here (``run_reason`` reads any other as ``unexpected``).
RUN_FAILURE_REASONS: frozenset[str] = frozenset(
    {
        "model-call-limit",
        "tool-call-limit",
        *get_args(ClientRefusal),
        "tool-refused",
        "tool-unavailable",
        "model-timeout",
        "model-filtered",
        "model-error",
        UNEXPECTED,
        GRAPH_FAILURE,
    }
)


def run_reason(error: BaseException) -> str:
    """The label word of a failed leg: ``failure_reason``'s word when it is in
    the runtime's closed set, ``graph-failure`` for every ``GraphFailure``
    whatever its code, else ``unexpected``."""
    if isinstance(error, GraphFailure):
        return GRAPH_FAILURE
    reason = failure_reason(error)
    return reason if reason in RUN_FAILURE_REASONS else UNEXPECTED


class RuntimeMeters:
    def __init__(self, meter_provider: MeterProvider) -> None:
        meter = meter_provider.get_meter(METER_NAME)
        self._runs = meter.create_counter(
            "meridian.runtime.runs",
            unit="{leg}",
            description=(
                "Legs of runs, once each when the leg ends, by outcome: "
                "completed, paused or failed (with the failure's reason word)."
            ),
        )
        self._model_calls = meter.create_counter(
            "meridian.runtime.model_calls",
            unit="{call}",
            description=(
                "Calls a graph asked of the model client, once each, by outcome "
                "and, for a failure, why: unreachable, timeout, refused, "
                "filtered, error or limit."
            ),
        )

    def leg_ended(
        self, identity: RunIdentity, outcome: RunOutcome, failure: Exception | None
    ) -> None:
        """Count a leg: ``failed`` with its reason word when it raised, else
        ``paused`` or ``completed`` by the status the graph ended on."""
        attributes = {
            "meridian.tenant": identity.tenant,
            "meridian.agent": identity.agent,
        }
        if failure is not None:
            attributes["meridian.outcome"] = "failed"
            attributes["meridian.reason"] = run_reason(failure)
        elif outcome.status == "AwaitingApproval":
            attributes["meridian.outcome"] = "paused"
        else:
            attributes["meridian.outcome"] = "completed"
        self._runs.add(1, metric_attributes(attributes))

    def model_call_observer(self, identity: RunIdentity) -> CallObserver:
        """The callback of one run's ``ModelClient``. The tenant and the agent
        are the run's own, which ``create_run`` checked against the registry."""

        def observe(outcome: CallOutcome, reason: CallReason | None = None) -> None:
            attributes = {
                "meridian.tenant": identity.tenant,
                "meridian.agent": identity.agent,
                "meridian.outcome": outcome,
            }
            if reason is not None:
                attributes["meridian.reason"] = reason
            self._model_calls.add(1, metric_attributes(attributes))

        return observe
