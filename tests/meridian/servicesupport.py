"""Helpers shared by the tests of the three services (S009)."""

import json
import re
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
    "output": {"text": "drafted", "finish_reason": "stop"},
    "usage": {"input_tokens": 1, "output_tokens": 1},
}

# The deployment after the Azure ones: where a planted deployment goes.
REPLAY_ENTRY = "  - id: replay-chat\n"
CHAT_ROUTE = re.compile(r"(- purpose: chat\n\s+candidates: )\[[^\]]*\]")


def azure_chat_deployment_yaml(
    deployment_id: str,
    deployment_name: str,
    *,
    sku: str,
    residency: str,
    data_classes: str,
) -> str:
    """A chat deployment for ``models.yaml`` that passes the registry checks,
    ending in the blank line that separates entries."""
    return f"""\
  - id: {deployment_id}
    provider: azure-openai
    purpose: chat
    model: gpt-4o
    version: "2024-11-20"
    deployment_name: {deployment_name}
    sku: {sku}
    region: swedencentral
    residency: {residency}
    data_classes: {data_classes}
    retires: 2027-04-14
    price:
      currency: USD
      input_per_million_tokens: 2.5
      output_per_million_tokens: 10
      source: "a test fixture"
      checked: 2026-09-30
    terraform_key: sdc/{deployment_name}
    rate_limits:
      requests_per_10_seconds: 20
      tokens_per_minute: 20000

"""


# A test-only global deployment: synthetic data only.
GLOBAL_DEPLOYMENT_YAML = azure_chat_deployment_yaml(
    "aoai-sdc-gpt-4o-global",
    "gpt-4o-global",
    sku="GlobalStandard",
    residency="global",
    data_classes="[synthetic]",
)
# A test-only second EU deployment, a copy of the real one under another name.
SECOND_DEPLOYMENT_YAML = azure_chat_deployment_yaml(
    "aoai-sdc-gpt-4o-second",
    "gpt-4o-second",
    sku="Standard",
    residency="eu-region",
    data_classes="[synthetic, internal, personal]",
)


def pin_chat_route(registry_dir: Path, *candidates: str) -> Path:
    """Rewrite the chat route's ``candidates:`` line in a planted registry, so a
    test does not depend on how many deployments the real route lists."""
    path = registry_dir / "policies.yaml"
    pinned, count = CHAT_ROUTE.subn(
        lambda m: f"{m[1]}[{', '.join(candidates)}]",
        path.read_text(encoding="utf-8"),
    )
    assert count == 1, f"policies.yaml has {count} chat routes, not one"
    path.write_text(pinned, encoding="utf-8")
    return registry_dir


class FakeClock:
    """A clock a test moves by hand, for the gateway's breaker and deadline."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


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
    "reason",
    "data_class",
    "sku",
    "region",
    "residency",
    "call_id",
    "http_status",
    "provider_model",
    "suppressed",
    "tool",
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
        "deployment, provider, model, input_tokens, output_tokens, "
        "reason, data_class, sku, region, residency, "
        "call_id, http_status, provider_model, suppressed, tool "
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
