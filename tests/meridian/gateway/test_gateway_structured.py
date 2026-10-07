"""POST /v1/chat with a response schema (S051).

Live mode over a scripted provider that records what reached it, the real
ledger on the throwaway PostgreSQL, and replay mode over the real registry. A
registry in which an agent declares ``structured_outputs`` cannot list a chat
deployment that does not (the registry checks refuse it), so the tests of a
route with a deployment that cannot honour a schema swap the loaded registry
for a copy with the declaration cleared: that is the gateway's second line, the
one that holds when a registry is wrong.

The words of the schema are test words (``quokka``) that nothing else in a
request holds, so a search for them finds a leak.
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
from fastapi.testclient import TestClient
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from servicesupport import (
    REGISTRY_DIR,
    REPLAY_ENTRY,
    SECOND_DEPLOYMENT_YAML,
    FakeClock,
    audit_events,
    owner_rows,
    pin_chat_route,
)

from meridian.platform.common.http import REFUSED
from meridian.platform.common.metrics import make_meter_provider
from meridian.platform.common.telemetry import make_tracer_provider
from meridian.platform.gateway import app as gateway_app
from meridian.platform.gateway.app import (
    COMPLETION_HEADER,
    COMPLETION_WITHHELD,
    PROVIDER_FAILED,
    REFUSAL_CONTENT_FILTER,
    REFUSAL_HEADER,
    create_app,
)
from meridian.platform.gateway.models import (
    RESPONSE_SCHEMA_REFUSAL,
    ChatRequest,
    EmbeddingRequest,
)
from meridian.platform.gateway.providers.base import (
    EmbeddingReply,
    ProviderError,
    ProviderReply,
)
from meridian.platform.gateway.replay import REPLAY_PREFIX
from meridian.platform.gateway.settings import GatewaySettings
from meridian.platform.registry import Registry, load_registry
from meridian.platform.registry.checks import run_checks
from meridian.platform.registry.models import Deployment

CHAT = "/v1/chat"
EMBEDDINGS = "/v1/embeddings"
FIRST = "aoai-sdc-gpt-4o"
SECOND = "aoai-sdc-gpt-4o-second"
TENANT = "claims-triage"
AGENT = "claims-triage"
SCHEMA_WORD = "quokka"
SCHEMA = {
    "type": "object",
    "properties": {
        "quokka_field": {"type": "string", "enum": ["quokka_one", "quokka_two"]},
        "quokka_note": {"type": ["string", "null"]},
    },
    "required": ["quokka_field", "quokka_note"],
    "additionalProperties": False,
}
# Counted without the module under test: the compact JSON, keys sorted, UTF-8
# bytes over three, rounded up.
SCHEMA_TOKENS = -(
    -len(
        json.dumps(
            SCHEMA, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
    )
    // 3
)
EMAIL = "ana.kovacs@example.com"
PLAIN_BODY = {"messages": [{"role": "user", "content": "A storm hit the roof."}]}
SCHEMA_BODY = PLAIN_BODY | {"response_schema": SCHEMA}
CANARY = "CANARY_schema_text"
ANSWER_TEXT = "a fake answer"


class RecordingProvider:
    """Answers or raises per deployment ID and records what it was sent."""

    def __init__(self) -> None:
        self.errors: dict[str, ProviderError] = {}
        self.called: list[str] = []
        self.chats: list[ChatRequest] = []

    def chat(
        self, deployment: Deployment, request: ChatRequest, *, timeout_seconds: float
    ) -> ProviderReply:
        self.called.append(deployment.id)
        self.chats.append(request)
        if deployment.id in self.errors:
            raise self.errors[deployment.id]
        return ProviderReply(
            text=ANSWER_TEXT,
            finish_reason="stop",
            model="gpt-4o-2024-11-20",
            input_tokens=11,
            output_tokens=7,
        )

    def embed(  # never reached: the schema is refused before any call
        self, deployment: Deployment, request: EmbeddingRequest, **_: object
    ) -> EmbeddingReply:
        raise AssertionError("an embedding was asked for")


@dataclass(slots=True)
class Gateway:
    client: TestClient
    provider: RecordingProvider
    exporter: InMemorySpanExporter
    reader: InMemoryMetricReader
    database: DatabaseHandle

    def post(
        self, body: object, path: str = CHAT, agent: str = AGENT
    ) -> tuple[httpx.Response, uuid.UUID]:
        run_id = uuid.uuid4()
        headers = {
            "X-Meridian-Tenant": TENANT,
            "X-Meridian-Agent": agent,
            "X-Meridian-Run": str(run_id),
        }
        return self.client.post(path, json=body, headers=headers), run_id

    def rows(self, run_id: uuid.UUID) -> list[dict]:
        return audit_events(self.database, run_id)

    def reserved(self) -> list[int]:
        rows = owner_rows(
            self.database,
            "SELECT reserved_tokens FROM gateway.usage ORDER BY reserved_at",
        )
        return [row[0] for row in rows]

    def chat_span(self) -> ReadableSpan:
        (span,) = [
            s for s in self.exporter.get_finished_spans() if s.name == "gateway.chat"
        ]
        return span

    def spans_as_text(self) -> str:
        return " ".join(
            str([span.name, span.status.description, span.attributes, span.events])
            for span in self.exporter.get_finished_spans()
        )

    def calls_counted(self) -> dict[tuple, float]:
        found: dict[tuple, float] = {}
        data = self.reader.get_metrics_data()
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
        """Every audit row, ledger row, span and metric as one string."""
        audit_rows = owner_rows(self.database, "SELECT * FROM audit.events")
        usage_rows = owner_rows(self.database, "SELECT * FROM gateway.usage")
        return str([audit_rows, usage_rows, self.calls_counted()]) + (
            self.spans_as_text()
        )


# ── registries ──────────────────────────────────────────────────────────────
@pytest.fixture
def two_honouring(plant: Callable[..., Path]) -> Path:
    """The real first deployment and a test-only second one, both declaring
    ``structured_outputs``, as the only chat route; the declaring agent and the
    registry checks agree."""
    plant(("models.yaml", REPLAY_ENTRY, SECOND_DEPLOYMENT_YAML + REPLAY_ENTRY))
    directory = pin_chat_route(plant(), FIRST, SECOND)
    assert run_checks(load_registry(directory)) == ()
    return directory


@pytest.fixture
def agent_not_declaring(plant: Callable[..., Path], two_honouring: Path) -> Path:
    """The same registry with the agent's declaration removed: it passes the
    checks, because no agent asks for a schema."""
    directory = plant(("agents.yaml", "    structured_outputs: true\n", ""))
    assert run_checks(load_registry(directory)) == ()
    return directory


def clearing(registry: Registry, *deployment_ids: str) -> Registry:
    """The registry with ``structured_outputs`` cleared on those deployments."""
    return registry.model_copy(
        update={
            "deployments": tuple(
                d.model_copy(update={"structured_outputs": False})
                if d.id in deployment_ids
                else d
                for d in registry.deployments
            )
        }
    )


@pytest.fixture
def clear_declaration(monkeypatch: pytest.MonkeyPatch) -> Callable[..., None]:
    """Make the gateway load a registry whose named deployments no longer
    declare ``structured_outputs``, which the checks would refuse on disk."""

    def clear(*deployment_ids: str) -> None:
        real = gateway_app.load_registry
        monkeypatch.setattr(
            gateway_app,
            "load_registry",
            lambda directory: clearing(real(directory), *deployment_ids),
        )

    return clear


@pytest.fixture
def build(fresh_database: DatabaseHandle) -> Callable[..., Gateway]:
    def make(registry_dir: Path, mode: str = "live") -> Gateway:
        exporter, reader = InMemorySpanExporter(), InMemoryMetricReader()
        provider = RecordingProvider()
        settings = GatewaySettings(
            registry_dir=registry_dir,
            mode=mode,  # type: ignore[arg-type]
            environment="test",
            database_url=fresh_database.dsn("model_gateway"),
        )
        app = create_app(
            settings,
            tracer_provider=make_tracer_provider("model-gateway", exporter),
            meter_provider=make_meter_provider("model-gateway", reader),
            providers={"azure-openai": provider} if mode == "live" else None,
            clock=FakeClock(),
        )
        return Gateway(TestClient(app), provider, exporter, reader, fresh_database)

    return make


# ── the schema reaches the provider as it is ────────────────────────────────
def test_the_provider_receives_the_schema_unchanged(
    build: Callable[..., Gateway], two_honouring: Path
) -> None:
    gateway = build(two_honouring)

    response, _ = gateway.post(SCHEMA_BODY)

    assert response.status_code == 200
    (sent,) = gateway.provider.chats
    assert sent.response_schema == SCHEMA
    assert gateway.provider.called == [FIRST]


def test_the_schema_is_unchanged_when_the_messages_are_redacted(
    build: Callable[..., Gateway], two_honouring: Path
) -> None:
    gateway = build(two_honouring)
    body = {
        "messages": [{"role": "user", "content": f"Mail {EMAIL} about the roof."}],
        "response_schema": SCHEMA,
    }

    response, _ = gateway.post(body)

    assert response.status_code == 200
    (sent,) = gateway.provider.chats
    assert EMAIL not in sent.messages[0].content  # redaction ran
    assert "[email]" in sent.messages[0].content
    assert sent.response_schema == SCHEMA


def test_a_request_without_a_schema_behaves_as_before(
    build: Callable[..., Gateway], two_honouring: Path
) -> None:
    gateway = build(two_honouring)

    response, run_id = gateway.post(PLAIN_BODY)

    assert response.status_code == 200
    (sent,) = gateway.provider.chats
    assert sent.response_schema is None
    assert "meridian.response_schema" not in gateway.chat_span().attributes
    (row,) = gateway.rows(run_id)
    assert (row["outcome"], row["deployment"]) == ("completed", FIRST)


def test_a_request_without_a_schema_from_an_undeclaring_agent_is_answered(
    build: Callable[..., Gateway], agent_not_declaring: Path
) -> None:
    gateway = build(agent_not_declaring)

    response, _ = gateway.post(PLAIN_BODY)

    assert response.status_code == 200


def test_the_span_says_a_schema_was_sent_and_never_what_it_was(
    build: Callable[..., Gateway], two_honouring: Path
) -> None:
    gateway = build(two_honouring)

    gateway.post(SCHEMA_BODY)

    assert gateway.chat_span().attributes["meridian.response_schema"] is True


# ── a schema outside the subset is a 422 that says nothing of it ────────────
def test_a_schema_outside_the_subset_is_a_422_with_one_fixed_sentence(
    build: Callable[..., Gateway],
    two_honouring: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    gateway = build(two_honouring)
    bad = {
        "type": "object",
        "properties": {CANARY: {"type": "string", "enum": [CANARY], CANARY: CANARY}},
        "required": [CANARY],
        "additionalProperties": CANARY,
    }

    response, run_id = gateway.post(PLAIN_BODY | {"response_schema": bad})

    assert response.status_code == 422
    assert response.json() == {
        "detail": [
            {
                "loc": ["body", "response_schema"],
                "msg": f"Value error, {RESPONSE_SCHEMA_REFUSAL}",
                "type": "value_error",
            }
        ]
    }
    assert CANARY not in response.text
    assert CANARY not in caplog.text + " ".join(r.getMessage() for r in caplog.records)
    assert gateway.provider.called == []
    assert gateway.rows(run_id) == []
    assert gateway.reserved() == []


def test_a_schema_that_is_not_an_object_is_a_422_that_echoes_nothing(
    build: Callable[..., Gateway], two_honouring: Path
) -> None:
    gateway = build(two_honouring)

    response, _ = gateway.post(PLAIN_BODY | {"response_schema": CANARY})

    assert response.status_code == 422
    assert CANARY not in response.text


def test_a_schema_nested_too_deep_for_json_is_refused_without_a_500(
    build: Callable[..., Gateway], two_honouring: Path
) -> None:
    gateway = build(two_honouring)
    nested = "[" * 3000 + "]" * 3000
    raw = (
        '{"messages": [{"role": "user", "content": "x"}], '
        f'"response_schema": {nested}}}'
    )

    response = gateway.client.post(
        CHAT,
        content=raw,
        headers={
            "Content-Type": "application/json",
            "X-Meridian-Tenant": TENANT,
            "X-Meridian-Agent": AGENT,
            "X-Meridian-Run": str(uuid.uuid4()),
        },
    )

    assert response.status_code in {400, 422}
    assert gateway.provider.called == []


def test_the_embeddings_route_refuses_the_field(
    build: Callable[..., Gateway], two_honouring: Path
) -> None:
    gateway = build(two_honouring)

    response, _ = gateway.post(
        {"inputs": ["a storm"], "response_schema": SCHEMA}, EMBEDDINGS
    )

    assert response.status_code == 422
    assert SCHEMA_WORD not in response.text
    assert gateway.provider.called == []


# ── the two refusals ────────────────────────────────────────────────────────
def test_an_agent_that_does_not_declare_the_field_is_a_403_audited_and_not_called(
    build: Callable[..., Gateway], agent_not_declaring: Path
) -> None:
    gateway = build(agent_not_declaring)

    response, run_id = gateway.post(SCHEMA_BODY)

    assert response.status_code == 403
    assert response.json() == {"detail": REFUSED}
    assert gateway.provider.called == []
    assert gateway.reserved() == []  # no ledger row
    (row,) = gateway.rows(run_id)
    assert (row["outcome"], row["reason"], row["data_class"]) == (
        "refused",
        "schema-not-allowed",
        "personal",
    )
    attributes = gateway.chat_span().attributes
    assert attributes["meridian.refusal"] == "schema-not-allowed"
    assert "meridian.response_schema" not in attributes
    assert gateway.calls_counted() == {("refused", "schema-not-allowed"): 1}
    assert SCHEMA_WORD not in gateway.everything_it_wrote()


def test_an_agent_the_registry_does_not_know_never_reaches_the_schema_check(
    build: Callable[..., Gateway], two_honouring: Path
) -> None:
    gateway = build(two_honouring)

    response, run_id = gateway.post(SCHEMA_BODY, agent="some-other-agent")

    assert response.status_code == 403
    (row,) = gateway.rows(run_id)
    assert row["reason"] == "agent-not-allowed"


def test_a_first_candidate_that_cannot_honour_a_schema_is_skipped_for_the_second(
    build: Callable[..., Gateway],
    two_honouring: Path,
    clear_declaration: Callable[..., None],
) -> None:
    clear_declaration(FIRST)
    gateway = build(two_honouring)

    response, run_id = gateway.post(SCHEMA_BODY)

    assert response.status_code == 200
    assert response.json()["deployment"] == SECOND
    assert gateway.provider.called == [SECOND]  # one attempt, none at the first
    (row,) = gateway.rows(run_id)
    assert (row["outcome"], row["deployment"]) == ("completed", SECOND)
    attributes = gateway.chat_span().attributes
    assert (attributes["meridian.attempts"], attributes["meridian.skipped"]) == (1, 0)


def test_without_a_schema_the_same_registry_still_tries_the_first_candidate(
    build: Callable[..., Gateway],
    two_honouring: Path,
    clear_declaration: Callable[..., None],
) -> None:
    clear_declaration(FIRST)
    gateway = build(two_honouring)

    response, _ = gateway.post(PLAIN_BODY)

    assert response.status_code == 200
    assert gateway.provider.called == [FIRST]


def test_a_failing_honouring_candidate_never_falls_back_to_free_text(
    build: Callable[..., Gateway],
    two_honouring: Path,
    clear_declaration: Callable[..., None],
) -> None:
    clear_declaration(SECOND)
    gateway = build(two_honouring)
    gateway.provider.errors[FIRST] = ProviderError("unavailable", 503)

    response, _ = gateway.post(SCHEMA_BODY)

    assert response.status_code == 502
    assert response.json() == {"detail": PROVIDER_FAILED}
    assert gateway.provider.called == [FIRST]  # the second cannot honour a schema


def test_no_candidate_that_honours_a_schema_is_a_403_audited_and_not_called(
    build: Callable[..., Gateway],
    two_honouring: Path,
    clear_declaration: Callable[..., None],
) -> None:
    clear_declaration(FIRST, SECOND)
    gateway = build(two_honouring)

    response, run_id = gateway.post(SCHEMA_BODY)

    assert response.status_code == 403
    assert response.json() == {"detail": REFUSED}
    assert gateway.provider.called == []
    assert gateway.reserved() == []
    (row,) = gateway.rows(run_id)
    assert (row["outcome"], row["reason"]) == ("refused", "no-schema-deployment")
    assert gateway.calls_counted() == {("refused", "no-schema-deployment"): 1}
    assert SCHEMA_WORD not in gateway.everything_it_wrote()


def test_a_models_refusal_of_a_structured_request_is_the_400_with_no_fallback(
    build: Callable[..., Gateway], two_honouring: Path
) -> None:
    gateway = build(two_honouring)
    gateway.provider.errors[FIRST] = ProviderError("filtered")

    response, _ = gateway.post(SCHEMA_BODY)

    assert response.status_code == 400
    # The model ran and answered with a refusal and no content: the ledger
    # keeps its reservation, so the wire marks it as a withheld completion
    # beside the filter's own mark (S069).
    assert response.headers[REFUSAL_HEADER] == REFUSAL_CONTENT_FILTER
    assert response.headers[COMPLETION_HEADER] == COMPLETION_WITHHELD
    assert gateway.provider.called == [FIRST]


# ── the ledger and the limiter see the schema ───────────────────────────────
def test_the_reservation_is_higher_by_exactly_the_schemas_estimate(
    build: Callable[..., Gateway], two_honouring: Path
) -> None:
    gateway = build(two_honouring)

    gateway.post(PLAIN_BODY)
    gateway.post(SCHEMA_BODY)

    plain, asking = gateway.reserved()
    assert SCHEMA_TOKENS > 0
    assert asking - plain == SCHEMA_TOKENS


# ── nothing of the schema is kept ───────────────────────────────────────────
def test_no_audit_row_span_metric_ledger_row_or_log_line_holds_a_word_of_the_schema(
    build: Callable[..., Gateway],
    two_honouring: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    gateway = build(two_honouring)

    response, run_id = gateway.post(SCHEMA_BODY)

    assert response.status_code == 200
    assert gateway.provider.chats[0].response_schema == SCHEMA  # it was sent
    assert len(gateway.rows(run_id)) == 1
    logged = caplog.text + " ".join(r.getMessage() for r in caplog.records)
    assert SCHEMA_WORD not in logged
    assert SCHEMA_WORD not in gateway.everything_it_wrote()
    assert SCHEMA_WORD not in response.text


# ── replay mode ─────────────────────────────────────────────────────────────
def test_replay_mode_answers_a_schema_request_with_the_replay_text(
    build: Callable[..., Gateway],
) -> None:
    gateway = build(REGISTRY_DIR, mode="replay")

    plain, _ = gateway.post(PLAIN_BODY)
    asking, run_id = gateway.post(SCHEMA_BODY)

    assert asking.status_code == 200
    body = asking.json()
    assert body["deployment"] == "replay-chat"
    assert body["output"]["text"].startswith(REPLAY_PREFIX)
    assert body["output"]["text"] == plain.json()["output"]["text"]
    with pytest.raises(json.JSONDecodeError):
        json.loads(body["output"]["text"])
    (row,) = gateway.rows(run_id)
    assert (row["outcome"], row["deployment"]) == ("completed", "replay-chat")
