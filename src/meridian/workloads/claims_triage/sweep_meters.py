"""What a sweep pass found, as a metric (S064).

``meridian.sweep.last_pass`` is a gauge with one series for each number of
``PassResult``, under ``meridian.finding`` with a fixed word each; it reaches
Prometheus as ``meridian_sweep_last_pass``. A gauge and not a counter: the sweep
is a process that lives seconds, so a counter would export one sample per run
and start from zero every time, while the last pass's numbers are what a person
asks of it. It is a plain gauge, set once from the pass's result, and not an
observable one: the process has the numbers before it has a callback to ask for
them.

``report_pass`` is called by the sweep's ``main`` after the pass, on a provider
of its own (``make_meter_provider``), flushes it and shuts it down. It never
raises, and the sweep's exit code and its summary line are what they were
whatever happens. The sweep's own WARNING (one) is for what the SDK raises: a
provider that cannot be built, a reader that fails the flush or the shutdown. An
export the collector refuses or does not answer is not one of them: the SDK's
reader catches it and its exporter logs it itself (at ERROR, with the collector's
address), so the flush still returns. A pass that could not run (the database
was unreachable) has no result and reports nothing; the series then goes quiet.
Without ``OTEL_EXPORTER_OTLP_ENDPOINT`` the provider has no reader and nothing
is sent.

The bounds below cap the provider's loop over its readers and the join of the
reader's thread. What caps an export is the exporter's own deadline
(``OTEL_EXPORTER_OTLP_TIMEOUT``, 10 s by default, retries inside it): a
collector that does not answer cost about 12 s in all, measured once.

This module is imported by ``main`` when the pass is done, not by the module
that holds ``main``: ``make_meter_provider`` lives in a module that loads
Starlette, and the sweep is a job that loads no web stack (a test holds it).
"""

import logging

from opentelemetry.sdk.metrics import MeterProvider

from meridian.platform.common.env import SettingsError
from meridian.platform.common.metrics import make_meter_provider, metric_attributes
from meridian.workloads.claims_triage.lifecycle import (
    DOCUMENTS_OVERDUE,
    TRIAGE_ABANDONED,
    TRIAGE_NOT_STARTED,
)
from meridian.workloads.claims_triage.sweep import (
    FAILURES,
    RUNS_ENDED,
    SERVICE_NAME,
    THREADS_CLEANED,
    PassResult,
)

logger = logging.getLogger(__name__)

METER_NAME = "meridian.sweep"
# Each bound is far inside the CronJob's 120 s for a pass. The exporter keeps its
# own deadline (``OTEL_EXPORTER_OTLP_TIMEOUT``, 10 s by default), which these do
# not cut (see the docstring above).
FLUSH_TIMEOUT_MILLIS = 5_000
SHUTDOWN_TIMEOUT_MILLIS = 5_000


def findings(result: PassResult) -> dict[str, int]:
    """The pass's six numbers under the fixed word of each: the words of the
    summary line's counts, the trigger words of the claims' moves among them."""
    return {
        DOCUMENTS_OVERDUE.trigger: result.overdue,
        TRIAGE_NOT_STARTED.trigger: result.not_started,
        TRIAGE_ABANDONED.trigger: result.abandoned,
        RUNS_ENDED: result.runs_ended,
        THREADS_CLEANED: result.threads_cleaned,
        FAILURES: result.failures,
    }


def record_pass(provider: MeterProvider, result: PassResult) -> None:
    """Set the six gauges from the pass's result."""
    gauge = provider.get_meter(METER_NAME).create_gauge(
        "meridian.sweep.last_pass",
        unit="{item}",
        description=(
            "What the last sweep pass found, one series for each number of its "
            "summary line."
        ),
    )
    for word, number in findings(result).items():
        gauge.set(number, metric_attributes({"meridian.finding": word}))


def _send(provider: MeterProvider) -> str | None:
    """Flush and shut the provider down, each within its bound. ``None`` when
    both went well, else what failed first. Only a class name is kept: the
    SDK's message quotes the exporter's error, which can hold an address."""
    failure = None
    try:
        provider.force_flush(timeout_millis=FLUSH_TIMEOUT_MILLIS)
    except Exception as exc:
        failure = f"the flush failed ({type(exc).__name__})"
    try:
        provider.shutdown(timeout_millis=SHUTDOWN_TIMEOUT_MILLIS)
    except Exception as exc:
        failure = failure or f"the shutdown failed ({type(exc).__name__})"
    return failure


def report_pass(result: PassResult) -> None:
    """Record the pass and send it before the sweep exits; never raises."""
    try:
        provider = make_meter_provider(SERVICE_NAME)
    except SettingsError as exc:
        # Names the variable and never its value or a path (``require_otlp_ca``).
        logger.warning("the sweep's metrics were not sent: %s", exc)
        return
    except Exception as exc:
        logger.warning(
            "the sweep's metrics were not sent: no provider (%s)", type(exc).__name__
        )
        return
    failure: str | None
    try:
        record_pass(provider, result)
    except Exception as exc:
        failure = f"the pass could not be recorded ({type(exc).__name__})"
        _send(provider)
    else:
        failure = _send(provider)
    if failure is not None:
        logger.warning("the sweep's metrics were not sent: %s", failure)
