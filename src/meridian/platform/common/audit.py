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
    deployment, provider, model, input_tokens, output_tokens
) VALUES (
    %(service)s, %(event)s, %(outcome)s, %(tenant)s, %(agent)s, %(run_id)s,
    %(reference)s, %(deployment)s, %(provider)s, %(model)s,
    %(input_tokens)s, %(output_tokens)s
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
