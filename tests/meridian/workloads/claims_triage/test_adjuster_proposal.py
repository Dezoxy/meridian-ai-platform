"""``GET /adjuster/claims/{claim_id}/proposal`` (S050, T-79): the stored
proposal as JSON, for the evaluation of a deployed stack, and nothing of the
claimant.

The first test goes through the real services (``stacksupport.build_stack``);
the rest put rows in the database and read them back.
"""

from datetime import timedelta

from dbsupport import DatabaseHandle
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from servicesupport import owner_rows
from stacksupport import CLAIMS, Stack, build_stack, stored_proposals
from workloads.claims_triage.test_adjuster_pages import (
    CANARY,
    CLAIMANT_EMAIL,
    CLAIMANT_NAME,
    LONG_AGO,
    RICH_PROPOSAL,
    SECURITY_HEADERS,
    client_for,
    put_claim,
    put_proposal,
)
from workloads.claims_triage.test_claims_app import (
    OTHER_TENANT,
    OUTPUT,
    claims_dsn,
    make_client,
)

from meridian.workloads.claims_triage.adjuster import NO_SUCH_CLAIM_DETAIL
from meridian.workloads.claims_triage.proposal import TriageProposal

CLAIM = "CLM-9301"
GOLDEN_CLAIM = "CLM-0001"
DATABASE_DOWN = "the database is unavailable"
ANSWER_KEYS = {"claim_id", "state", "proposal"}


def proposal_url(claim_id: str) -> str:
    return f"/adjuster/claims/{claim_id}/proposal"


def get_proposal(db: DatabaseHandle, claim_id: str = CLAIM):
    return client_for(db).get(proposal_url(claim_id))


def row_counts(db: DatabaseHandle) -> list[int]:
    """The rows of every table a read must leave alone."""
    tables = ("audit.events", "claims.claims", "claims.decisions")
    counts = []
    for table in tables:
        ((count,),) = owner_rows(db, f"SELECT count(*) FROM {table}")  # noqa: S608
        counts.append(int(count))
    return counts


def test_a_triaged_golden_claim_answers_its_stored_proposal_and_nothing_of_the_claimant(
    fresh_database: DatabaseHandle,
) -> None:
    stack: Stack = build_stack(fresh_database)
    posted = stack.post(CLAIMS[GOLDEN_CLAIM])
    assert posted.status_code == 201, posted.text
    claim = CLAIMS[GOLDEN_CLAIM]

    response = stack.client.get(proposal_url(GOLDEN_CLAIM))

    stored = stored_proposals(fresh_database)[GOLDEN_CLAIM]
    assert response.status_code == 200
    body = response.json()
    assert set(body) == ANSWER_KEYS
    assert body["claim_id"] == GOLDEN_CLAIM
    assert body["state"] == posted.json()["state"]
    assert body["proposal"] == stored.model_dump(mode="json")
    assert TriageProposal.model_validate(body["proposal"]) == stored
    for word in (
        claim["claimant"]["name"],
        claim["claimant"]["email"],
        claim["description"],
        claim["policy_number"],
    ):
        assert word not in response.text
    assert response.headers["content-type"] == "application/json"
    for name, value in SECURITY_HEADERS.items():
        assert response.headers[name] == value


def test_a_stored_proposal_is_answered_as_it_is_stored(
    fresh_database: DatabaseHandle,
) -> None:
    put_claim(fresh_database, CLAIM, "awaiting_adjuster", description=CANARY)
    put_proposal(fresh_database, CLAIM, RICH_PROPOSAL)

    response = get_proposal(fresh_database)

    assert response.status_code == 200
    assert response.json() == {
        "claim_id": CLAIM,
        "state": "awaiting_adjuster",
        "proposal": TriageProposal.model_validate(RICH_PROPOSAL).model_dump(
            mode="json"
        ),
    }
    for word in (CLAIMANT_NAME, CLAIMANT_EMAIL, CANARY):
        assert word not in response.text


def test_the_latest_proposal_is_the_one_answered(
    fresh_database: DatabaseHandle,
) -> None:
    put_claim(fresh_database, CLAIM)
    put_proposal(fresh_database, CLAIM, OUTPUT, created_at=LONG_AGO)
    put_proposal(
        fresh_database,
        CLAIM,
        RICH_PROPOSAL,
        created_at=LONG_AGO + timedelta(hours=1),
    )

    body = get_proposal(fresh_database).json()

    assert body["proposal"]["reason"] == RICH_PROPOSAL["reason"]
    assert body["proposal"]["reason"] != OUTPUT["reason"]


def test_a_claim_with_no_proposal_yet_answers_null(
    fresh_database: DatabaseHandle,
) -> None:
    put_claim(fresh_database, CLAIM, "triage_failed")

    response = get_proposal(fresh_database)

    assert response.status_code == 200
    assert response.json() == {
        "claim_id": CLAIM,
        "state": "triage_failed",
        "proposal": None,
    }


def test_a_row_with_no_document_answers_null(fresh_database: DatabaseHandle) -> None:
    put_claim(fresh_database, CLAIM)
    put_proposal(fresh_database, CLAIM, None)

    assert get_proposal(fresh_database).json()["proposal"] is None


def test_a_stored_proposal_that_fails_validation_answers_null_and_leaks_nothing(
    fresh_database: DatabaseHandle,
) -> None:
    put_claim(fresh_database, CLAIM)
    put_proposal(
        fresh_database,
        CLAIM,
        {"route": "adjuster", "reason": "unverified", "rationale": CANARY},
    )

    response = get_proposal(fresh_database)

    assert response.status_code == 200
    assert response.json()["proposal"] is None
    assert CANARY not in response.text


def test_an_unknown_claim_and_another_tenants_claim_are_the_same_404(
    fresh_database: DatabaseHandle,
) -> None:
    put_claim(fresh_database, CLAIM, tenant=OTHER_TENANT)
    put_proposal(fresh_database, CLAIM, RICH_PROPOSAL)

    other = get_proposal(fresh_database, CLAIM)
    unknown = get_proposal(fresh_database, "CLM-9999")

    assert (other.status_code, unknown.status_code) == (404, 404)
    assert other.json() == unknown.json() == {"detail": NO_SUCH_CLAIM_DETAIL}
    assert other.headers["content-type"] == "application/json"
    assert other.headers["cache-control"] == "no-store"


def test_a_claim_id_that_is_not_one_is_422_and_reads_nothing() -> None:
    response = make_client().get(proposal_url("not-a-claim"))

    assert response.status_code == 422


def test_a_read_writes_no_audit_row_and_changes_no_row(
    fresh_database: DatabaseHandle,
) -> None:
    put_claim(fresh_database, CLAIM)
    put_proposal(fresh_database, CLAIM, RICH_PROPOSAL)
    before = row_counts(fresh_database)

    get_proposal(fresh_database)
    get_proposal(fresh_database, "CLM-9999")

    assert row_counts(fresh_database) == before


def test_the_database_down_is_the_json_503_the_other_routes_give() -> None:
    response = make_client().get(proposal_url(CLAIM))

    assert response.status_code == 503
    assert response.json() == {"detail": DATABASE_DOWN}
    assert response.headers["content-type"] == "application/json"


def test_the_read_has_a_span_with_the_claim_and_the_tenant_and_nothing_else(
    fresh_database: DatabaseHandle,
) -> None:
    put_claim(fresh_database, CLAIM, description=CANARY)
    put_proposal(fresh_database, CLAIM, RICH_PROPOSAL)
    exporter = InMemorySpanExporter()
    client: TestClient = make_client(claims_dsn(fresh_database), exporter=exporter)

    client.get(proposal_url(CLAIM))

    (span,) = [
        s for s in exporter.get_finished_spans() if s.name == "claims.adjuster.proposal"
    ]
    assert dict(span.attributes or {}) == {
        "meridian.claim_id": CLAIM,
        "meridian.tenant": "claims-triage",
    }
    assert CANARY not in repr(span.to_json())


def test_the_proposal_is_not_read_under_claims() -> None:
    response = make_client().get(f"/claims/{CLAIM}/proposal")

    assert response.status_code in (404, 405)
