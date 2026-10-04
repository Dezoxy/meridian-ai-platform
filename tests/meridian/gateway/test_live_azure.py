"""Real chat and embedding calls through the gateway in live mode (S010, S042,
S045). Opt-in.

Skipped unless ``MERIDIAN_LIVE_AZURE=1``, so neither CI nor a plain ``make
pytest`` ever reaches Azure. ``make gateway-live`` sets the three variables
below from Terraform's outputs and this ``az login``, and starts the throwaway
PostgreSQL the audit row needs. The prompt is synthetic. The test prints the
deployment, the model string the provider reports, the finish reason and the
token counts; never the endpoint, the tenant ID or any text.

The second test proves the fallback against Azure (S042): the first chat
candidate is made to fail in this process, before any request leaves, and the
real second deployment answers. The third embeds two synthetic texts (S045) and
prints the deployment, the model string, the vector length and the token counts;
never a vector or a text.

The fourth and fifth are structured outputs against Azure (S051). In the fourth
the prompt asks for one bare word and the request carries a response schema: the
answer is the schema's object, which only the schema explains. In the fifth the
triage's own call (``assess``, through the runtime's ``ModelClient``) asks its
question about one synthetic description by schema, and ``read_answer`` reads
the answer as strictly as it reads any. They print the keys, the verdict's
status and the clause number, which is one of the two sent; never the rationale.

The sixth caps a long answer at 1024 tokens as the tenant ``development`` (T-45)
and prints the status, the finish reason, the output tokens and the speed;
``make eval-record`` runs it with the evaluation's recording.
"""

import json
import os
import time
import uuid
from http import HTTPStatus

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
from meridian.runtime.model_client import ModelClient
from meridian.workloads.claims_triage.assessment import PROMPT_PROBE_CLAIM, assess
from meridian.workloads.claims_triage.wording import Clause

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


LONG_PROMPT = (
    "Write a story of about 900 words about an invented storm over an imaginary "
    "harbour town, in plain prose, with no lists and no headings."
)
LONG_OUTPUT_TOKENS = 1024
NOT_AVAILABLE = "n/a"


def test_an_answer_capped_at_1024_tokens_ends_inside_the_read_limit(
    fresh_database: DatabaseHandle,
) -> None:
    """T-45: the gateway's read limit against the output cap. A synthetic prompt
    asks for about 900 words under ``max_output_tokens=1024`` as the tenant
    ``development``; the call must end with an answer (200) or the gateway's own
    timeout (504), never a hang or another status. The output is the status, the
    finish reason, the output tokens, the seconds the call took and the tokens
    per second, and nothing else."""
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
    client = TestClient(
        create_app(
            settings,
            tracer_provider=make_tracer_provider("gw", InMemorySpanExporter()),
        )
    )
    headers = {
        "X-Meridian-Tenant": "development",
        "X-Meridian-Agent": "claims-triage",
        "X-Meridian-Run": str(uuid.uuid4()),
    }
    body = {
        "messages": [{"role": "user", "content": LONG_PROMPT}],
        "max_output_tokens": LONG_OUTPUT_TOKENS,
    }

    started = time.monotonic()
    response = client.post("/v1/chat", json=body, headers=headers)
    seconds = time.monotonic() - started

    finish, tokens, rate = NOT_AVAILABLE, NOT_AVAILABLE, NOT_AVAILABLE
    if response.status_code == HTTPStatus.OK:
        reply = response.json()
        finish = reply["output"]["finish_reason"]
        tokens = reply["usage"]["output_tokens"]
        rate = f"{tokens / seconds:.1f}"
    print(f"\nstatus:        {response.status_code}")
    print(f"finish reason: {finish}")
    print(f"output tokens: {tokens}")
    print(f"seconds:       {seconds:.1f}")
    print(f"tokens/second: {rate}")
    assert response.status_code in (HTTPStatus.OK, HTTPStatus.GATEWAY_TIMEOUT)


def test_synthetic_texts_are_embedded_by_the_routed_deployment_and_audited(
    fresh_database: DatabaseHandle,
) -> None:
    """The embedding route against Azure (S045): the vectors have the
    registry's length, the count and the order the request had, and the ledger
    holds what Azure counted at the embedding price. The output names the
    deployment, the model string, the length and the token counts; never a
    vector or a text."""
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
    body = {"inputs": [PROMPT, "Storm damage to the roof of a synthetic house."]}

    response = client.post("/v1/embeddings", json=body, headers=headers)

    assert response.status_code == 200, response.text
    reply = response.json()
    (span,) = [
        s for s in exporter.get_finished_spans() if s.name == "gateway.embeddings"
    ]
    provider_model = span.attributes["gen_ai.response.model"]
    usage = reply["usage"]
    print(f"\ndeployment:     {reply['deployment']}")
    print(f"provider model: {provider_model}")
    print(f"dimensions:     {reply['dimensions']}, vectors {len(reply['embeddings'])}")
    print(f"tokens:         input {usage['input_tokens']}")
    deployment = load_registry(REGISTRY_DIR).deployment(reply["deployment"])
    assert deployment is not None
    assert reply["mode"] == "live"
    assert reply["dimensions"] == deployment.dimensions == 1024
    assert len(reply["embeddings"]) == len(body["inputs"])
    assert all(len(v) == reply["dimensions"] for v in reply["embeddings"])
    assert reply["embeddings"][0] != reply["embeddings"][1]
    assert usage["input_tokens"] > 0
    (event,) = audit_events(fresh_database, run_id)
    assert (event["event"], event["outcome"], event["tenant"]) == (
        "model.call",
        "completed",
        "development",
    )
    assert event["deployment"] == reply["deployment"]
    assert (event["input_tokens"], event["output_tokens"]) == (
        usage["input_tokens"],
        0,
    )
    assert event["data_class"] == "synthetic"
    assert PROMPT not in str(event)
    ((state, reserved, counted, charged, input_tokens, output_tokens),) = owner_rows(
        fresh_database,
        "SELECT state, reserved_tokens, charged_tokens, charged_micro_eur, "
        "input_tokens, output_tokens FROM gateway.usage WHERE call_id = %s",
        (event["call_id"],),
    )
    print(f"cost:           {charged} micro-EUR")
    print(f"reservation:    {reserved} tokens reserved, {counted} charged")
    assert state == "settled"
    assert (input_tokens, output_tokens) == (usage["input_tokens"], 0)
    assert counted == input_tokens
    assert charged > 0
    assert reserved >= input_tokens  # the estimate was not below Azure's own count


def live_settings(database: DatabaseHandle) -> GatewaySettings:
    return GatewaySettings.from_env(
        {
            MODE_ENV: "live",
            ENVIRONMENT_ENV: "local",
            DATABASE_URL_ENV: database.dsn("model_gateway"),
            REGISTRY_DIR_ENV: str(REGISTRY_DIR),
            CREDENTIAL_ENV: "azure-cli",
            ENDPOINTS_ENV: os.environ[ENDPOINTS_ENV],
            TENANT_ID_ENV: os.environ[TENANT_ID_ENV],
        }
    )


WORD_SCHEMA = {
    "type": "object",
    "properties": {"word": {"type": "string"}},
    "required": ["word"],
    "additionalProperties": False,
}


def test_a_response_schema_shapes_the_answer_to_a_prompt_that_asks_for_a_bare_word(
    fresh_database: DatabaseHandle,
) -> None:
    """Structured outputs against Azure (S051). The first test's prompt, which
    asks for one bare word, with a response schema: the answer is the schema's
    object and nothing around it, so the provider honoured the schema and not
    the prompt. The estimate, which counts the schema, was not below Azure's
    own input count."""
    exporter = InMemorySpanExporter()
    client = TestClient(
        create_app(
            live_settings(fresh_database),
            tracer_provider=make_tracer_provider("gw", exporter),
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
        "max_output_tokens": 32,
        "response_schema": WORD_SCHEMA,
    }

    response = client.post("/v1/chat", json=body, headers=headers)

    assert response.status_code == 200, response.text
    reply = response.json()
    usage = reply["usage"]
    answer = json.loads(reply["output"]["text"])  # no fence, nothing around it
    print(f"\ndeployment:    {reply['deployment']}")
    print(f"finish reason:  {reply['output']['finish_reason']}")
    print(f"answer keys:    {sorted(answer)}")
    print(
        f"tokens:         input {usage['input_tokens']}, "
        f"output {usage['output_tokens']}"
    )
    assert reply["output"]["finish_reason"] == "stop"
    assert set(answer) == {"word"}
    assert isinstance(answer["word"], str)
    (span,) = [s for s in exporter.get_finished_spans() if s.name == "gateway.chat"]
    assert span.attributes["meridian.response_schema"] is True
    (event,) = audit_events(fresh_database, run_id)
    assert (event["event"], event["outcome"]) == ("model.call", "completed")
    ((reserved, input_tokens),) = owner_rows(
        fresh_database,
        "SELECT reserved_tokens, input_tokens FROM gateway.usage WHERE call_id = %s",
        (event["call_id"],),
    )
    print(f"reservation:    {reserved} tokens reserved, input {input_tokens}")
    assert input_tokens == usage["input_tokens"]
    assert reserved - body["max_output_tokens"] >= input_tokens


# A fictional description and two clauses of the synthetic motor wording
# (MOTOR-TPL 3.1 and 3.2): the second excludes what the description states.
TRACK_DAY_CLAIM = PROMPT_PROBE_CLAIM.model_copy(
    update={
        "peril": "third_party_liability",
        "description": (
            "I took part in an amateur track day at the circuit. On the second "
            "lap I lost control in a corner and hit another participant's car, "
            "which was damaged at the rear."
        ),
    }
)
TRACK_DAY_CLAUSES = (
    Clause(
        "3.1",
        "Own vehicle damage",
        "This product insures only your legal liability to other people. It "
        "does not pay for damage to, or the loss of, the insured vehicle itself, "
        "whatever the cause.",
    ),
    Clause(
        "3.2",
        "Racing",
        "We do not cover loss or liability that arises while the insured "
        "vehicle takes part in a race, rally, speed test, timed session or "
        "track day, or while it is prepared for one, on or off a public road. "
        "This applies even if the event is meant for amateurs or for driver "
        "training.",
    ),
)
# The words ``read_answer`` gives an answer that came in the schema's shape:
# no word at all, or the model's own ``unsure``.
READ_AS_AN_ANSWER = (None, "unsure")


def test_the_triage_question_is_asked_by_schema_and_its_answer_is_read_strictly(
    fresh_database: DatabaseHandle,
) -> None:
    """The triage's own call against Azure (S051): ``assess`` sends its
    messages, its data class and its answer schema through the runtime's
    client, and reads the answer with ``read_answer``. An answer that was cut
    off, was not JSON, had another shape or named a clause that was not sent
    would be unavailable with that word; none of those is accepted here."""
    exporter = InMemorySpanExporter()
    client = TestClient(
        create_app(
            live_settings(fresh_database),
            tracer_provider=make_tracer_provider("gw", exporter),
        )
    )
    run_id = uuid.uuid4()
    model = ModelClient(
        client, tenant="development", agent="claims-triage", run_id=run_id, max_calls=1
    )

    assessed = assess(model, TRACK_DAY_CLAIM, "MOTOR-TPL", "2026-01", TRACK_DAY_CLAUSES)

    assert assessed.drafted_by is not None, assessed.unavailable_because
    print(f"\ndeployment:     {assessed.drafted_by.deployment}")
    print(f"assessment:     {assessed.assessment.status}")
    print(f"clause:         {assessed.assessment.clause}")
    print(f"unavailable:    {assessed.unavailable_because}")
    assert assessed.drafted_by.mode == "live"
    assert assessed.unavailable_because in READ_AS_AN_ANSWER
    (span,) = [s for s in exporter.get_finished_spans() if s.name == "gateway.chat"]
    assert span.attributes["meridian.response_schema"] is True
    (event,) = audit_events(fresh_database, run_id)
    assert (event["event"], event["outcome"]) == ("model.call", "completed")
    # The call names its own class, higher than the tenant's (T-11).
    assert event["data_class"] == "personal"
    print(
        f"tokens:         input {event['input_tokens']}, "
        f"output {event['output_tokens']}"
    )
    assert TRACK_DAY_CLAIM.description not in str(event)
