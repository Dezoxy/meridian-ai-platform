"""Bind a tool call to the run's own record (T-22).

The caller sends only the run ID. The tenant, the agent and the claim come
from the runtime's run row and the Claims API's claim row, which the tool
servers' roles may read through column grants (T-25): never ``SELECT *``.
"""

import uuid
from dataclasses import dataclass

import psycopg

from meridian.platform.toolserver.wire import RefusalReason

RUNNING = "Running"

SELECT_RUN = """
SELECT agent, tenant, reference, status
FROM runtime.runs
WHERE run_id = %s
"""

SELECT_CLAIM = """
SELECT tenant, policy_number
FROM claims.claims
WHERE claim_id = %s
"""


@dataclass(frozen=True, slots=True)
class RunBinding:
    """What a call is allowed to touch: this run's claim and its policy."""

    run_id: uuid.UUID
    tenant: str
    agent: str
    claim_id: str
    policy_number: str


@dataclass(frozen=True, slots=True)
class BindingRefused:
    """Why there is no binding, and what was read before that was known: the
    audit row and the span of the refusal name the run. A field is ``None``
    when it was not read; the run's ID is the caller's, but a well-formed one
    only."""

    reason: RefusalReason
    run_id: uuid.UUID
    tenant: str | None = None
    agent: str | None = None
    reference: str | None = None


def read_binding(
    conn: psycopg.Connection, run_id: uuid.UUID
) -> RunBinding | BindingRefused:
    """The binding of a running run, or why there is none."""
    run = conn.execute(SELECT_RUN, (run_id,)).fetchone()
    if run is None:
        return BindingRefused("unknown-run", run_id)
    agent, tenant, reference, status = run
    if status != RUNNING:
        return BindingRefused("run-not-running", run_id, tenant, agent, reference)
    claim = conn.execute(SELECT_CLAIM, (reference,)).fetchone()
    if claim is None:
        return BindingRefused("claim-not-bound", run_id, tenant, agent, reference)
    claim_tenant, policy_number = claim
    if claim_tenant != tenant or policy_number is None:
        return BindingRefused("claim-not-bound", run_id, tenant, agent, reference)
    return RunBinding(
        run_id=run_id,
        tenant=tenant,
        agent=agent,
        claim_id=reference,
        policy_number=policy_number,
    )
