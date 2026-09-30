"""Helpers shared by the tests of the three services (S009)."""

import json
import uuid
from pathlib import Path

import psycopg
from dbsupport import OWNER, DatabaseHandle
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from meridian.platform.common.db import connect

REPO_ROOT = Path(__file__).resolve().parents[2]
# Where the test suite's own stand-in graphs live: outside the meridian package.
TESTS_ROOT = REPO_ROOT / "tests"
REGISTRY_DIR = REPO_ROOT / "config" / "registry"
CLAIMS_JSON = REPO_ROOT / "data" / "synthetic" / "claims.json"

# What the gateway answers a chat call with, for the tests that stand in for it.
GATEWAY_REPLY = {
    "call_id": str(uuid.uuid4()),
    "mode": "replay",
    "deployment": "replay-chat",
    "provider": "replay",
    "model": "replay-chat",
    "output": {"text": "drafted"},
    "usage": {"input_tokens": 1, "output_tokens": 1},
}

AUDIT_COLUMNS = (
    "service",
    "event",
    "outcome",
    "tenant",
    "agent",
    "run_id",
    "reference",
    "deployment",
    "provider",
    "model",
    "input_tokens",
    "output_tokens",
)


def synthetic_claims() -> list[dict]:
    return json.loads(CLAIMS_JSON.read_text(encoding="utf-8"))


def claim_with_id(claim_id: str, index: int = 0) -> dict:
    """A synthetic claim from claims.json under another claim ID."""
    return {**synthetic_claims()[index], "claim_id": claim_id}


def owner_rows(db: DatabaseHandle, statement: str, params: tuple = ()) -> list[tuple]:
    """Read as the owner role, which sees every schema."""
    with connect(db.dsn(OWNER), "test-read") as conn:
        return conn.execute(statement, params).fetchall()


def audit_events(db: DatabaseHandle, run_id: uuid.UUID) -> list[dict]:
    """The audit rows of one run, oldest first, as column-name dicts."""
    rows = owner_rows(
        db,
        "SELECT service, event, outcome, tenant, agent, run_id, reference, "
        "deployment, provider, model, input_tokens, output_tokens "
        "FROM audit.events WHERE run_id = %s ORDER BY recorded_at, event",
        (run_id,),
    )
    return [dict(zip(AUDIT_COLUMNS, row, strict=True)) for row in rows]


def database_error(canary: str) -> psycopg.Error:
    """A psycopg error whose message quotes ``canary``, as PostgreSQL's does
    for the value it refused (``Key (email)=(...)``)."""
    return psycopg.errors.NotNullViolation(f"refused value: {canary}")


def assert_spans_hold_no_exception_and_no_canary(
    exporter: InMemorySpanExporter, canary: str
) -> None:
    """No finished span has an ``exception`` event, and nothing a span carries
    (status description, attributes, events) contains ``canary`` (T-03)."""
    spans = exporter.get_finished_spans()
    assert spans, "no span was finished, so nothing was checked"
    for span in spans:
        assert [e.name for e in span.events if e.name == "exception"] == [], span.name
        carried = [
            span.status.description or "",
            *map(str, span.attributes.values()),
            *(str(v) for e in span.events for v in e.attributes.values()),
        ]
        assert canary not in " ".join(carried), span.name
