"""What the tests of the claim brief share (S037, A1): a stand-in runtime that
answers from a script, the client of the Claims API on it, the rows a test
plants and reads, and the ``world`` fixture, which fails a test whose calls
changed a claim.

The brief never moves a claim, so every test that uses ``world`` ends by
comparing each claim's whole row with the one it started with."""

import uuid
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest
from dbsupport import DatabaseHandle
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from psycopg.types.json import Jsonb
from servicesupport import claim_with_id, owner_rows

from meridian.platform.common.telemetry import make_tracer_provider
from meridian.runtime.sweep import RUNNING_LEASE_SECONDS
from meridian.workloads.claims_triage.app import create_app
from meridian.workloads.claims_triage.settings import ClaimsSettings

TENANT = "claims-triage"
OTHER_TENANT = "another-tenant"
CLAIM_ID = "CLM-9601"
OTHER_TENANTS_CLAIM_ID = "CLM-9602"
CANARY = "brief-canary-text-77"
BRIEF_TEXT = "A short brief about the claim, drafted from its peril and history."
UNUSED_DSN = "postgresql://claims_api@db.invalid/meridian"
JUST_INSIDE_THE_LEASE = RUNNING_LEASE_SECONDS - 60
JUST_OUTSIDE_THE_LEASE = RUNNING_LEASE_SECONDS + 60

Answer = tuple[int, dict[str, Any]] | Exception


def paused(run_id: uuid.UUID, brief: str = BRIEF_TEXT) -> Answer:
    """What the runtime answers a first leg that worked."""
    return 200, {
        "run_id": str(run_id),
        "status": "AwaitingApproval",
        "output": {"brief": brief},
    }


def completed(run_id: uuid.UUID, filed: bool | None, brief: str = BRIEF_TEXT) -> Answer:
    """What the runtime answers a resumed leg that worked."""
    output: dict[str, Any] = {"brief": brief}
    if filed is not None:
        output["filed"] = filed
    return 200, {"run_id": str(run_id), "status": "Completed", "output": output}


def answering(run_id: uuid.UUID, status: str, output: Any) -> Answer:
    return 200, {"run_id": str(run_id), "status": status, "output": output}


def failed(run_id: uuid.UUID) -> Answer:
    """A run that failed: 502 with the run's ID, as the real runtime does."""
    return 502, {"run_id": str(run_id), "status": "Failed", "output": None}


class Runtime:
    """A stand-in runtime that answers from a script, in order, and then repeats
    its last answer. ``during`` runs inside each call, before the answer, to
    stand in for whatever else happens while a request waits."""

    def __init__(
        self, *answers: Answer, during: Callable[[], None] | None = None
    ) -> None:
        self.answers = list(answers)
        self.during = during
        self.requests: list[httpx.Request] = []
        self.client = httpx.Client(
            base_url="http://runtime.invalid", transport=httpx.MockTransport(self)
        )

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.during:
            self.during()
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, Exception):
            raise answer
        status, body = answer
        return httpx.Response(status, json=body)


def make_client(
    dsn: str = UNUSED_DSN,
    runtime: Runtime | None = None,
    exporter: InMemorySpanExporter | None = None,
) -> TestClient:
    app = create_app(
        ClaimsSettings(
            runtime_url="http://runtime.invalid", database_url=dsn, tenant=TENANT
        ),
        tracer_provider=make_tracer_provider("claims-api", exporter),
        http_client=(runtime or Runtime((200, {}))).client,
    )
    return TestClient(app, raise_server_exceptions=False)


# ── rows to work on ─────────────────────────────────────────────────────────
def add_claim(
    db: DatabaseHandle, claim_id: str, tenant: str = TENANT, state: str = "triaging"
) -> dict[str, Any]:
    """A stored claim (a synthetic one) in ``state``, as the owner."""
    claim = claim_with_id(claim_id)
    owner_rows(
        db,
        "INSERT INTO claims.claims (claim_id, tenant, submission, state) "
        "VALUES (%s, %s, %s, %s) RETURNING 1",
        (claim_id, tenant, Jsonb(claim), state),
    )
    return claim


def add_brief(
    db: DatabaseHandle,
    state: str,
    *,
    claim_id: str = CLAIM_ID,
    tenant: str = TENANT,
    run_id: uuid.UUID | None = None,
    brief: str | None = BRIEF_TEXT,
    age_seconds: float = 0,
) -> uuid.UUID | None:
    """A brief in ``state`` since ``age_seconds`` ago, as the owner; returns the
    run it names. A state after drafting names a run and holds a text unless
    the test says otherwise."""
    if state == "drafting":
        brief = None
    elif run_id is None:
        run_id = uuid.uuid4()
    owner_rows(
        db,
        "INSERT INTO claims.briefs (claim_id, tenant, run_id, state, brief, "
        "created_at, state_changed_at) "
        "VALUES (%s, %s, %s, %s, %s, now() - make_interval(secs => %s), "
        "now() - make_interval(secs => %s)) RETURNING 1",
        (
            claim_id,
            tenant,
            run_id,
            state,
            brief,
            float(age_seconds),
            float(age_seconds),
        ),
    )
    return run_id


def brief_rows(db: DatabaseHandle) -> list[tuple]:
    """Every brief as ``(state, run_id, text)``, oldest first."""
    return owner_rows(
        db,
        "SELECT state, run_id, brief FROM claims.briefs ORDER BY created_at, brief_id",
    )


def decisions(db: DatabaseHandle) -> list[tuple]:
    return owner_rows(
        db,
        "SELECT claim_id, run_id, decision FROM claims.decisions ORDER BY decided_at",
    )


def brief_events(db: DatabaseHandle) -> list[tuple]:
    """The audit rows the brief wrote: event, outcome, reason, tenant, run,
    reference, role, service, agent."""
    return owner_rows(
        db,
        "SELECT event, outcome, reason, tenant, run_id, reference, db_role, "
        "service, agent FROM audit.events WHERE event LIKE 'brief.%%' "
        "ORDER BY recorded_at",
    )


def everything_audited(db: DatabaseHandle) -> str:
    """Every column of every audit row as one text."""
    rows = owner_rows(db, "SELECT * FROM audit.events")
    return " ".join(str(value) for row in rows for value in row)


def claim_row(db: DatabaseHandle, claim_id: str) -> tuple:
    ((row,),) = owner_rows(
        db, "SELECT c FROM claims.claims AS c WHERE claim_id = %s", (claim_id,)
    )
    return row


@pytest.fixture
def world(fresh_database: DatabaseHandle) -> Iterator[DatabaseHandle]:
    """A migrated database of its own that holds the tenant's claim and another
    tenant's, and fails the test when its calls changed either row."""
    add_claim(fresh_database, CLAIM_ID)
    add_claim(fresh_database, OTHER_TENANTS_CLAIM_ID, OTHER_TENANT)
    before = {
        claim: claim_row(fresh_database, claim)
        for claim in (CLAIM_ID, OTHER_TENANTS_CLAIM_ID)
    }
    yield fresh_database
    after = {claim: claim_row(fresh_database, claim) for claim in before}
    assert after == before, "a call of the claim brief changed a claim"
