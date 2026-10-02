"""Bind a tool call to the run's own record (T-22).

The caller sends only the run ID. The tenant, the agent and the claim come
from the runtime's run row and the Claims API's claim row, which the tool
servers' roles may read through column grants (T-25): never ``SELECT *``.
"""

import uuid
from dataclasses import dataclass, replace

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

SELECT_POLICY_SCOPE = """
SELECT product, wording_version
FROM policy.policies
WHERE policy_number = %s
"""


@dataclass(frozen=True, slots=True)
class RunBinding:
    """What a call is allowed to touch: this run's claim and its policy.

    ``product`` and ``wording_version`` are the policy's, set only for a tool
    bound to the product (``with_policy_scope``): the claims role has no grant
    on ``policy.policies``, so they are not read for any other tool."""

    run_id: uuid.UUID
    tenant: str
    agent: str
    claim_id: str
    policy_number: str
    product: str | None = None
    wording_version: str | None = None


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


def with_policy_scope(
    conn: psycopg.Connection, binding: RunBinding
) -> RunBinding | BindingRefused:
    """The binding with its policy's product and wording version, or a refusal
    when the policy has no row. Neither is ever a caller's argument (T-22)."""
    row = conn.execute(SELECT_POLICY_SCOPE, (binding.policy_number,)).fetchone()
    if row is None:
        return BindingRefused(
            "policy-not-found",
            binding.run_id,
            binding.tenant,
            binding.agent,
            binding.claim_id,
        )
    product, wording_version = row
    return replace(binding, product=product, wording_version=wording_version)
