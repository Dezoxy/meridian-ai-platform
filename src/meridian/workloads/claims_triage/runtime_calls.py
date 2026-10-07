"""The Claims API's calls to the Agent Runtime (moved from ``triaging.py``, S037).

``triaging.py`` stood at 799 lines, one under the ceiling, when a run had to be
startable for an agent other than the triage's; the calls moved here, and
``triaging`` still offers every name it offered. A call posts to the runtime
with the trace context and raises ``RuntimeCallError`` for any failure; its
message is fixed text, never the runtime's body or the claim's.
"""

import logging
from typing import Any, get_args
from uuid import UUID

import httpx
from opentelemetry import propagate
from pydantic import ValidationError

from meridian.platform.common.runwire import RunResponse, RunState
from meridian.workloads.claims_triage.lifecycle import (
    AGENT,
    RUNTIME_CONNECT_TIMEOUT_SECONDS,
    RUNTIME_POOL_TIMEOUT_SECONDS,
    RUNTIME_TIMEOUT_SECONDS,
    RUNTIME_WRITE_TIMEOUT_SECONDS,
)
from meridian.workloads.claims_triage.meters import TriageFailure
from meridian.workloads.claims_triage.models import invalid_fields

# Ending a run is best effort (the claim's move stands), and ``add_documents``
# and ``triage_again`` run a new triage after it, one after the other, inside
# one lease (``TRIAGE_LEASE_SECONDS``), so this call must stay short: its read
# timeout, the other phases being the same.
END_RUN_TIMEOUT_SECONDS = 15.0
HTTP_GATEWAY_TIMEOUT = 504

logger = logging.getLogger(__name__)


def runtime_timeout(read_seconds: float = RUNTIME_TIMEOUT_SECONDS) -> httpx.Timeout:
    """The timeout of a call to the runtime: a value for each phase, so a
    connection that never opens does not wait as long as a slow answer. The
    values bound each phase, not the call: connect is given twice over TLS and a
    read is each wait for bytes. A runtime that answers in one piece is waited
    for at most pool + 2 x connect + write + read."""
    return httpx.Timeout(
        connect=RUNTIME_CONNECT_TIMEOUT_SECONDS,
        read=read_seconds,
        write=RUNTIME_WRITE_TIMEOUT_SECONDS,
        pool=RUNTIME_POOL_TIMEOUT_SECONDS,
    )


class RuntimeCallError(Exception):
    """The runtime gave no usable answer to a call.

    Carries the HTTP status it answered (0: none, it timed out, was unreachable
    or answered outside its contract), its run ID when it named one and the
    run's status when its error body gave one of the four (``run_status``: a
    resume that ended the run answers 502 with ``Failed``, one that left it paused
    answers 502 with ``AwaitingApproval``). The message is fixed text: neither the
    runtime's body nor the claim is kept. ``failure`` is the word the triage's
    metric counts it under (``meters.py``)."""

    def __init__(
        self,
        reason: str,
        *,
        status_code: int = 0,
        run_id: UUID | None = None,
        run_status: RunState | None = None,
        timed_out: bool = False,
        failure: TriageFailure = "runtime-failed",
    ) -> None:
        super().__init__(reason)
        self.status_code = status_code
        self.run_id = run_id
        self.run_status = run_status
        self.timed_out = timed_out or status_code == HTTP_GATEWAY_TIMEOUT
        self.failure: TriageFailure = failure


def _run_id_in(response: httpx.Response) -> UUID | None:
    try:
        return UUID(response.json()["run_id"])
    except (ValueError, KeyError, TypeError, AttributeError):
        return None


def _run_status_in(response: httpx.Response) -> RunState | None:
    """The run's status an error answer names, when it is one of the four words."""
    try:
        status = response.json()["status"]
    except (ValueError, KeyError, TypeError, AttributeError):
        return None
    return status if status in get_args(RunState) else None


def _call_runtime(
    http: httpx.Client,
    path: str,
    body: dict[str, Any],
    timeout: httpx.Timeout | None = None,
) -> RunResponse:
    """Post to the runtime with the trace context; raise ``RuntimeCallError``
    for any failure. ``timeout`` replaces the client's for this call."""
    headers: dict[str, str] = {}
    propagate.inject(headers)
    # ``timeout=None`` would mean no timeout to httpx, so none is passed.
    options: dict[str, Any] = {} if timeout is None else {"timeout": timeout}
    try:
        response = http.post(path, json=body, headers=headers, **options)
    except httpx.TimeoutException:
        raise RuntimeCallError(
            "the runtime timed out", timed_out=True, failure="runtime-timeout"
        ) from None
    except httpx.HTTPError:
        raise RuntimeCallError(
            "the runtime is unreachable", failure="runtime-unreachable"
        ) from None
    if not 200 <= response.status_code < 300:
        raise RuntimeCallError(
            "the runtime answered an error",
            status_code=response.status_code,
            run_id=_run_id_in(response),
            run_status=_run_status_in(response),
        )
    try:
        body = response.json()
    except ValueError as exc:
        # Nothing of the body: its text is the runtime's, and may quote the run.
        logger.warning("the runtime's answer is not JSON (%s)", type(exc).__name__)
        raise RuntimeCallError(
            "the runtime answered outside its contract", failure="bad-output"
        ) from None
    try:
        return RunResponse.model_validate(body)
    except ValidationError as exc:
        logger.warning(
            "the runtime's answer is not a run: %s %s",
            type(exc).__name__,
            invalid_fields(exc),
        )
        raise RuntimeCallError(
            "the runtime answered outside its contract", failure="bad-output"
        ) from None


def start_run(
    http: httpx.Client,
    tenant: str,
    reference: str,
    run_input: dict[str, Any],
    agent: str = AGENT,
) -> RunResponse:
    """Start a run of ``agent`` (the triage's, unless one is named) with
    ``run_input`` as the run's whole input, sent as it is given: each caller
    builds its own (the triage's is ``triaging.triage_run_input``, the brief's is
    ``{"claim": …}``), as the two workflows read different fields."""
    return _call_runtime(
        http,
        "/runs",
        {
            "agent": agent,
            "tenant": tenant,
            "reference": reference,
            "input": run_input,
        },
    )


def resume_run(
    http: httpx.Client,
    tenant: str,
    claim_id: str,
    run_id: UUID,
    timeout: httpx.Timeout | None = None,
) -> RunResponse:
    """Resume the paused run. The resume carries no decision: the run reads the
    one recorded here (T-31), so a caller of the runtime cannot make one up."""
    return _call_runtime(
        http,
        f"/runs/{run_id}/resume",
        {"tenant": tenant, "reference": claim_id, "input": {}},
        timeout,
    )


def end_run(
    http: httpx.Client, tenant: str, claim_id: str, run_id: UUID
) -> RunState | None:
    """End a paused run by resuming it: the run reads the word recorded for it
    and completes. Best effort: the run's status, or ``None`` when the call
    failed, after logging the claim, the run, the exception's class and fixed
    message (``RuntimeCallError`` messages are fixed text, never the runtime's
    body or the claim's), whether it timed out, and the runtime's status. A run
    that answered without completing is a run left behind: it is logged at
    ERROR too, with the IDs and its status only. The claim's move stands either
    way."""
    try:
        run = resume_run(
            http, tenant, claim_id, run_id, runtime_timeout(END_RUN_TIMEOUT_SECONDS)
        )
    except RuntimeCallError as exc:
        logger.error(
            "ending run %s of claim %s failed: %s: %s "
            "(runtime status %s, timed_out=%s)",
            run_id,
            claim_id,
            type(exc).__name__,
            str(exc),
            exc.status_code,
            exc.timed_out,
        )
        return None
    if run.status != "Completed":
        logger.error(
            "ending run %s of claim %s: run status %s", run_id, claim_id, run.status
        )
    return run.status
