"""The three services describe themselves: tags, summaries, error responses."""

from typing import Any

import pytest
from fastapi import FastAPI
from servicesupport import REGISTRY_DIR
from starlette.applications import Starlette
from starlette.routing import Route
from toolsupport import CONTRACTS_DIR

from meridian.platform.gateway.app import create_app as create_gateway
from meridian.platform.gateway.settings import GatewaySettings
from meridian.platform.knowledge_mcp.app import create_app as create_knowledge_mcp
from meridian.platform.knowledge_mcp.settings import KnowledgeServerSettings
from meridian.platform.policy_mcp.app import create_app as create_policy_mcp
from meridian.platform.toolserver.settings import ToolServerSettings
from meridian.runtime.app import create_app as create_runtime
from meridian.runtime.settings import RuntimeSettings
from meridian.workloads.claims_triage.app import create_app as create_claims
from meridian.workloads.claims_triage.mcp_server.app import (
    create_app as create_claims_mcp,
)
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
        # The Claims API with the route that takes a file switched on (S070):
        # off by default, so the spec above does not list it.
        "claims-uploads": create_claims(
            ClaimsSettings(
                runtime_url="http://runtime.invalid",
                database_url=DSN,
                uploads_enabled=True,
            )
        ),
    }


SPECS = {name: app.openapi() for name, app in apps().items()}
# operation -> the error statuses it must declare (422 is FastAPI's own)
ERRORS = {
    ("gateway", "post", "/v1/chat"): {
        "401",
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
        "401",
        "403",
        "413",
        "422",
        "429",
        "500",
        "502",
        "503",
        "504",
    },
    ("runtime", "post", "/runs"): {
        "401",
        "403",
        "413",
        "422",
        "500",
        "502",
        "503",
        "504",
    },
    ("runtime", "post", "/runs/{run_id}/resume"): {
        "401",
        "403",
        "404",
        "413",
        "422",
        "500",
        "502",
        "503",
        "504",
    },
    ("runtime", "get", "/runs/{run_id}"): {"401", "403", "404", "422", "500", "503"},
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
    ("claims", "post", "/claims/{claim_id}/triage"): {
        "404",
        "409",
        "413",
        "422",
        "500",
        "502",
        "503",
        "504",
    },
    ("claims", "post", "/claims/{claim_id}/withdrawal"): {
        "404",
        "409",
        "413",
        "422",
        "500",
        "503",
    },
    ("claims", "post", "/claims/{claim_id}/documents"): {
        "404",
        "409",
        "413",
        "422",
        "500",
        "502",
        "503",
        "504",
    },
    ("claims", "post", "/claims/{claim_id}/brief"): {
        "404",
        "409",
        "413",
        "422",
        "500",
        "502",
        "503",
        "504",
    },
    ("claims", "post", "/claims/{claim_id}/brief/decision"): {
        "404",
        "409",
        "413",
        "422",
        "500",
        "502",
        "503",
        "504",
    },
    ("claims", "get", "/claims/{claim_id}/brief"): {"404", "422", "500", "503"},
    ("claims-uploads", "post", "/claims/{claim_id}/files"): {
        "201",
        "403",
        "404",
        "409",
        "413",
        "415",
        "422",
        "429",
        "500",
        "503",
        "507",
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


def tool_server_apps() -> dict[str, Starlette]:
    """The three tool servers, built from settings with no database."""
    tools = ToolServerSettings(
        registry_dir=REGISTRY_DIR, database_url=DSN, allowed_hosts=("tool-server:8080",)
    )
    knowledge = KnowledgeServerSettings(
        registry_dir=REGISTRY_DIR,
        database_url=DSN,
        allowed_hosts=("tool-server:8080",),
        gateway_url="http://gateway.invalid",
    )
    return {
        "policy-mcp": create_policy_mcp(tools).app,
        "claims-mcp": create_claims_mcp(tools).app,
        "knowledge-mcp": create_knowledge_mcp(knowledge).app,
    }


@pytest.mark.parametrize("server", ["policy-mcp", "claims-mcp", "knowledge-mcp"])
def test_every_tool_server_declares_healthz_and_publishes_its_contract(
    server: str,
) -> None:
    app = tool_server_apps()[server]

    health = [
        route
        for route in app.routes
        if isinstance(route, Route) and route.path == "/healthz"
    ]

    assert len(health) == 1
    assert "GET" in (health[0].methods or set())
    assert (CONTRACTS_DIR / f"{server}.json").is_file()


def test_every_service_declares_the_503_of_a_certificate_near_its_end() -> None:
    """S056: the kubelet's probe restarts the container on this answer, so the
    contract of every service shows it, with a body of its own."""
    for spec in SPECS.values():
        responses = spec["paths"]["/healthz"]["get"]["responses"]

        assert {"200", "503"} <= set(responses)
        assert responses["503"]["content"]["application/json"]["schema"][
            "$ref"
        ].endswith("/CertificateExpiring")
        assert "certificate" in responses["503"]["description"].lower()
        expiring = spec["components"]["schemas"]["CertificateExpiring"]
        assert expiring["properties"]["status"]["const"] == "certificate-expiring"


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
    assert "run_id" not in schemas["DecisionResponse"]["required"]
    assert "run_status" not in schemas["DecisionResponse"]["required"]


@pytest.mark.parametrize(
    "path",
    [
        "/claims/{claim_id}/triage",
        "/claims/{claim_id}/withdrawal",
        "/claims/{claim_id}/documents",
    ],
)
def test_the_routes_that_move_a_claim_answer_the_claim_move_response(
    path: str,
) -> None:
    spec = SPECS["claims"]

    assert schema_ref(spec, path, "post", "200").endswith("/ClaimMoveResponse")
    assert schema_ref(spec, path, "post", "404").endswith("/ErrorBody")
    # A refusal against what is stored, among them the bound of twenty documents.
    assert schema_ref(spec, path, "post", "409").endswith("/ErrorBody")
    assert schema_ref(spec, path, "post", "503").endswith("/ClaimErrorBody")


@pytest.mark.parametrize(
    "path", ["/claims/{claim_id}/triage", "/claims/{claim_id}/withdrawal"]
)
def test_the_moves_that_take_no_input_declare_a_required_json_body_with_no_field(
    path: str,
) -> None:
    """T-01: a route with no body never checks the content type, so a cross-site
    form could reach it; a required JSON body is what keeps a browser from posting
    without a preflight."""
    spec = SPECS["claims"]

    body = spec["paths"][path]["post"]["requestBody"]
    assert body["required"] is True
    assert list(body["content"]) == ["application/json"]
    assert body["content"]["application/json"]["schema"]["$ref"].endswith(
        "/ClaimMoveRequest"
    )
    request = spec["components"]["schemas"]["ClaimMoveRequest"]
    assert request["additionalProperties"] is False
    assert not request.get("properties")
    assert not request.get("required")


def test_the_brief_routes_answer_the_brief_view_and_its_errors_carry_the_claim() -> (
    None
):
    spec = SPECS["claims"]
    start = "/claims/{claim_id}/brief"
    decision = "/claims/{claim_id}/brief/decision"

    assert schema_ref(spec, start, "post", "201").endswith("/BriefView")
    assert schema_ref(spec, decision, "post", "200").endswith("/BriefView")
    assert schema_ref(spec, start, "get", "200").endswith("/BriefView")
    for path, method in ((start, "post"), (decision, "post")):
        assert schema_ref(spec, path, method, "404").endswith("/ErrorBody")
        assert schema_ref(spec, path, method, "409").endswith("/ErrorBody")
        assert schema_ref(spec, path, method, "502").endswith("/ClaimErrorBody")
        assert schema_ref(spec, path, method, "504").endswith("/ClaimErrorBody")
    view = spec["components"]["schemas"]["BriefView"]
    assert set(view["properties"]) == {
        "claim_id",
        "state",
        "brief",
        "run_id",
        "created_at",
        "state_changed_at",
    }
    assert view["properties"]["state"]["enum"] == [
        "drafting",
        "awaiting_decision",
        "filed",
        "rejected",
        "failed",
    ]


def test_starting_a_brief_takes_a_required_json_body_with_no_field() -> None:
    """T-01, as the moves that take no input: a required JSON body keeps a
    browser from posting without a preflight."""
    spec = SPECS["claims"]

    body = spec["paths"]["/claims/{claim_id}/brief"]["post"]["requestBody"]

    assert body["required"] is True
    assert list(body["content"]) == ["application/json"]
    assert body["content"]["application/json"]["schema"]["$ref"].endswith(
        "/ClaimMoveRequest"
    )


def test_the_brief_decision_is_one_of_two_words_and_the_run_it_was_read_on() -> None:
    spec = SPECS["claims"]

    body = spec["paths"]["/claims/{claim_id}/brief/decision"]["post"]["requestBody"]
    request = spec["components"]["schemas"]["BriefDecision"]

    assert body["required"] is True
    assert list(body["content"]) == ["application/json"]
    assert request["additionalProperties"] is False
    assert set(request["required"]) == {"decision", "run"}
    # Not triage's third word: a brief is filed or it is not.
    assert request["properties"]["decision"]["enum"] == ["approve", "reject"]
    assert request["properties"]["run"]["format"] == "uuid"


def test_the_claim_move_response_has_a_run_and_a_proposal_only_when_a_triage_ran() -> (
    None
):
    schemas = SPECS["claims"]["components"]["schemas"]

    assert set(schemas["ClaimMoveResponse"]["required"]) == {"claim_id", "state"}
    assert schemas["DocumentsArrival"]["additionalProperties"] is False
    assert schemas["DocumentsArrival"]["required"] == ["documents"]
    assert schemas["DocumentsArrival"]["properties"]["documents"]["maxItems"] == 20


def test_the_claim_response_run_id_is_a_uuid() -> None:
    schema = SPECS["claims"]["components"]["schemas"]["ClaimResponse"]

    assert schema["properties"]["run_id"]["format"] == "uuid"


# ── the route that stores a claimant's file (S070): only when it is switched on ─
UPLOAD = "/claims/{claim_id}/files"


def test_the_upload_route_is_listed_when_the_switch_is_on_and_not_when_it_is_off() -> (
    None
):
    assert UPLOAD in SPECS["claims-uploads"]["paths"]
    assert UPLOAD not in SPECS["claims"]["paths"]
    assert set(SPECS["claims-uploads"]["paths"][UPLOAD]) == {"post"}
    # Every other path is the same in both: the switch adds this one route.
    assert set(SPECS["claims-uploads"]["paths"]) - {UPLOAD} == set(
        SPECS["claims"]["paths"]
    )


def test_the_upload_routes_html_twin_is_in_no_contract() -> None:
    # The claimant's form posts to it; it is a page, as the other pages are.
    for name in ("claims", "claims-uploads"):
        assert not [p for p in SPECS[name]["paths"] if p.startswith("/claimant")]
        assert "/claimant/claims/{claim_id}/files" not in SPECS[name]["paths"]


def test_the_upload_route_answers_201_with_a_stored_file_and_names_its_errors() -> None:
    spec = SPECS["claims-uploads"]

    assert schema_ref(spec, UPLOAD, "post", "201").endswith("/StoredFile")
    for status in ("403", "404", "409", "413", "415", "429", "507"):
        assert schema_ref(spec, UPLOAD, "post", status).endswith("/ErrorBody")
    for status in ("408", "500", "503"):
        assert schema_ref(spec, UPLOAD, "post", status).endswith("/UploadErrorBody")
    assert spec["paths"][UPLOAD]["post"]["tags"] == ["claims"]


def test_the_upload_routes_error_body_has_an_optional_claim_and_no_run() -> None:
    # The route's own answers carry the claim; the middleware's 500 and the audit
    # log's 503 carry only ``detail``, so the claim cannot be required, and an
    # upload starts no run, so there is no ``run_id`` as in ``ClaimErrorBody``.
    body = SPECS["claims-uploads"]["components"]["schemas"]["UploadErrorBody"]

    assert set(body["properties"]) == {"detail", "claim_id"}
    assert body["required"] == ["detail"]


def test_the_upload_route_says_what_its_415_and_busy_503_mean() -> None:
    responses = SPECS["claims-uploads"]["paths"][UPLOAD]["post"]["responses"]

    assert "PDF, a JPEG or a PNG" in responses["415"]["description"]
    assert "busy" in responses["503"]["description"].lower()
    assert "deadline" in responses["408"]["description"]
    # Not the shared sentence every route's 415 would get.
    assert "not of a type this route takes" not in responses["415"]["description"]


def test_the_upload_route_says_synthetic_files_only() -> None:
    post = SPECS["claims-uploads"]["paths"][UPLOAD]["post"]
    form = post["requestBody"]["content"]["multipart/form-data"]["schema"]

    assert "Synthetic files only" in post["summary"]
    assert "never a real person's document" in post["summary"]
    assert "Synthetic files only" in form["properties"]["file"]["description"]


def test_the_upload_routes_429_and_busy_503_say_how_long_to_wait() -> None:
    responses = SPECS["claims-uploads"]["paths"][UPLOAD]["post"]["responses"]

    for status in ("429", "503"):
        header = responses[status]["headers"]["Retry-After"]
        assert header["schema"] == {"type": "integer"}
    # No other answer of the route carries one.
    assert {s for s, r in responses.items() if "headers" in r} == {"429", "503"}


def test_the_upload_route_takes_one_kind_and_one_file_as_multipart_form_data() -> None:
    spec = SPECS["claims-uploads"]

    body = spec["paths"][UPLOAD]["post"]["requestBody"]
    form = body["content"]["multipart/form-data"]["schema"]

    assert body["required"] is True
    assert list(body["content"]) == ["multipart/form-data"]
    assert form["additionalProperties"] is False
    assert set(form["required"]) == {"kind", "file"}
    assert form["properties"]["kind"]["enum"] == [
        "police_report",
        "photos",
        "repair_estimate",
        "accident_statement",
        "other",
    ]
    assert form["properties"]["file"]["format"] == "binary"


def test_a_stored_file_is_its_identifier_kind_type_size_and_hash_and_nothing_else() -> (
    None
):
    stored = SPECS["claims-uploads"]["components"]["schemas"]["StoredFile"]

    assert set(stored["properties"]) == {
        "file_id",
        "kind",
        "media_type",
        "size_bytes",
        "sha256",
    }
    assert set(stored["required"]) == set(stored["properties"])
    assert stored["additionalProperties"] is False
    assert stored["properties"]["file_id"]["format"] == "uuid"
    assert stored["properties"]["media_type"]["enum"] == [
        "application/pdf",
        "image/jpeg",
        "image/png",
    ]
    # The hash is lower-case hex of 32 bytes and the size is the file's: 1 byte to
    # 1 MiB, so a generated client can check what it is given.
    assert stored["properties"]["sha256"]["pattern"] == "^[0-9a-f]{64}$"
    assert stored["properties"]["size_bytes"]["minimum"] == 1
    assert stored["properties"]["size_bytes"]["maximum"] == 1024 * 1024
