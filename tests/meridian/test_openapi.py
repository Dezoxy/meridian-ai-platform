"""The three services describe themselves: tags, summaries, error responses."""

from typing import Any

import pytest
from fastapi import FastAPI
from servicesupport import REGISTRY_DIR

from meridian.platform.gateway.app import create_app as create_gateway
from meridian.platform.gateway.settings import GatewaySettings
from meridian.runtime.app import create_app as create_runtime
from meridian.runtime.settings import RuntimeSettings
from meridian.workloads.claims_triage.app import create_app as create_claims
from meridian.workloads.claims_triage.settings import ClaimsSettings

DSN = "postgresql://role@db.invalid/meridian"


def apps() -> dict[str, FastAPI]:
    return {
        "gateway": create_gateway(
            GatewaySettings(
                registry_dir=REGISTRY_DIR,
                mode="replay",
                environment="test",
                database_url=DSN,
            )
        ),
        "runtime": create_runtime(
            RuntimeSettings(
                registry_dir=REGISTRY_DIR,
                gateway_url="http://gateway.invalid",
                database_url=DSN,
            )
        ),
        "claims": create_claims(
            ClaimsSettings(runtime_url="http://runtime.invalid", database_url=DSN)
        ),
    }


SPECS = {name: app.openapi() for name, app in apps().items()}
# operation -> the error statuses it must declare (422 is FastAPI's own)
ERRORS = {
    ("gateway", "post", "/v1/chat"): {
        "403",
        "413",
        "422",
        "429",
        "500",
        "502",
        "503",
        "504",
    },
    ("gateway", "post", "/v1/embeddings"): {
        "403",
        "413",
        "422",
        "429",
        "500",
        "502",
        "503",
        "504",
    },
    ("runtime", "post", "/runs"): {"403", "413", "422", "500", "502", "503", "504"},
    ("runtime", "post", "/runs/{run_id}/resume"): {
        "403",
        "404",
        "413",
        "422",
        "500",
        "502",
        "503",
        "504",
    },
    ("runtime", "get", "/runs/{run_id}"): {"404", "422", "500", "503"},
    ("claims", "post", "/claims"): {"409", "413", "422", "500", "502", "503", "504"},
    ("claims", "post", "/claims/{claim_id}/decision"): {
        "404",
        "409",
        "413",
        "422",
        "500",
        "502",
        "503",
        "504",
    },
}


def operations() -> list[tuple[str, str, str, dict[str, Any]]]:
    return [
        (service, method, path, operation)
        for service, spec in SPECS.items()
        for path, methods in spec["paths"].items()
        for method, operation in methods.items()
    ]


@pytest.mark.parametrize(
    ("service", "method", "path", "operation"),
    operations(),
    ids=lambda v: v if isinstance(v, str) else "",
)
def test_every_operation_has_a_tag_and_a_one_line_description(
    service: str, method: str, path: str, operation: dict[str, Any]
) -> None:
    assert operation["tags"]
    assert operation["summary"].strip()
    assert "\n" not in operation["summary"]


@pytest.mark.parametrize(("service", "method", "path"), list(ERRORS))
def test_an_endpoint_lists_the_error_responses_it_can_answer(
    service: str, method: str, path: str
) -> None:
    declared = set(SPECS[service]["paths"][path][method]["responses"])

    assert ERRORS[(service, method, path)] <= declared


def test_reading_a_run_takes_its_tenant_and_reference_as_query_parameters() -> None:
    operation = SPECS["runtime"]["paths"]["/runs/{run_id}"]["get"]

    assert {(p["in"], p["name"], p["required"]) for p in operation["parameters"]} == {
        ("path", "run_id", True),
        ("query", "tenant", True),
        ("query", "reference", True),
    }


def test_every_service_declares_healthz() -> None:
    for spec in SPECS.values():
        assert "get" in spec["paths"]["/healthz"]


def schema_ref(spec: dict[str, Any], path: str, method: str, status: str) -> str:
    response = spec["paths"][path][method]["responses"][status]
    return response["content"]["application/json"]["schema"]["$ref"]


def gateway_schema(name: str) -> dict[str, Any]:
    return SPECS["gateway"]["components"]["schemas"][name]


def test_both_gateway_routes_take_the_same_three_caller_headers() -> None:
    paths = SPECS["gateway"]["paths"]

    declared = {
        path: {
            (p["in"], p["name"], p["required"])
            for p in paths[path]["post"]["parameters"]
        }
        for path in ("/v1/chat", "/v1/embeddings")
    }

    assert declared["/v1/embeddings"] == declared["/v1/chat"]
    assert declared["/v1/chat"] == {
        ("header", "X-Meridian-Tenant", True),
        ("header", "X-Meridian-Agent", True),
        ("header", "X-Meridian-Run", True),
        # Optional: it can only raise the tenant's class (S047, T-13).
        ("header", "X-Meridian-Data-Class", False),
    }


def test_the_embeddings_route_describes_503_and_413_as_chat_does() -> None:
    paths = SPECS["gateway"]["paths"]

    for status in ("503", "413"):
        assert (
            paths["/v1/embeddings"]["post"]["responses"][status]["description"]
            == paths["/v1/chat"]["post"]["responses"][status]["description"]
        )
    assert (
        "audit log"
        in paths["/v1/embeddings"]["post"]["responses"]["503"]["description"]
    )


def test_the_embedding_request_is_the_inputs_alone_and_bounded() -> None:
    schema = gateway_schema("EmbeddingRequest")

    # The caller never chooses the model or the dimensions (T-54).
    assert set(schema["properties"]) == {"inputs"}
    assert schema["required"] == ["inputs"]
    assert schema["additionalProperties"] is False
    inputs = schema["properties"]["inputs"]
    assert (inputs["minItems"], inputs["maxItems"]) == (1, 16)
    assert (inputs["items"]["minLength"], inputs["items"]["maxLength"]) == (1, 8000)


def test_the_embedding_response_names_what_made_the_vectors_and_counts_input_only() -> (
    None
):
    response = gateway_schema("EmbeddingResponse")
    usage = gateway_schema("EmbeddingUsage")

    assert set(response["required"]) == {
        "call_id",
        "mode",
        "deployment",
        "provider",
        "model",
        "dimensions",
        "embeddings",
        "usage",
    }
    assert response["additionalProperties"] is False
    assert set(usage["properties"]) == {"input_tokens"}
    assert response["properties"]["call_id"]["format"] == "uuid"


def test_the_embeddings_route_answers_its_success_with_the_embedding_response() -> None:
    assert schema_ref(SPECS["gateway"], "/v1/embeddings", "post", "200").endswith(
        "/EmbeddingResponse"
    )


def test_post_runs_declares_run_response_as_its_success_and_error_model() -> None:
    spec = SPECS["runtime"]

    assert schema_ref(spec, "/runs", "post", "200").endswith("/RunResponse")
    # A failed run answers with the same body, status "Failed".
    assert schema_ref(spec, "/runs", "post", "502").endswith("/RunResponse")
    assert schema_ref(spec, "/runs", "post", "504").endswith("/RunResponse")
    assert schema_ref(spec, "/runs", "post", "403").endswith("/ErrorBody")
    assert schema_ref(spec, "/runs", "post", "503").endswith("/RunErrorBody")


def test_the_claims_api_error_answers_carry_the_claim_id() -> None:
    spec = SPECS["claims"]

    assert schema_ref(spec, "/claims", "post", "502").endswith("/ClaimErrorBody")
    error = spec["components"]["schemas"]["ClaimErrorBody"]
    assert {"detail", "claim_id"} <= set(error["required"])


def test_the_decision_endpoint_declares_its_success_and_error_models() -> None:
    spec = SPECS["claims"]
    path = "/claims/{claim_id}/decision"

    assert schema_ref(spec, path, "post", "200").endswith("/DecisionResponse")
    assert schema_ref(spec, path, "post", "502").endswith("/ClaimErrorBody")
    assert schema_ref(spec, path, "post", "504").endswith("/ClaimErrorBody")
    assert schema_ref(spec, path, "post", "404").endswith("/ErrorBody")
    schemas = spec["components"]["schemas"]
    assert schemas["ClaimDecision"]["additionalProperties"] is False
    assert schemas["ClaimDecision"]["properties"]["decision"]["enum"] == [
        "approve",
        "reject",
        "request_documents",
    ]


def test_the_claims_answers_name_the_claims_state() -> None:
    schemas = SPECS["claims"]["components"]["schemas"]

    assert "state" in schemas["ClaimResponse"]["required"]
    # The run is absent when a claim was referred with no paused run (S048).
    assert set(schemas["DecisionResponse"]["required"]) == {"claim_id", "state"}


def test_the_claim_response_run_id_is_a_uuid() -> None:
    schema = SPECS["claims"]["components"]["schemas"]["ClaimResponse"]

    assert schema["properties"]["run_id"]["format"] == "uuid"
