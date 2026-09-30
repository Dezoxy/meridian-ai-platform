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
    ("gateway", "post", "/v1/chat"): {"403", "413", "422", "500", "503"},
    ("runtime", "post", "/runs"): {"403", "413", "422", "500", "502", "503", "504"},
    ("runtime", "get", "/runs/{run_id}"): {"404", "422", "500", "503"},
    ("claims", "post", "/claims"): {"409", "413", "422", "500", "502", "503", "504"},
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


def test_every_service_declares_healthz() -> None:
    for spec in SPECS.values():
        assert "get" in spec["paths"]["/healthz"]


def schema_ref(spec: dict[str, Any], path: str, method: str, status: str) -> str:
    response = spec["paths"][path][method]["responses"][status]
    return response["content"]["application/json"]["schema"]["$ref"]


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


def test_the_claim_response_run_id_is_a_uuid() -> None:
    schema = SPECS["claims"]["components"]["schemas"]["ClaimResponse"]

    assert schema["properties"]["run_id"]["format"] == "uuid"
