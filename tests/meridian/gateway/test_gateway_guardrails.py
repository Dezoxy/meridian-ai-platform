"""A request's data class, redaction and the provider's content filter (S047).

Live mode over a scripted provider that records what reached it and answers or
raises per deployment, the real ledger on the throwaway PostgreSQL, the limiter
and the circuit breaker on a fake clock. Nothing here reaches a network or
Azure. The values that must not be found anywhere are test values (the e-mail
address, IBAN and card number are the documented fictional ones).
"""

import json
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from dbsupport import DatabaseHandle
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from servicesupport import (
    GLOBAL_DEPLOYMENT_YAML,
    REPLAY_ENTRY,
    SECOND_DEPLOYMENT_YAML,
    FakeClock,
    assert_spans_hold_no_exception_and_no_canary,
    audit_events,
    owner_rows,
    pin_chat_route,
)

from meridian.platform.common.http import REFUSED
from meridian.platform.common.metrics import make_meter_provider
from meridian.platform.common.telemetry import make_tracer_provider
from meridian.platform.gateway.app import PROVIDER_FILTERED, create_app
from meridian.platform.gateway.budget import chat_estimate, embedding_estimate
from meridian.platform.gateway.models import ChatRequest, EmbeddingRequest
from meridian.platform.gateway.providers.base import (
    EmbeddingReply,
    ProviderError,
    ProviderReply,
)
from meridian.platform.gateway.resilience import FAILURE_THRESHOLD
from meridian.platform.gateway.settings import GatewaySettings
from meridian.platform.gateway.walk import closing_for
from meridian.platform.registry import Registry, load_registry
from meridian.platform.registry.checks import run_checks
from meridian.platform.registry.models import Deployment

CHAT = "/v1/chat"
EMBEDDINGS = "/v1/embeddings"
DATA_CLASS_HEADER = "X-Meridian-Data-Class"
FIRST = "aoai-sdc-gpt-4o"
SECOND = "aoai-sdc-gpt-4o-second"
GLOBAL = "aoai-sdc-gpt-4o-global"
EMBEDDING = "aoai-sdc-text-embedding-3-large"
PERSONAL_TENANT = "claims-triage"
SYNTHETIC_TENANT = "development"
CANARY = "CANARY-provider-internals-9917"
ANSWER_TEXT = "a fake answer"
PROVIDER_MODEL = "gpt-4o-2024-11-20"
EMBEDDING_MODEL = "text-embedding-3-large"

# The documented fictional values (RFC 2606 domain, the standard test card, an
# IBAN that passes the checksum and belongs to no account).
EMAIL = "ana.kovacs@example.com"
IBAN = "HU42 1177 3016 1111 1018 0000 0000"
CARD = "4111 1111 1111 1111"
# What a log, a label or a row would carry even if the value were cut short.
ORIGINALS = (EMAIL, "ana.kovacs", "1177 3016", "4111 1111")
USER_TEXT = f"Claimant {EMAIL} paid with {CARD} from {IBAN}, thanks."
REDACTED_USER_TEXT = "Claimant [email] paid with [card] from [iban], thanks."
REDACTIONS_IN_USER_TEXT = 3
SYSTEM_TEXT = "You draft triage summaries."
PLAIN_BODY = {"messages": [{"role": "user", "content": "A storm hit the roof."}]}
PII_BODY = {
    "messages": [
        {"role": "system", "content": SYSTEM_TEXT},
        {"role": "user", "content": USER_TEXT},
        {"role": "assistant", "content": "Understood."},
        {"role": "user", "content": f"Again: {EMAIL}"},
    ]
}
# Four values in two messages.
REDACTIONS_IN_PII_BODY = REDACTIONS_IN_USER_TEXT + 1
EMBEDDING_BODY = {"inputs": [f"Mail {EMAIL} or pay {CARD}", "A storm hit the roof."]}


@dataclass(frozen=True, slots=True)
class Outcome:
    """What one deployment does when it is called."""

    error: Exception | None = None


OK = Outcome()


class RecordingProvider:
    """Answers or raises per deployment ID and records what it was sent."""

    def __init__(self) -> None:
        self.outcomes: dict[str, Outcome] = {}
        self.called: list[str] = []
        self.chats: list[ChatRequest] = []
        self.embeds: list[EmbeddingRequest] = []

    def _outcome(self, deployment: Deployment) -> Outcome:
        self.called.append(deployment.id)
        outcome = self.outcomes.get(deployment.id, OK)
        if outcome.error is not None:
            raise outcome.error
        return outcome

    def chat(
        self, deployment: Deployment, request: ChatRequest, *, timeout_seconds: float
    ) -> ProviderReply:
        self.chats.append(request)
        self._outcome(deployment)
        return ProviderReply(
            text=ANSWER_TEXT,
            finish_reason="stop",
            model=PROVIDER_MODEL,
            input_tokens=11,
            output_tokens=7,
        )

    def embed(
        self,
        deployment: Deployment,
        request: EmbeddingRequest,
        *,
        timeout_seconds: float,
    ) -> EmbeddingReply:
        self.embeds.append(request)
        self._outcome(deployment)
        assert deployment.dimensions is not None
        vector = (0.0,) * deployment.dimensions
        return EmbeddingReply(
            embeddings=tuple(vector for _ in request.inputs),
            model=EMBEDDING_MODEL,
            input_tokens=9,
        )


@dataclass(slots=True)
class Gateway:
    app: FastAPI
    client: TestClient
    provider: RecordingProvider
    clock: FakeClock
    exporter: InMemorySpanExporter
    reader: InMemoryMetricReader
    database: DatabaseHandle
    registry: Registry

    def post(
        self,
        path: str = CHAT,
        body: object | None = None,
        *,
        tenant: str = PERSONAL_TENANT,
        data_class: str | None = None,
    ) -> tuple[httpx.Response, uuid.UUID]:
        run_id = uuid.uuid4()
        headers = {
            "X-Meridian-Tenant": tenant,
            "X-Meridian-Agent": "claims-triage",
            "X-Meridian-Run": str(run_id),
        }
        if data_class is not None:
            headers[DATA_CLASS_HEADER] = data_class
        default = PLAIN_BODY if path == CHAT else EMBEDDING_BODY
        response = self.client.post(
            path, json=default if body is None else body, headers=headers
        )
        return response, run_id

    def rows(self, run_id: uuid.UUID) -> list[dict]:
        return audit_events(self.database, run_id)

    def usage(self) -> list[dict]:
        names = ["deployment", "state", "reserved_tokens"]
        rows = owner_rows(
            self.database,
            "SELECT deployment, state, reserved_tokens FROM gateway.usage "
            "ORDER BY reserved_at, deployment",
        )
        return [dict(zip(names, row, strict=True)) for row in rows]

    def counters(self) -> list[tuple]:
        return owner_rows(
            self.database, "SELECT kind, amount FROM gateway.budget_counters"
        )

    def span(self, name: str) -> ReadableSpan:
        (span,) = [s for s in self.exporter.get_finished_spans() if s.name == name]
        return span

    def calls_counted(self) -> dict[tuple, float]:
        data = self.reader.get_metrics_data()
        found: dict[tuple, float] = {}
        for resource in data.resource_metrics if data else ():
            for scope in resource.scope_metrics:
                for metric in scope.metrics:
                    if metric.name != "meridian.gateway.calls":
                        continue
                    for point in metric.data.data_points:
                        attributes = dict(point.attributes)
                        key = (
                            attributes["meridian.outcome"],
                            attributes.get("meridian.reason"),
                        )
                        found[key] = found.get(key, 0) + point.value
        return found

    def everything_it_wrote(self) -> str:
        """Every audit row and every ledger row as one string, and the metrics:
        what a value that must not be kept is looked for in."""
        audit_rows = owner_rows(self.database, "SELECT * FROM audit.events")
        usage_rows = owner_rows(self.database, "SELECT * FROM gateway.usage")
        return str([audit_rows, usage_rows, self.calls_counted()])


def limits_block(requests: int = 10, tokens_per_minute: int = 10000) -> str:
    return (
        f"      requests_per_10_seconds: {requests}\n"
        f"      tokens_per_minute: {tokens_per_minute}\n"
        "      tokens_per_day: 300000\n"
        "      cost_per_month_eur: 10\n"
    )


SEED_LIMITS = limits_block()


@pytest.fixture
def planted(plant: Callable[..., Path]) -> Path:
    """The registry that also holds the test-only second EU deployment and the
    global one; each test pins the chat route it needs."""
    return plant(
        (
            "models.yaml",
            REPLAY_ENTRY,
            SECOND_DEPLOYMENT_YAML + GLOBAL_DEPLOYMENT_YAML + REPLAY_ENTRY,
        )
    )


@pytest.fixture
def registry_with(plant: Callable[..., Path], planted: Path) -> Callable[..., Path]:
    """A registry whose chat route lists ``candidates`` and, when given, whose
    claims-triage tenant has the planted limits."""

    def make(*candidates: str, **limits: int) -> Path:
        if limits:
            plant(("tenants.yaml", SEED_LIMITS, limits_block(**limits)))
        directory = pin_chat_route(planted, *candidates)
        assert run_checks(load_registry(directory)) == ()
        return directory

    return make


@pytest.fixture
def build(fresh_database: DatabaseHandle) -> Callable[[Path], Gateway]:
    def make(registry_dir: Path) -> Gateway:
        clock = FakeClock()
        exporter, reader = InMemorySpanExporter(), InMemoryMetricReader()
        provider = RecordingProvider()
        settings = GatewaySettings(
            registry_dir=registry_dir,
            mode="live",
            environment="test",
            database_url=fresh_database.dsn("model_gateway"),
        )
        app = create_app(
            settings,
            tracer_provider=make_tracer_provider("model-gateway", exporter),
            meter_provider=make_meter_provider("model-gateway", reader),
            providers={"azure-openai": provider},
            clock=clock,
        )
        return Gateway(
            app,
            TestClient(app),
            provider,
            clock,
            exporter,
            reader,
            fresh_database,
            load_registry(registry_dir),
        )

    return make


@pytest.fixture
def two(
    registry_with: Callable[..., Path], build: Callable[[Path], Gateway]
) -> Gateway:
    """The seeded limits and a chat route of two EU deployments."""
    return build(registry_with(FIRST, SECOND))


@pytest.fixture
def global_first(
    registry_with: Callable[..., Path], build: Callable[[Path], Gateway]
) -> Gateway:
    """A chat route that lists a global deployment, which holds synthetic data
    only, ahead of an EU one."""
    return build(registry_with(GLOBAL, FIRST))


# ── 1. a request's data class: a header may raise it, never lower it ────────
@pytest.mark.parametrize("path", [CHAT, EMBEDDINGS])
@pytest.mark.parametrize("value", ["Personal", "public", "", "personal ", "secret"])
def test_a_value_that_is_not_one_of_the_four_classes_is_a_422(
    two: Gateway, path: str, value: str
) -> None:
    response, run_id = two.post(path, data_class=value)

    assert response.status_code == 422
    assert two.provider.called == []
    assert two.rows(run_id) == []


def test_a_422_for_the_class_header_does_not_echo_it(two: Gateway) -> None:
    response, _ = two.post(data_class=CANARY)

    assert response.status_code == 422
    assert CANARY not in response.text


def test_a_synthetic_tenant_that_sends_personal_is_filtered_as_personal(
    global_first: Gateway,
) -> None:
    without, _ = global_first.post(tenant=SYNTHETIC_TENANT)
    assert without.json()["deployment"] == GLOBAL  # synthetic data may go global
    global_first.provider.called.clear()
    global_first.exporter.clear()

    response, run_id = global_first.post(tenant=SYNTHETIC_TENANT, data_class="personal")

    assert response.status_code == 200
    assert global_first.provider.called == [FIRST]  # the global one was skipped
    (row,) = global_first.rows(run_id)
    assert (row["deployment"], row["residency"], row["data_class"]) == (
        FIRST,
        "eu-region",
        "personal",
    )
    assert global_first.span("gateway.chat").attributes["meridian.data_class"] == (
        "personal"
    )
    (attempt,) = [
        s
        for s in global_first.exporter.get_finished_spans()
        if s.name == "gateway.attempt"
    ]
    assert attempt.attributes["meridian.data_class"] == "personal"


@pytest.mark.parametrize(
    ("tenant", "header", "used"),
    [
        (SYNTHETIC_TENANT, None, "synthetic"),
        (SYNTHETIC_TENANT, "synthetic", "synthetic"),
        (SYNTHETIC_TENANT, "internal", "internal"),
        (SYNTHETIC_TENANT, "personal", "personal"),
        (PERSONAL_TENANT, None, "personal"),
        (PERSONAL_TENANT, "synthetic", "personal"),
        (PERSONAL_TENANT, "internal", "personal"),
        (PERSONAL_TENANT, "personal", "personal"),
    ],
)
def test_the_class_used_is_the_higher_one_in_the_row_and_on_the_span(
    two: Gateway, tenant: str, header: str | None, used: str
) -> None:
    response, run_id = two.post(tenant=tenant, data_class=header)

    assert response.status_code == 200
    (row,) = two.rows(run_id)
    assert row["data_class"] == used
    assert two.span("gateway.chat").attributes["meridian.data_class"] == used


@pytest.mark.parametrize("path", [CHAT, EMBEDDINGS])
@pytest.mark.parametrize("tenant", [PERSONAL_TENANT, SYNTHETIC_TENANT])
def test_a_special_class_is_a_403_audited_as_special_data_before_any_limit_or_ledger(
    registry_with: Callable[..., Path],
    build: Callable[[Path], Gateway],
    path: str,
    tenant: str,
) -> None:
    gateway = build(registry_with(FIRST, SECOND, requests=1))

    response, run_id = gateway.post(path, tenant=tenant, data_class="special")

    assert response.status_code == 403
    assert response.json() == {"detail": REFUSED}
    assert gateway.provider.called == []
    assert gateway.usage() == []  # no reservation
    assert gateway.counters() == []
    (row,) = gateway.rows(run_id)
    assert (row["outcome"], row["reason"], row["data_class"]) == (
        "refused",
        "special-data",
        "special",
    )
    assert row["input_tokens"] is None
    assert gateway.span("gateway." + path.rsplit("/", 1)[1]).attributes[
        "meridian.refusal"
    ] == ("special-data")
    assert gateway.calls_counted() == {("refused", "special-data"): 1}
    # No admission: the one request a window of this tenant allows is unspent.
    if tenant == PERSONAL_TENANT:
        assert gateway.post(path)[0].status_code == 200


# ── 2. redaction, on every request, before anything sees the text ───────────
@pytest.mark.parametrize("tenant", [PERSONAL_TENANT, SYNTHETIC_TENANT])
def test_a_chat_request_reaches_the_provider_with_its_identifiers_replaced(
    two: Gateway, tenant: str
) -> None:
    response, _ = two.post(CHAT, PII_BODY, tenant=tenant)

    assert response.status_code == 200
    (sent,) = two.provider.chats
    assert [(m.role, m.content) for m in sent.messages] == [
        ("system", SYSTEM_TEXT),
        ("user", REDACTED_USER_TEXT),
        ("assistant", "Understood."),
        ("user", "Again: [email]"),
    ]
    assert sent.max_output_tokens == 1024


def test_an_embedding_request_reaches_the_provider_with_its_identifiers_replaced(
    two: Gateway,
) -> None:
    response, _ = two.post(EMBEDDINGS)

    assert response.status_code == 200
    (sent,) = two.provider.embeds
    assert sent.inputs == ("Mail [email] or pay [card]", "A storm hit the roof.")


def test_a_request_with_nothing_to_redact_reaches_the_provider_unchanged(
    two: Gateway,
) -> None:
    two.post(CHAT, PLAIN_BODY)
    two.post(EMBEDDINGS, {"inputs": ["one", "two"]})

    assert [m.content for m in two.provider.chats[0].messages] == [
        "A storm hit the roof."
    ]
    assert two.provider.embeds[0].inputs == ("one", "two")


def test_a_json_message_still_parses_and_differs_only_in_the_redacted_values(
    two: Gateway,
) -> None:
    document = {
        "claim": {
            "id": "C-1042",
            "note": f"Reached at {EMAIL} on Monday",
            "payout": {"iban": IBAN, "card": CARD},
            "amount": 1250.5,
            "documents": ["photo-1.jpg", "ő-ü.pdf"],
        },
        "policy": None,
    }
    content = json.dumps(document, ensure_ascii=False)
    body = {"messages": [{"role": "user", "content": content}]}

    response, _ = two.post(CHAT, body)

    assert response.status_code == 200
    (sent,) = two.provider.chats
    assert json.loads(sent.messages[0].content) == {
        "claim": {
            "id": "C-1042",
            "note": "Reached at [email] on Monday",
            "payout": {"iban": "[iban]", "card": "[card]"},
            "amount": 1250.5,
            "documents": ["photo-1.jpg", "ő-ü.pdf"],
        },
        "policy": None,
    }


def test_the_estimate_is_computed_from_the_redacted_text(two: Gateway) -> None:
    original = ChatRequest.model_validate(PII_BODY)
    redacted = ChatRequest.model_validate(
        {
            "messages": [
                {"role": m.role, "content": c}
                for m, c in zip(
                    original.messages,
                    (
                        SYSTEM_TEXT,
                        REDACTED_USER_TEXT,
                        "Understood.",
                        "Again: [email]",
                    ),
                    strict=True,
                )
            ]
        }
    )
    assert chat_estimate(redacted).tokens < chat_estimate(original).tokens

    two.post(CHAT, PII_BODY)

    (reserved,) = two.usage()
    assert reserved["reserved_tokens"] == chat_estimate(redacted).tokens


def test_the_embedding_estimate_is_computed_from_the_redacted_text(
    registry_with: Callable[..., Path], build: Callable[[Path], Gateway]
) -> None:
    gateway = build(registry_with(FIRST))
    original = EmbeddingRequest.model_validate(EMBEDDING_BODY)
    redacted = EmbeddingRequest(
        inputs=("Mail [email] or pay [card]", "A storm hit the roof.")
    )
    assert embedding_estimate(redacted).tokens < embedding_estimate(original).tokens

    gateway.post(EMBEDDINGS)

    (reserved,) = gateway.usage()
    assert reserved["reserved_tokens"] == embedding_estimate(redacted).tokens


def test_the_rate_limiter_admits_the_redacted_estimate_not_the_original(
    registry_with: Callable[..., Path], build: Callable[[Path], Gateway]
) -> None:
    redacted_tokens = chat_estimate(
        ChatRequest.model_validate(
            {"messages": [{"role": "user", "content": REDACTED_USER_TEXT}]}
        )
    ).tokens
    body = {"messages": [{"role": "user", "content": USER_TEXT}]}
    assert chat_estimate(ChatRequest.model_validate(body)).tokens > redacted_tokens + 5
    # A window that fits the redacted request and not the original.
    gateway = build(registry_with(FIRST, tokens_per_minute=redacted_tokens + 5))

    response, _ = gateway.post(CHAT, body)

    assert response.status_code == 200


@pytest.mark.parametrize("path", [CHAT, EMBEDDINGS])
def test_the_span_carries_the_number_of_redactions_and_nothing_of_them(
    two: Gateway, path: str
) -> None:
    body = PII_BODY if path == CHAT else EMBEDDING_BODY
    expected = REDACTIONS_IN_PII_BODY if path == CHAT else 2

    two.post(path, body)

    span = two.span("gateway." + path.rsplit("/", 1)[1])
    assert span.attributes["meridian.redactions"] == expected
    assert type(span.attributes["meridian.redactions"]) is int  # a count, no kinds


def test_no_redaction_count_is_set_when_nothing_was_redacted(two: Gateway) -> None:
    two.post(CHAT)

    assert "meridian.redactions" not in two.span("gateway.chat").attributes


def test_a_refused_request_still_says_how_many_values_were_redacted(
    two: Gateway,
) -> None:
    response, _ = two.post(CHAT, PII_BODY, tenant="no-such-tenant")

    assert response.status_code == 403
    assert two.provider.called == []
    span = two.span("gateway.chat")
    assert span.attributes["meridian.redactions"] == REDACTIONS_IN_PII_BODY


@pytest.mark.parametrize("scenario", ["completed", "failed", "refused"])
@pytest.mark.parametrize("path", [CHAT, EMBEDDINGS])
def test_no_original_value_is_in_a_span_a_row_a_metric_or_a_log(
    two: Gateway,
    caplog: pytest.LogCaptureFixture,
    path: str,
    scenario: str,
) -> None:
    caplog.set_level(logging.DEBUG)
    if scenario == "failed":
        two.provider.outcomes[FIRST] = Outcome(ProviderError("rejected", 400))
        two.provider.outcomes[EMBEDDING] = Outcome(ProviderError("rejected", 400))
    body = PII_BODY if path == CHAT else EMBEDDING_BODY
    tenant = "no-such-tenant" if scenario == "refused" else PERSONAL_TENANT

    response, run_id = two.post(path, body, tenant=tenant)

    assert (
        response.status_code
        == {"completed": 200, "failed": 502, "refused": 403}[scenario]
    )
    written = two.everything_it_wrote()
    logged = caplog.text + " ".join(r.getMessage() for r in caplog.records)
    for original in ORIGINALS:
        assert original not in written
        assert original not in logged
        assert original not in response.text
        assert_spans_hold_no_exception_and_no_canary(two.exporter, original)
    assert len(two.rows(run_id)) == 1


# ── 3. the provider's content filter ────────────────────────────────────────
def filtered(status: int | None = 400) -> Outcome:
    return Outcome(ProviderError("filtered", status))


@pytest.mark.parametrize(
    ("failure", "closed"),
    [
        pytest.param(ProviderError("filtered", 400), "release", id="prompt-400"),
        pytest.param(ProviderError("filtered"), "keep", id="completion-withheld"),
    ],
)
def test_a_filtered_prompt_is_released_and_a_withheld_completion_is_kept(
    failure: ProviderError, closed: str
) -> None:
    assert closing_for(failure) == closed


@pytest.mark.parametrize(
    ("status", "state"), [(400, "released"), (None, "kept")], ids=["400", "none"]
)
def test_the_reservation_of_a_filtered_attempt_is_closed_by_its_status(
    two: Gateway, status: int | None, state: str
) -> None:
    two.provider.outcomes[FIRST] = filtered(status)

    two.post()

    assert [(u["deployment"], u["state"]) for u in two.usage()] == [(FIRST, state)]


@pytest.mark.parametrize("path", [CHAT, EMBEDDINGS])
def test_a_filtered_first_attempt_is_a_400_with_no_second_attempt(
    registry_with: Callable[..., Path],
    build: Callable[[Path], Gateway],
    path: str,
) -> None:
    gateway = build(registry_with(FIRST, SECOND))
    gateway.provider.outcomes[FIRST] = filtered()
    gateway.provider.outcomes[EMBEDDING] = filtered()

    response, run_id = gateway.post(path)

    assert response.status_code == 400
    assert response.json() == {"detail": PROVIDER_FILTERED}
    assert gateway.provider.called == [FIRST if path == CHAT else EMBEDDING]
    (row,) = gateway.rows(run_id)
    assert (row["outcome"], row["reason"], row["http_status"]) == (
        "failed",
        "filtered",
        400,
    )
    assert gateway.calls_counted() == {("failed", "filtered"): 1}
    parent = gateway.span("gateway." + path.rsplit("/", 1)[1])
    assert parent.attributes["error.type"] == "filtered"


def test_a_filtered_call_leaves_the_circuit_closed(two: Gateway) -> None:
    two.provider.outcomes[FIRST] = filtered()
    for _ in range(FAILURE_THRESHOLD + 1):
        response, _ = two.post()
        assert response.status_code == 400
    two.provider.outcomes[FIRST] = OK
    two.provider.called.clear()

    response, run_id = two.post()

    assert response.json()["deployment"] == FIRST
    assert two.provider.called == [FIRST]
    (row,) = two.rows(run_id)  # no "skipped ... circuit-open" row
    assert row["outcome"] == "completed"


def test_a_filtered_answer_carries_no_word_of_the_provider(two: Gateway) -> None:
    two.provider.outcomes[FIRST] = Outcome(ProviderError("filtered", 400))

    response, run_id = two.post(
        CHAT, {"messages": [{"role": "user", "content": CANARY}]}
    )

    assert response.status_code == 400
    assert CANARY not in response.text
    assert CANARY not in two.everything_it_wrote()
    assert_spans_hold_no_exception_and_no_canary(two.exporter, CANARY)
    assert len(two.rows(run_id)) == 1


def test_a_malformed_body_is_still_a_422_and_never_a_400(two: Gateway) -> None:
    headers = {
        "X-Meridian-Tenant": PERSONAL_TENANT,
        "X-Meridian-Agent": "claims-triage",
        "X-Meridian-Run": str(uuid.uuid4()),
        "Content-Type": "application/json",
    }

    statuses = [
        two.client.post(CHAT, json={"messages": []}, headers=headers).status_code,
        two.client.post(EMBEDDINGS, json={"inputs": []}, headers=headers).status_code,
        two.client.post(CHAT, content=b"{not json", headers=headers).status_code,
        two.client.post(EMBEDDINGS, content=b"[1,", headers=headers).status_code,
    ]

    assert statuses == [422, 422, 422, 422]


@pytest.mark.parametrize("path", [CHAT, EMBEDDINGS])
def test_both_routes_list_400_for_the_content_filter(two: Gateway, path: str) -> None:
    responses = two.app.openapi()["paths"][path]["post"]["responses"]

    assert "content filter" in responses["400"]["description"]
    assert responses["400"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "/ErrorBody"
    )


def test_a_header_value_is_listed_as_an_optional_header_of_both_routes(
    two: Gateway,
) -> None:
    paths = two.app.openapi()["paths"]

    for path in (CHAT, EMBEDDINGS):
        declared = {
            (p["in"], p["name"]): p["required"]
            for p in paths[path]["post"]["parameters"]
        }
        assert declared[("header", DATA_CLASS_HEADER)] is False
