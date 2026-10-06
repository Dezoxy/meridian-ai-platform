"""The Agent Runtime's metrics: runs and model calls (S064).

Two counters on the ``meridian.runtime`` meter, which reach Prometheus as
``meridian_runtime_runs_total`` and ``meridian_runtime_model_calls_total``.

``meridian.runtime.runs`` counts each leg of a run once, after its status was
written: a start and each resume are legs. A request refused before a run exists
(a caller the identity check refuses, a tenant that may not run the agent, an
agent that has no graph) counts nothing here; the audit row of the refusal is
where those are seen. A resumed leg that fails and leaves the run paused again
is one ``failed`` leg, the run's own status notwithstanding: the leg is what is
counted. The leg's own end is counted, not the stored status that a sweep may
have written over it, unless nothing could be written: then the leg is
``failed`` with ``not-saved``. A run the database refused before its first leg
(or a resume it refused to claim) is one ``failed`` count with ``not-started``.
A meter never raises into the work it counts (``counted_safely``).

``meridian.runtime.model_calls`` counts each call a graph asks of the model
client once, by outcome and, for a failure, a reason that tells a gateway that
could not be reached from one that timed out and from one that answered an error
(the run's failure word, ``model-error``, covers the first and the last).

Every label is a registry identifier or a word of a closed set the code defines
(T-03, T-49). A ``GraphFailure``'s code is text the workload chose, so it is
never a label: it is the one word ``graph-failure`` (the code stays in the audit
row and the log line).
"""

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import get_args

import psycopg
from opentelemetry.sdk.metrics import MeterProvider

from meridian.platform.common.metrics import counted_safely, metric_attributes
from meridian.runtime.failures import UNEXPECTED, GraphFailure, failure_reason
from meridian.runtime.model_client import CallObserver, CallOutcome, CallReason
from meridian.runtime.runs import RunIdentity, RunOutcome
from meridian.runtime.tool_client import ClientRefusal

logger = logging.getLogger(__name__)

METER_NAME = "meridian.runtime"
GRAPH_FAILURE = "graph-failure"
# The metric's own words, not a run's failure word (no audit row has them): the
# leg's final status could not be written, the run could not be started.
NOT_SAVED = "not-saved"
NOT_STARTED = "not-started"
# The words ``failure_reason`` gives for what the runtime itself raises, the
# one word for every ``GraphFailure`` and the two above. A new failure word is
# not a label until it is listed here (``run_reason`` reads any other as
# ``unexpected``).
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
        NOT_SAVED,
        NOT_STARTED,
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

    @counted_safely
    def leg_ended(
        self,
        identity: RunIdentity,
        outcome: RunOutcome,
        failure: Exception | None,
        saved: bool = True,
    ) -> None:
        """Count a leg, after its status was written: ``failed`` with its
        reason word when it raised, else ``paused`` or ``completed`` by the
        status the graph ended on; any other status is ``failed``,
        ``unexpected``. A leg whose status could not be written (``saved`` is
        false) is ``failed``, ``not-saved``, whatever it did."""
        attributes = {
            "meridian.tenant": identity.tenant,
            "meridian.agent": identity.agent,
        }
        if not saved:
            words = ("failed", NOT_SAVED)
        elif failure is not None:
            words = ("failed", run_reason(failure))
        elif outcome.status == "Completed":
            words = ("completed", None)
        elif outcome.status == "AwaitingApproval":
            words = ("paused", None)
        else:
            words = ("failed", UNEXPECTED)
        attributes["meridian.outcome"] = words[0]
        if words[1] is not None:
            attributes["meridian.reason"] = words[1]
        self._runs.add(1, metric_attributes(attributes))

    @counted_safely
    def not_started(self, tenant: str, agent: str) -> None:
        """Count a run the database refused before a leg existed. The tenant
        and the agent are ones the registry holds: the caller checked them."""
        self._runs.add(
            1,
            metric_attributes(
                {
                    "meridian.tenant": tenant,
                    "meridian.agent": agent,
                    "meridian.outcome": "failed",
                    "meridian.reason": NOT_STARTED,
                }
            ),
        )

    @contextmanager
    def start_counted(self, tenant: str, agent: str) -> Iterator[None]:
        """Count a database error in the block as a run that did not start,
        and let it go on to its answer."""
        try:
            yield
        except psycopg.Error:
            self.not_started(tenant, agent)
            raise

    def model_call_observer(self, identity: RunIdentity) -> CallObserver:
        """The callback of one run's ``ModelClient``. The tenant and the agent
        are the run's own, which ``create_run`` checked against the registry."""

        @counted_safely
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


def shut_down(provider: MeterProvider) -> None:
    """Shut a provider down, which sends what it holds. A failure is one
    WARNING with its class and stops nothing that follows."""
    try:
        provider.shutdown()
    except Exception as exc:
        logger.warning(
            "the meter provider did not shut down cleanly: %s", type(exc).__name__
        )
