"""A provider's token counts are bounded before they reach the ledger (S058).

``walk.py`` settles whatever a reply says it used. ``operations.py`` holds a
reply to the bound first: an input count over ``INPUT_TOKEN_FACTOR`` times the
estimate, or an output count over the wire's cap, is a bad response of that
deployment, so one absurd count is not charged to the tenant's day and month.
The unit tests need no database; the app tests run the real ledger on the
throwaway PostgreSQL over a scripted provider.
"""

import json
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import httpx
import pytest
from dbsupport import DatabaseHandle
from fastapi.testclient import TestClient
from servicesupport import (
    REGISTRY_DIR,
    REPLAY_ENTRY,
    REPO_ROOT,
    SECOND_DEPLOYMENT_YAML,
    audit_events,
    owner_rows,
    pin_chat_route,
    pin_embedding_route,
)

from meridian.platform.gateway.app import PROVIDER_FAILED, create_app
from meridian.platform.gateway.budget import chat_estimate, embedding_estimate
from meridian.platform.gateway.models import (
    MAX_OUTPUT_TOKENS,
    ChatRequest,
    EmbeddingRequest,
)
from meridian.platform.gateway.operations import (
    INPUT_TOKEN_FACTOR,
    chat_operation,
    embedding_operation,
)
from meridian.platform.gateway.providers.base import (
    EmbeddingReply,
    ProviderError,
    ProviderReply,
)
from meridian.platform.gateway.settings import GatewaySettings
from meridian.platform.registry import load_registry
from meridian.platform.registry.models import Deployment

CLAIM_TEXT = "claimant-secret-text-42"
CHAT_BODY = {"messages": [{"role": "user", "content": CLAIM_TEXT}]}
CHAT_REQUEST = ChatRequest.model_validate(CHAT_BODY)
EMBEDDING_BODY = {"inputs": [CLAIM_TEXT, "second text"]}
EMBEDDING_REQUEST = EmbeddingRequest.model_validate(EMBEDDING_BODY)
CHAT_BOUND = INPUT_TOKEN_FACTOR * chat_estimate(CHAT_REQUEST).input_tokens
EMBEDDING_BOUND = INPUT_TOKEN_FACTOR * embedding_estimate(EMBEDDING_REQUEST).tokens
ABSURD_TOKENS = 2_000_000_000  # under 2^31, so only the bound stops it
DIMENSIONS = 1024
FIRST = "aoai-sdc-gpt-4o"
SECOND = "aoai-sdc-gpt-4o-second"
EMBEDDER = "aoai-sdc-text-embedding-3-large"
TODAY = date(2026, 10, 1)
HTTP_BAD_GATEWAY = 502
RECORDING = REPO_ROOT / "data" / "evaluation" / "recordings" / "claims-triage.json"


# ── a provider that answers with the counts a test names ────────────────────
def chat_reply(input_tokens: int = 11, output_tokens: int = 7) -> ProviderReply:
    return ProviderReply(
        text="a fake answer",
        finish_reason="stop",
        model="gpt-4o-2024-11-20",
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


def embedding_reply(input_tokens: int = 9, inputs: int = 2) -> EmbeddingReply:
    vector = (0.5,) + (0.0,) * (DIMENSIONS - 1)
    return EmbeddingReply(
        embeddings=(vector,) * inputs,
        model="text-embedding-3-large",
        input_tokens=input_tokens,
    )


@dataclass(slots=True)
class FakeProvider:
    """Answers per deployment ID with the reply a test set, a good one otherwise."""

    chat_replies: dict[str, ProviderReply] = field(default_factory=dict)
    embedding_replies: dict[str, EmbeddingReply] = field(default_factory=dict)
    called: list[str] = field(default_factory=list)

    def chat(
        self, deployment: Deployment, request: ChatRequest, *, timeout_seconds: float
    ) -> ProviderReply:
        self.called.append(deployment.id)
        return self.chat_replies.get(deployment.id, chat_reply())

    def embed(
        self,
        deployment: Deployment,
        request: EmbeddingRequest,
        *,
        timeout_seconds: float,
    ) -> EmbeddingReply:
        self.called.append(deployment.id)
        return self.embedding_replies.get(deployment.id, embedding_reply())


def deployment_of(deployment_id: str) -> Deployment:
    found = load_registry(REGISTRY_DIR).deployment(deployment_id)
    assert found is not None
    return found


def chat_call(provider: FakeProvider) -> ProviderReply:
    operation = chat_operation(CHAT_REQUEST, chat_estimate(CHAT_REQUEST))
    return operation.call(provider, deployment_of(FIRST), 1.0)  # type: ignore[arg-type]


def embedding_call(provider: FakeProvider) -> EmbeddingReply:
    operation = embedding_operation(
        EMBEDDING_REQUEST, embedding_estimate(EMBEDDING_REQUEST)
    )
    return operation.call(
        provider,  # type: ignore[arg-type]
        deployment_of(EMBEDDER),
        1.0,
    )


# ── unit: chat ──────────────────────────────────────────────────────────────
def test_a_chat_input_count_at_the_bound_is_returned() -> None:
    reply = chat_reply(input_tokens=CHAT_BOUND)

    provider = FakeProvider({FIRST: reply})

    assert chat_call(provider) == reply


@pytest.mark.parametrize("input_tokens", [CHAT_BOUND + 1, -1, ABSURD_TOKENS])
def test_a_chat_input_count_out_of_bounds_is_a_bad_response(input_tokens: int) -> None:
    provider = FakeProvider({FIRST: chat_reply(input_tokens=input_tokens)})

    with pytest.raises(ProviderError) as raised:
        chat_call(provider)

    assert (raised.value.kind, raised.value.sent) == ("bad-response", True)


def test_a_chat_output_count_at_the_wires_cap_is_returned() -> None:
    reply = chat_reply(output_tokens=MAX_OUTPUT_TOKENS)

    assert chat_call(FakeProvider({FIRST: reply})) == reply


@pytest.mark.parametrize("output_tokens", [MAX_OUTPUT_TOKENS + 1, -1])
def test_a_chat_output_count_out_of_bounds_is_a_bad_response(
    output_tokens: int,
) -> None:
    provider = FakeProvider({FIRST: chat_reply(output_tokens=output_tokens)})

    with pytest.raises(ProviderError) as raised:
        chat_call(provider)

    assert raised.value.kind == "bad-response"


# ── unit: embeddings ────────────────────────────────────────────────────────
def test_an_embedding_input_count_at_the_bound_is_returned() -> None:
    reply = embedding_reply(input_tokens=EMBEDDING_BOUND)

    assert embedding_call(FakeProvider(embedding_replies={EMBEDDER: reply})) == reply


@pytest.mark.parametrize("input_tokens", [EMBEDDING_BOUND + 1, -1, ABSURD_TOKENS])
def test_an_embedding_input_count_out_of_bounds_is_a_bad_response(
    input_tokens: int,
) -> None:
    reply = embedding_reply(input_tokens=input_tokens)
    provider = FakeProvider(embedding_replies={EMBEDDER: reply})

    with pytest.raises(ProviderError) as raised:
        embedding_call(provider)

    assert (raised.value.kind, raised.value.sent) == ("bad-response", True)


# ── through the app ─────────────────────────────────────────────────────────
@dataclass(slots=True)
class Gateway:
    client: TestClient
    provider: FakeProvider
    database: DatabaseHandle

    def post(self, path: str, body: Mapping) -> tuple[httpx.Response, uuid.UUID]:
        run_id = uuid.uuid4()
        response = self.client.post(
            path,
            json=body,
            headers={
                "X-Meridian-Tenant": "claims-triage",
                "X-Meridian-Agent": "claims-triage",
                "X-Meridian-Run": str(run_id),
            },
        )
        return response, run_id

    def usage(self) -> list[tuple]:
        return owner_rows(
            self.database,
            "SELECT deployment, state, reserved_tokens, charged_tokens, "
            "input_tokens, output_tokens FROM gateway.usage ORDER BY reserved_at",
        )

    def day_counter(self) -> int:
        ((amount,),) = owner_rows(
            self.database,
            "SELECT amount FROM gateway.budget_counters WHERE kind = 'tokens-day'",
        )
        return amount


@pytest.fixture
def build(
    plant: Callable[..., Path], fresh_database: DatabaseHandle
) -> Callable[..., Gateway]:
    def make(*chat_candidates: str) -> Gateway:
        planted = plant(
            ("models.yaml", REPLAY_ENTRY, SECOND_DEPLOYMENT_YAML + REPLAY_ENTRY)
        )
        pin_chat_route(planted, *chat_candidates)
        pin_embedding_route(planted, EMBEDDER)
        provider = FakeProvider()
        settings = GatewaySettings(
            registry_dir=planted,
            mode="live",
            environment="test",
            database_url=fresh_database.dsn("model_gateway"),
        )
        app = create_app(
            settings,
            providers={"azure-openai": provider},  # type: ignore[dict-item]
            today=lambda: TODAY,
        )
        return Gateway(TestClient(app), provider, fresh_database)

    return make


def test_a_chat_reply_of_absurd_input_tokens_is_a_502_and_the_reservation_is_kept(
    build: Callable[..., Gateway],
) -> None:
    gateway = build(FIRST)
    gateway.provider.chat_replies[FIRST] = chat_reply(input_tokens=ABSURD_TOKENS)
    reserved = chat_estimate(CHAT_REQUEST).tokens

    response, run_id = gateway.post("/v1/chat", CHAT_BODY)

    assert response.status_code == HTTP_BAD_GATEWAY
    assert response.json() == {"detail": PROVIDER_FAILED}
    (row,) = audit_events(gateway.database, run_id)
    assert (row["outcome"], row["reason"], row["deployment"]) == (
        "failed",
        "bad-response",
        FIRST,
    )
    (usage,) = gateway.usage()
    assert usage[:4] == (FIRST, "kept", reserved, reserved)
    assert gateway.day_counter() == reserved


def test_an_embedding_reply_of_absurd_input_tokens_is_a_502_and_the_reservation_is_kept(
    build: Callable[..., Gateway],
) -> None:
    gateway = build(FIRST)
    gateway.provider.embedding_replies[EMBEDDER] = embedding_reply(ABSURD_TOKENS)
    reserved = embedding_estimate(EMBEDDING_REQUEST).tokens

    response, run_id = gateway.post("/v1/embeddings", EMBEDDING_BODY)

    assert response.status_code == HTTP_BAD_GATEWAY
    assert response.json() == {"detail": PROVIDER_FAILED}
    (row,) = audit_events(gateway.database, run_id)
    assert (row["outcome"], row["reason"], row["deployment"]) == (
        "failed",
        "bad-response",
        EMBEDDER,
    )
    (usage,) = gateway.usage()
    assert usage[:4] == (EMBEDDER, "kept", reserved, reserved)
    assert gateway.day_counter() == reserved


def test_a_count_out_of_bounds_from_the_first_candidate_is_followed_by_the_second(
    build: Callable[..., Gateway],
) -> None:
    gateway = build(FIRST, SECOND)
    gateway.provider.chat_replies[FIRST] = chat_reply(input_tokens=ABSURD_TOKENS)

    response, run_id = gateway.post("/v1/chat", CHAT_BODY)

    assert response.status_code == httpx.codes.OK
    assert response.json()["deployment"] == SECOND
    assert gateway.provider.called == [FIRST, SECOND]
    first, second = audit_events(gateway.database, run_id)
    assert (first["outcome"], first["reason"]) == ("failed", "bad-response")
    assert second["outcome"] == "completed"
    assert [(state, deployment) for deployment, state, *_ in gateway.usage()] == [
        ("kept", FIRST),
        ("settled", SECOND),
    ]


# ── the recording holds counts a real deployment gave ───────────────────────
def test_every_recorded_output_count_is_within_the_wires_cap() -> None:
    entries = json.loads(RECORDING.read_text(encoding="utf-8"))["entries"]

    assert entries
    assert all(0 <= e["output_tokens"] <= MAX_OUTPUT_TOKENS for e in entries.values())
