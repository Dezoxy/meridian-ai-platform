"""The Claims Triage App's metrics: every triage is one count (S064).

``run_taken_triage`` counts each triage it took once: ``meridian.claims.triages``
by how it ended, and, when it stored a proposal, ``meridian.claims.assessments``
by the assessment's outcome. Every route that triages a claim is held to the
same rule here: the JSON routes, the claimant's pages and the adjuster's page,
each through the real app, PostgreSQL and a stand-in runtime. The invariants the
path tests check are that the assessments add up to the rows of
``claims.triage_proposals`` and that each triage is one count of the other.
"""

import logging
import uuid
from datetime import date
from typing import Any, get_args

import httpx
import psycopg
import pytest
from dbsupport import DatabaseHandle
from exportsupport import record_exports
from fastapi.testclient import TestClient
from opentelemetry import metrics as otel_metrics
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from servicesupport import claim_with_id, metric_points, owner_rows

from meridian.platform.common import http as common_http
from meridian.platform.common import metrics as common_metrics
from meridian.platform.common.metrics import make_meter_provider
from meridian.platform.common.telemetry import make_tracer_provider
from meridian.workloads.claims_triage import app as claims_app
from meridian.workloads.claims_triage import meters as claims_meters
from meridian.workloads.claims_triage import triaging
from meridian.workloads.claims_triage.app import create_app
from meridian.workloads.claims_triage.proposal import (
    AssessmentStatus,
    UnavailableBecause,
)
from meridian.workloads.claims_triage.settings import ClaimsSettings

SERIES = "meridian.claims.assessments"
TRIAGES = "meridian.claims.triages"
TENANT = "claims-triage"
CANARY = "claimant-text-canary-73"
DRAFTED_BY = {
    "deployment": "replay-chat",
    "provider": "replay",
    "mode": "replay",
    "prompt": "a" * 64,
}
CITATION = {"product": "HOME-STD", "wording_version": "2026-01", "clause": "2.1"}
# A valid proposal as the graph writes it: referred to an adjuster, the
# assessment unavailable. Each test changes what it needs.
UNAVAILABLE = {
    "route": "adjuster",
    "reason": "unverified",
    "recommendation": None,
    "payable_amount": None,
    "exclusion_clause": None,
    "fraud_indicators": [],
    "missing_documents": [],
    "citations": [],
    "gaps": ["exclusion_assessment"],
    "assessment": "unavailable",
    "unavailable_because": "not-json",
    "rationale": None,
    "drafted_by": DRAFTED_BY,
}
NOT_NEEDED = UNAVAILABLE | {
    "route": "auto_approve",
    "reason": "within_threshold",
    "recommendation": "approve",
    "payable_amount": 1800,
    "citations": [CITATION],
    "gaps": [],
    "assessment": "not_needed",
    "unavailable_because": None,
    "drafted_by": None,
}
NONE_APPLIES = UNAVAILABLE | {
    "route": "request_documents",
    "reason": "missing_documents",
    "missing_documents": ["photos"],
    "citations": [CITATION],
    "gaps": [],
    "assessment": "none_applies",
    "unavailable_because": None,
    "rationale": "No circumstance exclusion applies.",
}
APPLIES = UNAVAILABLE | {
    "reason": "excluded",
    "recommendation": "reject",
    "exclusion_clause": "2.1",
    "citations": [CITATION],
    "gaps": [],
    "assessment": "applies",
    "unavailable_because": None,
    "rationale": "The loss falls under a circumstance exclusion.",
}
# The run's status that goes with each route, as the real runtime answers.
STATUS_OF_ROUTE = {
    "adjuster": "AwaitingApproval",
    "auto_approve": "Completed",
    "request_documents": "Completed",
}
REPORT_DATE = date(2026, 7, 13)
SAME_ORIGIN = {"Origin": "http://testserver"}
# The label keys and the words of the outcome: what a series may carry.
LABEL_KEYS = {"meridian.tenant", "meridian.outcome", "meridian.reason"}


class Runtime:
    """A stand-in runtime: every call answers a new run that ended with the
    proposal ``output``, or, with ``fails``, as a run that failed (502), or,
    with ``status``, as an answer with that status (the run's ID in its body),
    or, with ``body``, as a 200 whose body is that text (not a run), or, with
    ``raises``, not at all (the transport raises it)."""

    def __init__(
        self,
        output: dict[str, Any],
        *,
        fails: bool = False,
        status: int | None = None,
        body: str | None = None,
        raises: Exception | None = None,
    ) -> None:
        self.output = output
        self.fails = fails
        self.status = status
        self.body = body
        self.raises = raises
        self.client = httpx.Client(
            base_url="http://runtime.invalid", transport=httpx.MockTransport(self)
        )

    def __call__(self, request: httpx.Request) -> httpx.Response:
        run_id = str(uuid.uuid4())
        if self.raises is not None:
            raise self.raises
        if self.body is not None:
            return httpx.Response(200, text=self.body)
        if self.status is not None:
            return httpx.Response(
                self.status, json={"run_id": run_id, "status": "Failed", "output": None}
            )
        if self.fails:
            return httpx.Response(
                502, json={"run_id": run_id, "status": "Failed", "output": None}
            )
        status = STATUS_OF_ROUTE[self.output["route"]]
        return httpx.Response(
            200, json={"run_id": run_id, "status": status, "output": self.output}
        )


def make_client(
    db: DatabaseHandle, runtime: Runtime, reader: InMemoryMetricReader
) -> TestClient:
    app = create_app(
        ClaimsSettings(
            runtime_url="http://runtime.invalid",
            database_url=db.dsn("claims_api"),
            tenant=TENANT,
        ),
        tracer_provider=make_tracer_provider("claims-api"),
        meter_provider=make_meter_provider("claims-api", reader),
        http_client=runtime.client,
        today=lambda: REPORT_DATE,
    )
    return TestClient(app, raise_server_exceptions=False)


def counts(reader: InMemoryMetricReader) -> list[tuple[dict, float]]:
    return metric_points(reader, SERIES)


def triages(reader: InMemoryMetricReader) -> list[tuple[dict, float]]:
    return metric_points(reader, TRIAGES)


def triage_count(outcome: str, reason: str | None = None) -> tuple[dict, float]:
    """The one count of a triage that ended as ``outcome``."""
    labels = {"meridian.tenant": TENANT, "meridian.outcome": outcome}
    if reason is not None:
        labels["meridian.reason"] = reason
    return labels, 1


def proposals_stored(db: DatabaseHandle) -> int:
    ((rows,),) = owner_rows(db, "SELECT count(*) FROM claims.triage_proposals")
    return rows


def with_reason(word: str) -> dict[str, Any]:
    return UNAVAILABLE | {"unavailable_because": word}


# ── the label sets are the model's ──────────────────────────────────────────
def test_the_outcome_and_reason_words_are_the_ones_the_proposal_model_defines() -> None:
    # A new word in the model fails here, so that someone reads what the series
    # now carries before the dashboards and rules meet it.
    assert get_args(AssessmentStatus) == (
        "not_needed",
        "none_applies",
        "applies",
        "unavailable",
    )
    assert get_args(UnavailableBecause) == (
        "truncated",
        "not-json",
        "not-the-format",
        "unknown-clause",
        "unsure",
        "too-long",
        "special-data",
        "injection-suspected",
        "filtered",
    )


def test_the_triages_outcomes_and_failure_words_are_the_modules_own() -> None:
    # A new word fails here, so that someone reads what the series now carries
    # before the dashboards and rules meet it.
    assert get_args(claims_meters.TriageOutcome) == ("stored", "failed", "taken-over")
    assert get_args(claims_meters.TriageFailure) == (
        "runtime-unreachable",
        "runtime-timeout",
        "runtime-failed",
        "bad-output",
        "proposal-lost",
        "unexpected",
    )
    assert claims_meters.TRIAGES == TRIAGES


# ── each outcome and each reason, through the path that stores a proposal ───
@pytest.mark.parametrize(
    ("output", "outcome"),
    [
        pytest.param(NOT_NEEDED, "not_needed", id="not-needed"),
        pytest.param(NONE_APPLIES, "none_applies", id="none-applies"),
        pytest.param(APPLIES, "applies", id="applies"),
    ],
)
def test_an_assessment_that_was_made_is_one_count_by_its_outcome_and_no_reason(
    fresh_database: DatabaseHandle, output: dict[str, Any], outcome: str
) -> None:
    reader = InMemoryMetricReader()
    client = make_client(fresh_database, Runtime(output), reader)

    response = client.post("/claims", json=claim_with_id("CLM-9101"))

    assert response.status_code == 201
    assert counts(reader) == [
        ({"meridian.tenant": TENANT, "meridian.outcome": outcome}, 1)
    ]


@pytest.mark.parametrize("word", get_args(UnavailableBecause))
def test_an_unavailable_assessment_is_one_count_under_its_own_reason_word(
    fresh_database: DatabaseHandle, word: str
) -> None:
    reader = InMemoryMetricReader()
    client = make_client(fresh_database, Runtime(with_reason(word)), reader)

    response = client.post("/claims", json=claim_with_id("CLM-9101"))

    assert response.status_code == 201
    assert counts(reader) == [
        (
            {
                "meridian.tenant": TENANT,
                "meridian.outcome": "unavailable",
                "meridian.reason": word,
            },
            1,
        )
    ]


def test_no_label_holds_a_claims_id_or_the_text_of_a_claim_or_a_rationale(
    fresh_database: DatabaseHandle,
) -> None:
    reader = InMemoryMetricReader()
    claim = claim_with_id("CLM-9101") | {"description": CANARY}
    output = APPLIES | {"rationale": f"{CANARY} {CANARY}"}
    client = make_client(fresh_database, Runtime(output), reader)

    assert client.post("/claims", json=claim).status_code == 201

    points = counts(reader)
    assert points
    for attributes, _ in points:
        assert set(attributes) <= LABEL_KEYS
        for value in attributes.values():
            assert CANARY not in value
            assert "CLM-9101" not in value
            assert claim["policy_number"] not in value
            assert claim["peril"] not in value
            assert output["route"] not in value
            assert output["exclusion_clause"] not in value


# ── one stored proposal is one count, on every route ────────────────────────
def post_json_claim(client: TestClient, claim: dict[str, Any]) -> httpx.Response:
    return client.post("/claims", json=claim)


def claimant_form(claim: dict[str, Any]) -> dict[str, str]:
    return {
        "claim_id": claim["claim_id"],
        "policy_number": claim["policy_number"],
        "peril": claim["peril"],
        "loss_date": claim["loss_date"],
        "claimed_amount": str(claim["claimed_amount"]),
        "city": claim["loss_location"]["city"],
        "country": claim["loss_location"]["country"],
        "description": claim["description"],
        "documents": "\n".join(claim["documents"]),
        "claimant_name": claim["claimant"]["name"],
        "claimant_email": claim["claimant"]["email"],
    }


def test_a_claim_posted_as_json_is_one_proposal_and_one_count(
    fresh_database: DatabaseHandle,
) -> None:
    reader = InMemoryMetricReader()
    client = make_client(fresh_database, Runtime(UNAVAILABLE), reader)

    assert post_json_claim(client, claim_with_id("CLM-9101")).status_code == 201

    assert proposals_stored(fresh_database) == 1
    assert sum(value for _, value in counts(reader)) == 1


def test_a_claim_submitted_on_the_claimants_page_is_one_proposal_and_one_count(
    fresh_database: DatabaseHandle,
) -> None:
    reader = InMemoryMetricReader()
    client = make_client(fresh_database, Runtime(UNAVAILABLE), reader)

    response = client.post(
        "/claimant/claims",
        data=claimant_form(claim_with_id("CLM-9101")),
        headers=SAME_ORIGIN,
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert proposals_stored(fresh_database) == 1
    assert sum(value for _, value in counts(reader)) == 1


def test_a_triage_again_by_json_stores_a_second_proposal_and_counts_a_second(
    fresh_database: DatabaseHandle,
) -> None:
    reader = InMemoryMetricReader()
    client = make_client(fresh_database, Runtime(UNAVAILABLE), reader)
    post_json_claim(client, claim_with_id("CLM-9101"))

    response = client.post("/claims/CLM-9101/triage", json={})

    assert response.status_code == 200
    assert proposals_stored(fresh_database) == 2
    assert sum(value for _, value in counts(reader)) == 2


def test_the_adjusters_send_back_stores_a_second_proposal_and_counts_a_second(
    fresh_database: DatabaseHandle,
) -> None:
    reader = InMemoryMetricReader()
    client = make_client(fresh_database, Runtime(UNAVAILABLE), reader)
    first = post_json_claim(client, claim_with_id("CLM-9101"))

    response = client.post(
        "/adjuster/claims/CLM-9101/triage",
        data={"run": first.json()["run_id"]},
        headers=SAME_ORIGIN,
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert proposals_stored(fresh_database) == 2
    assert sum(value for _, value in counts(reader)) == 2


def test_documents_by_json_triage_the_claim_again_and_count_the_new_proposal(
    fresh_database: DatabaseHandle,
) -> None:
    reader = InMemoryMetricReader()
    client = make_client(fresh_database, Runtime(NONE_APPLIES), reader)
    post_json_claim(client, claim_with_id("CLM-9101"))

    response = client.post("/claims/CLM-9101/documents", json={"documents": ["a"]})

    assert response.status_code == 200
    assert proposals_stored(fresh_database) == 2
    assert sum(value for _, value in counts(reader)) == 2


def test_documents_on_the_claimants_page_count_the_new_proposal(
    fresh_database: DatabaseHandle,
) -> None:
    reader = InMemoryMetricReader()
    client = make_client(fresh_database, Runtime(NONE_APPLIES), reader)
    post_json_claim(client, claim_with_id("CLM-9101"))

    response = client.post(
        "/claimant/claims/CLM-9101/documents",
        data={"documents": "photos"},
        headers=SAME_ORIGIN,
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert proposals_stored(fresh_database) == 2
    assert sum(value for _, value in counts(reader)) == 2


def test_a_claim_posted_again_after_its_triage_stores_nothing_and_counts_nothing(
    fresh_database: DatabaseHandle,
) -> None:
    reader = InMemoryMetricReader()
    client = make_client(fresh_database, Runtime(UNAVAILABLE), reader)
    claim = claim_with_id("CLM-9101")
    post_json_claim(client, claim)

    again = post_json_claim(client, claim)

    assert again.status_code == 409
    assert proposals_stored(fresh_database) == 1
    assert sum(value for _, value in counts(reader)) == 1


def test_a_triage_that_failed_is_no_assessment_and_its_retry_is_one_of_each(
    fresh_database: DatabaseHandle,
) -> None:
    reader = InMemoryMetricReader()
    claim = claim_with_id("CLM-9101")
    runtime = Runtime(UNAVAILABLE, fails=True)
    client = make_client(fresh_database, runtime, reader)
    assert post_json_claim(client, claim).status_code == 502
    assert counts(reader) == []
    assert triages(reader) == [triage_count("failed", "runtime-failed")]
    runtime.fails = False

    retried = post_json_claim(client, claim)

    assert retried.status_code == 201
    assert proposals_stored(fresh_database) == 1
    assert sum(value for _, value in counts(reader)) == 1
    assert sorted(triages(reader), key=repr) == sorted(
        [triage_count("failed", "runtime-failed"), triage_count("stored")], key=repr
    )


# ── every triage is one count, however it ends ──────────────────────────────
def test_a_stored_proposal_is_one_stored_triage_beside_its_assessment(
    fresh_database: DatabaseHandle,
) -> None:
    reader = InMemoryMetricReader()
    client = make_client(fresh_database, Runtime(APPLIES), reader)

    assert post_json_claim(client, claim_with_id("CLM-9101")).status_code == 201

    assert triages(reader) == [triage_count("stored")]
    assert [value for _, value in counts(reader)] == [1]


@pytest.mark.parametrize(
    ("runtime", "reason"),
    [
        pytest.param(
            Runtime(UNAVAILABLE, raises=httpx.ConnectError("refused")),
            "runtime-unreachable",
            id="unreachable",
        ),
        pytest.param(
            Runtime(UNAVAILABLE, raises=httpx.ReadTimeout("no answer")),
            "runtime-timeout",
            id="timed-out-waiting",
        ),
        pytest.param(Runtime(UNAVAILABLE, status=502), "runtime-failed", id="502"),
        # The runtime answered, with a failure that was a timeout of its own
        # (the gateway's): the hop worked, so it is the runtime's failure.
        pytest.param(Runtime(UNAVAILABLE, status=504), "runtime-failed", id="504"),
        pytest.param(Runtime(UNAVAILABLE, status=500), "runtime-failed", id="500"),
    ],
)
def test_a_run_the_runtime_could_not_give_is_one_failed_triage_under_its_reason(
    fresh_database: DatabaseHandle, runtime: Runtime, reason: str
) -> None:
    reader = InMemoryMetricReader()
    client = make_client(fresh_database, runtime, reader)

    response = post_json_claim(client, claim_with_id("CLM-9101"))

    assert response.status_code in {502, 504}
    assert triages(reader) == [triage_count("failed", reason)]
    assert counts(reader) == []
    assert proposals_stored(fresh_database) == 0


def test_an_answer_outside_the_runtimes_contract_is_a_bad_output(
    fresh_database: DatabaseHandle,
) -> None:
    reader = InMemoryMetricReader()
    client = make_client(fresh_database, Runtime(UNAVAILABLE, body="no"), reader)

    response = post_json_claim(client, claim_with_id("CLM-9101"))

    assert response.status_code == 502
    assert triages(reader) == [triage_count("failed", "bad-output")]
    assert counts(reader) == []


def test_an_answer_that_is_not_a_proposal_is_a_bad_output_and_no_assessment(
    fresh_database: DatabaseHandle,
) -> None:
    reader = InMemoryMetricReader()
    broken = Runtime(UNAVAILABLE | {"route": "auto_approve"})
    client = make_client(fresh_database, broken, reader)

    response = post_json_claim(client, claim_with_id("CLM-9101"))

    assert response.status_code == 502
    assert triages(reader) == [triage_count("failed", "bad-output")]
    assert counts(reader) == []
    assert proposals_stored(fresh_database) == 0


def test_a_proposal_the_database_refused_is_a_lost_proposal_and_no_assessment(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    def lost(*_: object) -> bool:
        raise psycopg.OperationalError("the connection was lost")

    monkeypatch.setattr(triaging, "close_triage", lost)
    reader = InMemoryMetricReader()
    client = make_client(fresh_database, Runtime(UNAVAILABLE), reader)

    response = post_json_claim(client, claim_with_id("CLM-9101"))

    assert response.status_code == 503
    assert triages(reader) == [triage_count("failed", "proposal-lost")]
    assert counts(reader) == []


def test_a_triage_another_request_took_over_is_counted_taken_over_once(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(triaging, "close_triage", lambda *_: False)
    reader = InMemoryMetricReader()
    client = make_client(fresh_database, Runtime(UNAVAILABLE), reader)

    response = post_json_claim(client, claim_with_id("CLM-9101"))

    assert response.status_code == 409
    assert triages(reader) == [triage_count("taken-over")]
    assert counts(reader) == []


def test_a_bug_that_leaves_the_triage_by_no_branch_is_counted_once_and_raised(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*_: object) -> bool:
        raise RuntimeError(CANARY)

    monkeypatch.setattr(triaging, "close_triage", broken)
    reader = InMemoryMetricReader()
    client = make_client(fresh_database, Runtime(UNAVAILABLE), reader)

    response = post_json_claim(client, claim_with_id("CLM-9101"))

    assert response.status_code == 500
    assert triages(reader) == [triage_count("failed", "unexpected")]
    assert counts(reader) == []
    assert CANARY not in repr(triages(reader))


def test_an_answer_that_cannot_be_built_after_the_proposal_is_stored_is_a_stored_triage(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unbuildable(**_: object) -> None:
        raise RuntimeError(CANARY)

    monkeypatch.setattr(triaging, "ClaimResponse", unbuildable)
    reader = InMemoryMetricReader()
    client = make_client(fresh_database, Runtime(UNAVAILABLE), reader)

    response = post_json_claim(client, claim_with_id("CLM-9101"))

    assert response.status_code == 500
    assert proposals_stored(fresh_database) == 1
    assert triages(reader) == [triage_count("stored")]
    assert counts(reader) == [
        (
            {
                "meridian.tenant": TENANT,
                "meridian.outcome": "unavailable",
                "meridian.reason": "not-json",
            },
            1,
        )
    ]
    assert CANARY not in response.text


def test_no_triage_label_holds_a_claims_id_or_the_text_of_a_claim(
    fresh_database: DatabaseHandle,
) -> None:
    reader = InMemoryMetricReader()
    claim = claim_with_id("CLM-9101") | {"description": CANARY}
    client = make_client(fresh_database, Runtime(UNAVAILABLE, fails=True), reader)

    assert client.post("/claims", json=claim).status_code == 502

    ((attributes, _),) = triages(reader)
    assert set(attributes) <= LABEL_KEYS
    for value in attributes.values():
        assert CANARY not in value
        assert "CLM-9101" not in value


# ── a counter that raises does not fail the triage it counts ────────────────
class ExplodingCounter:
    def add(self, *args: object, **kwargs: object) -> None:
        raise RuntimeError(CANARY)


class ExplodingMeter:
    def create_counter(self, *args: object, **kwargs: object) -> ExplodingCounter:
        return ExplodingCounter()


class ExplodingProvider:
    def get_meter(self, *args: object, **kwargs: object) -> ExplodingMeter:
        return ExplodingMeter()


def test_a_triage_whose_proposal_was_stored_still_answers_with_the_claim(
    fresh_database: DatabaseHandle, caplog: pytest.LogCaptureFixture
) -> None:
    app = create_app(
        ClaimsSettings(
            runtime_url="http://runtime.invalid",
            database_url=fresh_database.dsn("claims_api"),
            tenant=TENANT,
        ),
        tracer_provider=make_tracer_provider("claims-api"),
        meter_provider=ExplodingProvider(),  # type: ignore[arg-type]
        http_client=Runtime(APPLIES).client,
        today=lambda: REPORT_DATE,
    )
    client = TestClient(app, raise_server_exceptions=False)

    with caplog.at_level(logging.WARNING):
        response = post_json_claim(client, claim_with_id("CLM-9101"))

    assert response.status_code == 201
    assert response.json()["claim_id"] == "CLM-9101"
    assert proposals_stored(fresh_database) == 1
    warnings = [r for r in caplog.records if r.name == common_metrics.__name__]
    assert {r.getMessage() for r in warnings} == {
        "a metric was not recorded: RuntimeError in ClaimsMeters.proposal_stored"
    }
    assert CANARY not in caplog.text


# ── the meter provider's lifecycle ──────────────────────────────────────────
class ShutdownSpy(MeterProvider):
    def __init__(self) -> None:
        super().__init__()
        self.shutdowns = 0

    def shutdown(self, *args: object, **kwargs: object) -> None:
        self.shutdowns += 1
        super().shutdown(*args, **kwargs)


def settings() -> ClaimsSettings:
    return ClaimsSettings(
        runtime_url="http://runtime.invalid",
        database_url="postgresql://claims_api@db.invalid/meridian",
        tenant=TENANT,
    )


def test_an_injected_meter_provider_is_left_to_its_caller() -> None:
    spy = ShutdownSpy()

    with TestClient(create_app(settings(), meter_provider=spy)):
        pass

    assert spy.shutdowns == 0


def test_a_meter_provider_the_app_built_is_shut_down_with_the_app(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spy = ShutdownSpy()
    monkeypatch.setattr(claims_app, "make_meter_provider", lambda _name: spy)

    with TestClient(create_app(settings())):
        assert spy.shutdowns == 0

    assert spy.shutdowns == 1


class FailingShutdown(MeterProvider):
    """A provider whose reader fails as it closes: the SDK raises a bare
    ``Exception`` whose text can quote what the exporter hit. It fails once: the
    SDK's exit hook shuts every provider down again at the end of the run."""

    failed = False

    def shutdown(self, *args: object, **kwargs: object) -> None:
        if not self.failed:
            self.failed = True
            raise Exception(f"the reader failed {CANARY}")


class TracerSpy(TracerProvider):
    def __init__(self) -> None:
        super().__init__()
        self.shutdowns = 0

    def shutdown(self) -> None:
        self.shutdowns += 1
        super().shutdown()


def test_a_meter_provider_that_fails_to_shut_down_is_one_warning_and_what_follows_runs(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    tracer = TracerSpy()
    monkeypatch.setattr(claims_app, "make_meter_provider", lambda _: FailingShutdown())
    monkeypatch.setattr(common_http, "make_tracer_provider", lambda _: tracer)

    with caplog.at_level(logging.WARNING), TestClient(create_app(settings())):
        pass

    # The lifespan flushes the tracer provider after ``close``; a raise out of
    # the meter's shutdown would have skipped that and lost the spans.
    assert tracer.shutdowns == 1
    (record,) = [r for r in caplog.records if r.name == claims_app.__name__]
    assert record.levelno == logging.WARNING
    assert "Exception" in record.getMessage()
    assert CANARY not in caplog.text


def test_an_app_built_with_no_meter_provider_sends_its_series_to_the_collector(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = record_exports(monkeypatch)
    app = create_app(
        ClaimsSettings(
            runtime_url="http://runtime.invalid",
            database_url=fresh_database.dsn("claims_api"),
            tenant=TENANT,
        ),
        tracer_provider=make_tracer_provider("claims-api", InMemorySpanExporter()),
        http_client=Runtime(APPLIES).client,
        today=lambda: REPORT_DATE,
    )

    with TestClient(app) as client:  # the lifespan's end closes the provider
        assert post_json_claim(client, claim_with_id("CLM-9101")).status_code == 201

    stored = {"meridian.tenant": TENANT, "meridian.outcome": "stored"}
    applies = {"meridian.tenant": TENANT, "meridian.outcome": "applies"}
    assert recorder.points(TRIAGES) == [("claims-api", stored, 1)]
    assert recorder.points(SERIES) == [("claims-api", applies, 1)]


def test_the_app_sets_no_global_meter_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        otel_metrics, "set_meter_provider", lambda provider: calls.append("meter")
    )

    with TestClient(create_app(settings())) as client:
        assert client.get("/healthz").status_code == 200

    assert calls == []
