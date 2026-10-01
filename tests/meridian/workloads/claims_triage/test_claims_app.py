"""POST /claims with a stand-in runtime."""

import json
import logging
import uuid
from typing import Any

import httpx
import psycopg
import pytest
from dbsupport import DatabaseHandle
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from servicesupport import (
    assert_spans_hold_no_exception_and_no_canary,
    claim_with_id,
    database_error,
    owner_rows,
)

from meridian.platform.common.telemetry import make_tracer_provider
from meridian.workloads.claims_triage import app as claims_app
from meridian.workloads.claims_triage.app import create_app
from meridian.workloads.claims_triage.settings import ClaimsSettings

UNUSED_DSN = "postgresql://claims_api@db.invalid/meridian"
CANARY = "claimant-secret-text-42"
DRAFTED_BY = {"deployment": "replay-chat", "provider": "replay", "mode": "replay"}
OUTPUT = {
    "route": "adjuster",
    "reason": "a reason",
    "draft": "a draft",
    "drafted_by": DRAFTED_BY,
}


class Runtime:
    """A stand-in runtime: answers with a canned body and keeps the requests."""

    def __init__(
        self,
        status: int = 200,
        body: dict[str, Any] | None = None,
        raises: Exception | None = None,
    ) -> None:
        self.run_id = uuid.uuid4()
        self.requests: list[httpx.Request] = []
        self.status = status
        self.raises = raises
        self.body = body or {
            "run_id": str(self.run_id),
            "status": "Completed",
            "output": OUTPUT,
        }
        self.client = httpx.Client(
            base_url="http://runtime.invalid", transport=httpx.MockTransport(self)
        )

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.raises:
            raise self.raises
        return httpx.Response(self.status, json=self.body)


def failing_runtime(run_id: uuid.UUID | None = None) -> Runtime:
    """A runtime whose run failed: 502 with the run's ID, as the real one does."""
    run_id = run_id or uuid.uuid4()
    return Runtime(
        status=502, body={"run_id": str(run_id), "status": "Failed", "output": None}
    )


def make_client(
    dsn: str = UNUSED_DSN,
    runtime: Runtime | None = None,
    exporter: InMemorySpanExporter | None = None,
) -> TestClient:
    app = create_app(
        ClaimsSettings(
            runtime_url="http://runtime.invalid",
            database_url=dsn,
            tenant="claims-triage",
        ),
        tracer_provider=make_tracer_provider("claims-api", exporter),
        http_client=(runtime or Runtime()).client,
    )
    return TestClient(app, raise_server_exceptions=False)


def claims_dsn(db: DatabaseHandle) -> str:
    return db.dsn("claims_api")


# ── the happy path ──────────────────────────────────────────────────────────
def test_a_claim_is_stored_triaged_and_answered_201(
    fresh_database: DatabaseHandle,
) -> None:
    runtime = Runtime()
    exporter = InMemorySpanExporter()
    client = make_client(claims_dsn(fresh_database), runtime, exporter)
    claim = claim_with_id("CLM-9101")

    response = client.post("/claims", json=claim)

    assert response.status_code == 201
    assert response.json() == {
        "claim_id": "CLM-9101",
        "run_id": str(runtime.run_id),
        "run_status": "Completed",
        "proposal": {
            "route": "adjuster",
            "reason": "a reason",
            "drafted_by": DRAFTED_BY,
        },
    }
    ((tenant, submission),) = owner_rows(
        fresh_database, "SELECT tenant, submission FROM claims.claims"
    )
    assert (tenant, submission) == ("claims-triage", claim)
    ((claim_id, run_id, route, reason, draft, deployment, provider, mode),) = (
        owner_rows(
            fresh_database,
            "SELECT claim_id, run_id, route, reason, draft, drafted_by_deployment, "
            "drafted_by_provider, drafted_by_mode FROM claims.triage_proposals",
        )
    )
    assert (claim_id, run_id, route, reason, draft, deployment, provider, mode) == (
        "CLM-9101",
        runtime.run_id,
        "adjuster",
        "a reason",
        "a draft",
        "replay-chat",
        "replay",
        "replay",
    )
    (request,) = runtime.requests
    assert (request.method, request.url.path) == ("POST", "/runs")
    assert json.loads(request.content) == {
        "agent": "claims-triage",
        "tenant": "claims-triage",
        "reference": "CLM-9101",
        # The runtime gets what the graph needs: not the claimant's name or email.
        "input": {"claim": {k: v for k, v in claim.items() if k != "claimant"}},
    }
    assert claim["claimant"]["name"] not in request.content.decode()
    assert claim["claimant"]["email"] not in request.content.decode()
    (span,) = [s for s in exporter.get_finished_spans() if s.name == "claims.submit"]
    assert dict(span.attributes) == {
        "meridian.claim_id": "CLM-9101",
        "meridian.tenant": "claims-triage",
        "meridian.run_id": str(runtime.run_id),
    }


def test_a_run_that_is_awaiting_approval_answers_201_without_a_proposal(
    fresh_database: DatabaseHandle,
) -> None:
    run_id = str(uuid.uuid4())
    runtime = Runtime(
        body={"run_id": run_id, "status": "AwaitingApproval", "output": None}
    )
    client = make_client(claims_dsn(fresh_database), runtime)

    response = client.post("/claims", json=claim_with_id("CLM-9102"))

    assert response.status_code == 201
    assert response.json()["run_status"] == "AwaitingApproval"
    assert response.json()["proposal"] is None
    assert owner_rows(fresh_database, "SELECT 1 FROM claims.triage_proposals") == []


# ── conflict, retry and failure ─────────────────────────────────────────────
def count(db: DatabaseHandle, table: str) -> int:
    return owner_rows(db, f"SELECT count(*) FROM claims.{table}")[0][0]  # noqa: S608


def test_a_repeated_claim_that_already_has_a_proposal_is_409_and_starts_no_run(
    fresh_database: DatabaseHandle,
) -> None:
    runtime = Runtime()
    client = make_client(claims_dsn(fresh_database), runtime)
    claim = claim_with_id("CLM-9103")
    client.post("/claims", json=claim)

    second = client.post("/claims", json=claim)

    assert second.status_code == 409
    assert set(second.json()) == {"detail"}
    assert len(runtime.requests) == 1
    assert (
        count(fresh_database, "claims"),
        count(fresh_database, "triage_proposals"),
    ) == (1, 1)


def test_a_repeated_claim_with_a_different_body_is_409_and_starts_no_run(
    fresh_database: DatabaseHandle,
) -> None:
    runtime = failing_runtime()
    client = make_client(claims_dsn(fresh_database), runtime)
    claim = claim_with_id("CLM-9103")
    client.post("/claims", json=claim)  # stored, triage failed: no proposal yet

    second = client.post("/claims", json=claim | {"claimed_amount": 1})

    assert second.status_code == 409
    assert len(runtime.requests) == 1
    ((stored,),) = owner_rows(fresh_database, "SELECT submission FROM claims.claims")
    assert stored == claim  # the first submission stays


def test_a_repeated_identical_claim_without_a_proposal_runs_triage_again(
    fresh_database: DatabaseHandle,
) -> None:
    claim = claim_with_id("CLM-9103")
    failed = failing_runtime()
    make_client(claims_dsn(fresh_database), failed).post("/claims", json=claim)
    working = Runtime()

    retry = make_client(claims_dsn(fresh_database), working).post("/claims", json=claim)

    assert retry.status_code == 201
    assert retry.json()["run_id"] == str(working.run_id)
    assert len(working.requests) == 1
    assert (
        count(fresh_database, "claims"),
        count(fresh_database, "triage_proposals"),
    ) == (1, 1)


def test_a_retry_after_a_run_that_awaits_approval_runs_triage_again(
    fresh_database: DatabaseHandle,
) -> None:
    claim = claim_with_id("CLM-9103")
    paused = Runtime(
        body={"run_id": str(uuid.uuid4()), "status": "AwaitingApproval", "output": None}
    )
    make_client(claims_dsn(fresh_database), paused).post("/claims", json=claim)

    retry = make_client(claims_dsn(fresh_database), Runtime()).post(
        "/claims", json=claim
    )

    assert retry.status_code == 201
    assert retry.json()["proposal"] is not None


FAILURES = [
    pytest.param(failing_runtime(), 502, True, id="failed"),
    pytest.param(Runtime(status=500, body={"detail": "x"}), 502, False, id="500"),
    pytest.param(
        Runtime(status=500, body={"detail": "x", "run_id": str(uuid.uuid4())}),
        502,
        True,
        id="500-with-run-id",
    ),
    pytest.param(
        Runtime(raises=httpx.ConnectError("refused hunter2")),
        502,
        False,
        id="unreachable",
    ),
    pytest.param(
        Runtime(raises=httpx.ReadTimeout("slow hunter2")), 504, False, id="timeout"
    ),
    pytest.param(
        Runtime(
            status=504,
            body={"run_id": str(uuid.uuid4()), "status": "Failed", "output": None},
        ),
        504,
        True,
        id="runtime-answered-504",
    ),
    pytest.param(
        Runtime(
            body={
                "run_id": str(uuid.uuid4()),
                "status": "Completed",
                "output": {**OUTPUT, "route": "auto_reject"},
            }
        ),
        502,
        True,
        id="bad-output",
    ),
    pytest.param(
        Runtime(
            body={
                "run_id": str(uuid.uuid4()),
                "status": "Completed",
                "output": {**OUTPUT, "route": "request_documents"},
            }
        ),
        502,
        True,
        id="route-not-yet-allowed",
    ),
    pytest.param(
        Runtime(
            body={"run_id": str(uuid.uuid4()), "status": "Completed", "output": None}
        ),
        502,
        True,
        id="no-output",
    ),
    pytest.param(Runtime(body={"unexpected": True}), 502, False, id="bad-contract"),
]


@pytest.mark.parametrize(("runtime", "status", "run_known"), FAILURES)
def test_a_runtime_failure_keeps_the_claim_and_answers_with_its_ids(
    fresh_database: DatabaseHandle, runtime: Runtime, status: int, run_known: bool
) -> None:
    client = make_client(claims_dsn(fresh_database), runtime)

    response = client.post("/claims", json=claim_with_id("CLM-9104"))

    assert response.status_code == status
    body = response.json()
    assert body["claim_id"] == "CLM-9104"
    assert set(body) == {"claim_id", "detail", *({"run_id"} if run_known else set())}
    if run_known:
        assert uuid.UUID(body["run_id"])
    assert "hunter2" not in response.text
    assert owner_rows(fresh_database, "SELECT claim_id FROM claims.claims") == [
        ("CLM-9104",)
    ]
    assert owner_rows(fresh_database, "SELECT 1 FROM claims.triage_proposals") == []


def test_the_log_of_a_runtime_failure_names_the_status_and_the_run_but_no_content(
    fresh_database: DatabaseHandle, caplog: pytest.LogCaptureFixture
) -> None:
    run_id = uuid.uuid4()
    runtime = Runtime(status=500, body={"detail": CANARY, "run_id": str(run_id)})
    client = make_client(claims_dsn(fresh_database), runtime)

    with caplog.at_level(logging.ERROR, logger=claims_app.__name__):
        client.post("/claims", json=claim_with_id("CLM-9104"))

    assert "RuntimeCallError" in caplog.text
    assert "500" in caplog.text
    assert str(run_id) in caplog.text
    assert "CLM-9104" in caplog.text
    assert CANARY not in caplog.text


def test_when_the_proposal_cannot_be_stored_the_answer_is_503_with_both_ids(
    fresh_database: DatabaseHandle,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    runtime = Runtime()

    def refuse(*_a: object, **_k: object) -> None:
        raise psycopg.OperationalError(f"down {CANARY}")

    monkeypatch.setattr(claims_app, "_insert_proposal", refuse)
    client = make_client(claims_dsn(fresh_database), runtime)

    with caplog.at_level(logging.ERROR, logger=claims_app.__name__):
        response = client.post("/claims", json=claim_with_id("CLM-9108"))

    assert response.status_code == 503
    assert response.json()["claim_id"] == "CLM-9108"
    assert response.json()["run_id"] == str(runtime.run_id)
    assert CANARY not in response.text + caplog.text
    assert str(runtime.run_id) in caplog.text
    assert [
        r.levelno for r in caplog.records if str(runtime.run_id) in r.getMessage()
    ] == [logging.ERROR]
    assert count(fresh_database, "claims") == 1
    assert count(fresh_database, "triage_proposals") == 0


def test_when_the_database_is_down_the_answer_is_503_and_no_run_starts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def no_database(*_a: object, **_k: object) -> None:
        raise psycopg.OperationalError("password=hunter2")

    monkeypatch.setattr(claims_app, "connect", no_database)
    runtime = Runtime()

    response = make_client(runtime=runtime).post(
        "/claims", json=claim_with_id("CLM-9105")
    )

    assert response.status_code == 503
    assert response.json()["claim_id"] == "CLM-9105"
    assert "hunter2" not in response.text
    assert runtime.requests == []


def test_a_database_error_that_is_not_an_outage_is_a_500_not_a_503(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse(*_a: object, **_k: object) -> None:
        raise database_error(CANARY)

    monkeypatch.setattr(claims_app, "connect", refuse)

    response = make_client().post("/claims", json=claim_with_id("CLM-9105"))

    assert response.status_code == 500
    assert CANARY not in response.text


def test_a_database_error_inside_the_span_leaves_no_message_in_any_span(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse(*_a: object, **_k: object) -> None:
        raise database_error(CANARY)

    monkeypatch.setattr(claims_app, "connect", refuse)
    exporter = InMemorySpanExporter()

    make_client(exporter=exporter).post("/claims", json=claim_with_id("CLM-9105"))

    assert_spans_hold_no_exception_and_no_canary(exporter, CANARY)
    (span,) = [s for s in exporter.get_finished_spans() if s.name == "claims.submit"]
    assert span.status.description == "NotNullViolation"


def test_an_unexpected_error_inside_the_span_leaves_no_message_in_any_span(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Not a psycopg error, so no handler in the Claims API catches it: only the
    # span wrapper and the catch-all middleware stand between it and a trace.
    def explode(*_a: object, **_k: object) -> None:
        raise RuntimeError(CANARY)

    monkeypatch.setattr(claims_app, "connect", explode)
    exporter = InMemorySpanExporter()

    response = make_client(exporter=exporter).post(
        "/claims", json=claim_with_id("CLM-9107")
    )

    assert response.status_code == 500
    assert CANARY not in response.text
    assert_spans_hold_no_exception_and_no_canary(exporter, CANARY)
    (span,) = [s for s in exporter.get_finished_spans() if s.name == "claims.submit"]
    assert span.status.description == "RuntimeError"


# ── validation: no database needed, no content echoed ───────────────────────
@pytest.mark.parametrize(
    "overrides",
    [
        {"claim_id": "CLM-1"},
        {"extra": "field"},
        {"description": "x" * 5001},
        {"loss_date": "2099-01-01"},
    ],
    ids=["claim-id", "extra-field", "long-description", "loss-after-report"],
)
def test_an_invalid_claim_is_422_and_starts_nothing(overrides: dict[str, Any]) -> None:
    runtime = Runtime()

    response = make_client(runtime=runtime).post(
        "/claims", json=claim_with_id("CLM-9106") | overrides
    )

    assert response.status_code == 422
    assert runtime.requests == []
    assert "xxxxxxxx" not in response.text


@pytest.mark.parametrize(
    "overrides",
    [
        {"description": "before\x00after"},
        {"claimant": {"name": "N\x00", "email": "a@b.example"}},
        {"claimant": {"name": "N", "email": "a\x00@b.example"}},
        {"loss_location": {"city": "Li\x00nz", "country": "AT"}},
        {"documents": ["photo\x00"]},
    ],
    ids=["description", "name", "email", "city", "documents"],
)
def test_a_nul_byte_in_a_free_text_field_is_a_422_not_an_outage(
    overrides: dict[str, Any],
) -> None:
    runtime = Runtime()

    response = make_client(runtime=runtime).post(
        "/claims", json=claim_with_id("CLM-9109") | overrides
    )

    assert response.status_code == 422
    assert runtime.requests == []
    assert "before" not in response.text


def test_a_422_does_not_echo_the_claimant() -> None:
    claim = claim_with_id("CLM-9107")
    claim["claimant"] = {"name": "Secret Name", "email": "not-an-email"}

    response = make_client().post("/claims", json=claim)

    assert response.status_code == 422
    assert "Secret Name" not in response.text
    assert "not-an-email" not in response.text


# ── a cross-origin "simple request" (T-01) ──────────────────────────────────
# A web page can POST text/plain to 127.0.0.1 without a CORS preflight. The
# Claims API takes JSON only, so such a request must change nothing.
def test_a_text_plain_post_with_a_json_looking_body_is_refused() -> None:
    runtime = Runtime()

    response = make_client(runtime=runtime).post(
        "/claims",
        content=json.dumps(claim_with_id("CLM-9110")),
        headers={"Content-Type": "text/plain"},
    )

    assert response.status_code == 422
    assert runtime.requests == []


def test_a_text_plain_post_stores_no_claim(fresh_database: DatabaseHandle) -> None:
    runtime = Runtime()

    response = make_client(claims_dsn(fresh_database), runtime).post(
        "/claims",
        content=json.dumps(claim_with_id("CLM-9111")),
        headers={"Content-Type": "text/plain"},
    )

    assert response.status_code == 422
    assert owner_rows(fresh_database, "SELECT count(*) FROM claims.claims") == [(0,)]
    assert runtime.requests == []


# ── size, health, lifecycle, transport ──────────────────────────────────────
def test_a_body_over_64_kib_is_413() -> None:
    response = make_client().post(
        "/claims",
        content=b"x" * (64 * 1024 + 1),
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 413


def test_healthz_and_the_lifespan_need_no_database() -> None:
    with make_client() as client:
        response = client.get("/healthz")

    assert (response.status_code, response.json()) == (200, {"status": "ok"})


def test_the_runtime_client_ignores_proxy_variables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built: list[dict[str, Any]] = []

    class SpyClient(httpx.Client):
        def __init__(self, **kwargs: Any) -> None:
            built.append(kwargs)
            super().__init__(**kwargs)

    monkeypatch.setattr(claims_app.httpx, "Client", SpyClient)

    create_app(
        ClaimsSettings(runtime_url="http://runtime.invalid", database_url=UNUSED_DSN)
    )

    (kwargs,) = built
    assert kwargs["trust_env"] is False
    assert kwargs["base_url"] == "http://runtime.invalid"
