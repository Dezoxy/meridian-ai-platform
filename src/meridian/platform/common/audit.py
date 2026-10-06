"""Append events to ``audit.events``: identifiers and outcomes, no content.

The table has no column for claimant text, prompts or model output (T-03,
T-25), so there is nothing here to leak. Statements use psycopg placeholders
only (T-07).
"""

import uuid
from dataclasses import asdict, dataclass

import psycopg

from meridian.platform.common.db import connect

INSERT_EVENT = """
INSERT INTO audit.events (
    service, event, outcome, tenant, agent, run_id, reference,
    deployment, provider, model, input_tokens, output_tokens,
    reason, data_class, sku, region, residency,
    call_id, http_status, provider_model, suppressed, tool, purpose, worker
) VALUES (
    %(service)s, %(event)s, %(outcome)s, %(tenant)s, %(agent)s, %(run_id)s,
    %(reference)s, %(deployment)s, %(provider)s, %(model)s,
    %(input_tokens)s, %(output_tokens)s,
    %(reason)s, %(data_class)s, %(sku)s, %(region)s, %(residency)s,
    %(call_id)s, %(http_status)s, %(provider_model)s, %(suppressed)s, %(tool)s,
    %(purpose)s, %(worker)s
)
"""


class AuditUnavailable(Exception):
    """The audit event could not be written; the caller must fail closed
    (QA-05)."""


@dataclass(frozen=True, slots=True)
class AuditEvent:
    service: str
    event: str
    outcome: str
    tenant: str | None = None
    agent: str | None = None
    run_id: uuid.UUID | None = None
    reference: str | None = None
    deployment: str | None = None
    provider: str | None = None
    model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    # Why a call was refused or failed, and what route it took (S010, T-12).
    # ``reference`` stays the caller's own identifier.
    reason: str | None = None
    data_class: str | None = None
    sku: str | None = None
    region: str | None = None
    residency: str | None = None
    # The gateway's call identifier, the provider's status and the model name
    # the provider reported (S011).
    call_id: uuid.UUID | None = None
    http_status: int | None = None
    provider_model: str | None = None
    # On a refusal row only: the refusals of its tenant and reason since the
    # last row that wrote none (T-49).
    suppressed: int | None = None
    # The tool a tool server ran or refused (S013).
    tool: str | None = None
    # On a refusal row of the Model Gateway only: ``chat`` or ``embedding``,
    # for a refusal that names no deployment (S058).
    purpose: str | None = None
    # On a tool call's row, and on a refusal of one, written by the Agent
    # Runtime and the tool servers: the ID of the worker of the agent that made
    # the call, when it named one that the agent declares (S031).
    worker: str | None = None


def record_event(conn: psycopg.Connection, event: AuditEvent) -> None:
    """Insert in the caller's transaction, so it commits with the caller's work."""
    conn.execute(INSERT_EVENT, asdict(event))


def write_audit(dsn: str, event: AuditEvent) -> None:
    """Insert and commit in a connection of its own.

    Raises ``AuditUnavailable`` on any database error; the cause is chained but
    never put in a response.
    """
    try:
        with connect(dsn, event.service) as conn:
            record_event(conn, event)
    except psycopg.Error as exc:
        raise AuditUnavailable(type(exc).__name__) from exc
