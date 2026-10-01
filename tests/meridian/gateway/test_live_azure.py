"""Real chat calls through the gateway in live mode (S010, S042). Opt-in.

Skipped unless ``MERIDIAN_LIVE_AZURE=1``, so neither CI nor a plain ``make
pytest`` ever reaches Azure. ``make gateway-live`` sets the three variables
below from Terraform's outputs and this ``az login``, and starts the throwaway
PostgreSQL the audit row needs. The prompt is synthetic. The test prints the
deployment, the model string the provider reports, the finish reason and the
token counts; never the endpoint, the tenant ID or any text.

The second test proves the fallback against Azure (S042): the first chat
candidate is made to fail in this process, before any request leaves, and the
real second deployment answers.
"""

import os
import uuid

import pytest
from dbsupport import DatabaseHandle
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from servicesupport import REGISTRY_DIR, audit_events, owner_rows

from meridian.platform.common.db import DATABASE_URL_ENV
from meridian.platform.common.env import REGISTRY_DIR_ENV
from meridian.platform.common.telemetry import make_tracer_provider
from meridian.platform.gateway.app import AZURE_KIND, create_app
from meridian.platform.gateway.models import ChatRequest
from meridian.platform.gateway.providers.azure_openai import (
    AzureOpenAIProvider,
    azure_cli_token_provider,
    refuse_sdk_environment,
)
from meridian.platform.gateway.providers.base import ProviderError, ProviderReply
from meridian.platform.gateway.settings import (
    CREDENTIAL_ENV,
    ENDPOINTS_ENV,
    ENVIRONMENT_ENV,
    MODE_ENV,
    TENANT_ID_ENV,
    GatewaySettings,
)
from meridian.platform.registry import load_registry
from meridian.platform.registry.models import Deployment

LIVE_ENV = "MERIDIAN_LIVE_AZURE"
PROMPT = "Reply with the single word: ready."
INJECTED_KIND = "unavailable"

pytestmark = pytest.mark.skipif(
    os.environ.get(LIVE_ENV) != "1",
    reason=f"opt-in: set {LIVE_ENV}=1 (make gateway-live)",
)


def test_one_synthetic_prompt_is_answered_by_the_routed_deployment_and_audited(
    fresh_database: DatabaseHandle,
) -> None:
    settings = GatewaySettings.from_env(
        {
            MODE_ENV: "live",
            ENVIRONMENT_ENV: "local",
            DATABASE_URL_ENV: fresh_database.dsn("model_gateway"),
            REGISTRY_DIR_ENV: str(REGISTRY_DIR),
            CREDENTIAL_ENV: "azure-cli",
            ENDPOINTS_ENV: os.environ[ENDPOINTS_ENV],
            TENANT_ID_ENV: os.environ[TENANT_ID_ENV],
        }
    )
    exporter = InMemorySpanExporter()
    client = TestClient(
        create_app(settings, tracer_provider=make_tracer_provider("gw", exporter))
    )
    run_id = uuid.uuid4()
    headers = {
        "X-Meridian-Tenant": "development",
        "X-Meridian-Agent": "claims-triage",
        "X-Meridian-Run": str(run_id),
    }
    body = {
        "messages": [{"role": "user", "content": PROMPT}],
        "max_output_tokens": 16,
    }

    response = client.post("/v1/chat", json=body, headers=headers)

    assert response.status_code == 200, response.text
    reply = response.json()
    (span,) = [s for s in exporter.get_finished_spans() if s.name == "gateway.chat"]
    provider_model = span.attributes["gen_ai.response.model"]
    usage = reply["usage"]
    print(f"\ndeployment:    {reply['deployment']}")
    print(f"provider model: {provider_model}")
    print(f"finish reason:  {reply['output']['finish_reason']}")
    print(
        f"tokens:         input {usage['input_tokens']}, "
        f"output {usage['output_tokens']}"
    )
    assert reply["mode"] == "live"
    assert reply["output"]["finish_reason"] in {"stop", "length"}
    assert usage["input_tokens"] > 0
    assert usage["output_tokens"] > 0
    (event,) = audit_events(fresh_database, run_id)
    assert (event["event"], event["outcome"], event["tenant"]) == (
        "model.call",
        "completed",
        "development",
    )
    assert event["deployment"] == reply["deployment"]
    assert (event["input_tokens"], event["output_tokens"]) == (
        usage["input_tokens"],
        usage["output_tokens"],
    )
    assert event["data_class"] == "synthetic"
    assert PROMPT not in str(event)
    # The ledger holds what Azure counted and what it costs at the registry's
    # price, and the estimate was not below Azure's own input count.
    ((state, reserved, counted, charged, input_tokens, output_tokens),) = owner_rows(
        fresh_database,
        "SELECT state, reserved_tokens, charged_tokens, charged_micro_eur, "
        "input_tokens, output_tokens FROM gateway.usage WHERE call_id = %s",
        (event["call_id"],),
    )
    print(f"cost:           {charged} micro-EUR")
    print(f"reservation:    {reserved} tokens reserved, {counted} charged")
    assert state == "settled"
    assert (input_tokens, output_tokens) == (
        usage["input_tokens"],
        usage["output_tokens"],
    )
    assert counted == input_tokens + output_tokens
    assert charged > 0
    assert reserved - body["max_output_tokens"] >= input_tokens


class FirstCandidateDown:
    """The real adapter, except that one deployment fails before any request
    leaves this process: the fault the fallback is for, injected."""

    def __init__(self, real: AzureOpenAIProvider, down: str) -> None:
        self._real = real
        self._down = down

    def chat(
        self, deployment: Deployment, request: ChatRequest, *, timeout_seconds: float
    ) -> ProviderReply:
        if deployment.id == self._down:
            raise ProviderError(INJECTED_KIND)
        return self._real.chat(deployment, request, timeout_seconds=timeout_seconds)


def test_with_the_first_candidate_down_the_second_deployment_answers(
    fresh_database: DatabaseHandle,
) -> None:
    route = load_registry(REGISTRY_DIR).route("chat")
    assert route is not None
    first, second = route.candidates[:2]
    # This test builds the adapter itself, past the gateway's start checks.
    refuse_sdk_environment()
    settings = GatewaySettings.from_env(
        {
            MODE_ENV: "live",
            ENVIRONMENT_ENV: "local",
            DATABASE_URL_ENV: fresh_database.dsn("model_gateway"),
            REGISTRY_DIR_ENV: str(REGISTRY_DIR),
            CREDENTIAL_ENV: "azure-cli",
            ENDPOINTS_ENV: os.environ[ENDPOINTS_ENV],
            TENANT_ID_ENV: os.environ[TENANT_ID_ENV],
        }
    )
    real = AzureOpenAIProvider(
        settings.azure_openai_endpoints,
        azure_cli_token_provider(settings.azure_tenant_id),
    )
    exporter = InMemorySpanExporter()
    client = TestClient(
        create_app(
            settings,
            tracer_provider=make_tracer_provider("gw", exporter),
            providers={AZURE_KIND: FirstCandidateDown(real, first)},
        )
    )
    run_id = uuid.uuid4()
    headers = {
        "X-Meridian-Tenant": "development",
        "X-Meridian-Agent": "claims-triage",
        "X-Meridian-Run": str(run_id),
    }
    body = {
        "messages": [{"role": "user", "content": PROMPT}],
        "max_output_tokens": 16,
    }

    try:
        response = client.post("/v1/chat", json=body, headers=headers)
    finally:
        real.close()

    assert response.status_code == 200, response.text
    reply = response.json()
    failed, completed = audit_events(fresh_database, run_id)
    usage = reply["usage"]
    print(f"\nfirst candidate: {failed['deployment']} {failed['outcome']}")
    print(f"reason:          {failed['reason']} (injected)")
    print(f"answered by:     {reply['deployment']}")
    print(
        f"tokens:          input {usage['input_tokens']}, "
        f"output {usage['output_tokens']}"
    )
    assert reply["deployment"] == second
    assert usage["output_tokens"] > 0
    assert (failed["deployment"], failed["outcome"], failed["reason"]) == (
        first,
        "failed",
        INJECTED_KIND,
    )
    assert (completed["deployment"], completed["outcome"]) == (second, "completed")
    assert failed["call_id"] == completed["call_id"] == uuid.UUID(reply["call_id"])
    assert completed["output_tokens"] == usage["output_tokens"]
    attempts = [s for s in exporter.get_finished_spans() if s.name == "gateway.attempt"]
    assert [s.attributes["meridian.deployment"] for s in attempts] == [first, second]
    (chat,) = [s for s in exporter.get_finished_spans() if s.name == "gateway.chat"]
    assert chat.attributes["meridian.attempts"] == 2
    assert PROMPT not in str(failed) + str(completed)
