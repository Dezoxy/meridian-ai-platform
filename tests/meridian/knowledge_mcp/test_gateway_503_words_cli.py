"""``meridian knowledge ingest`` ends on a 503 of the gateway's (S073, K6): the
command's last line names which one, and nothing of the reply's body is in its
output or in the audit log. The scripted replies are in
``test_gateway_503_words.py``; these need the database the command connects to."""

import json

import httpx
import pytest
from dbsupport import INGEST_ROLE, DatabaseHandle
from knowledgesupport import REAL_SOURCE, TENANT, ScriptedGateway, chunk_count
from servicesupport import REGISTRY_DIR, owner_rows
from typer.testing import CliRunner

from meridian.platform.cli import app
from meridian.platform.cli import knowledge as knowledge_cli
from meridian.platform.cli.db import INGEST_DATABASE_URL_ENV
from meridian.platform.common.env import REGISTRY_DIR_ENV

CANARY = "CANARY-gateway-body-9052"
runner = CliRunner()


@pytest.fixture
def ingesting(
    monkeypatch: pytest.MonkeyPatch, fresh_database: DatabaseHandle
) -> DatabaseHandle:
    monkeypatch.setenv(INGEST_DATABASE_URL_ENV, fresh_database.dsn(INGEST_ROLE))
    monkeypatch.setenv(knowledge_cli.GATEWAY_URL_ENV, "http://gateway.invalid:8080")
    monkeypatch.setenv(REGISTRY_DIR_ENV, str(REGISTRY_DIR))
    return fresh_database


def ingest_against(
    monkeypatch: pytest.MonkeyPatch, response: httpx.Response
) -> tuple[ScriptedGateway, str, int]:
    """Run the command against a gateway that answers ``response`` and return
    the gateway, the command's output and its exit code."""
    gateway = ScriptedGateway(script=lambda index, inputs: response)
    monkeypatch.setattr(knowledge_cli, "make_http_client", lambda url: gateway.http())
    result = runner.invoke(
        app,
        ["knowledge", "ingest", "--tenant", TENANT, "--from", str(REAL_SOURCE)],
    )
    return gateway, result.output, result.exit_code


@pytest.mark.parametrize(
    ("detail", "word"),
    [
        pytest.param(
            "the rate store is unavailable", "rate-store-unavailable", id="rate-store"
        ),
        pytest.param("the database is unavailable", "database-unavailable", id="db"),
        pytest.param("the audit log is unavailable", "audit-unavailable", id="audit"),
        pytest.param(
            "the model provider is unavailable", "provider-unavailable", id="provider"
        ),
        pytest.param(f"down {CANARY}", "unknown", id="a-text-it-does-not-know"),
    ],
)
def test_the_commands_last_line_names_the_word_of_the_503_it_ended_on(
    ingesting: DatabaseHandle,
    monkeypatch: pytest.MonkeyPatch,
    detail: str,
    word: str,
) -> None:
    response = httpx.Response(503, content=json.dumps({"detail": detail}).encode())

    gateway, output, exit_code = ingest_against(monkeypatch, response)

    assert exit_code == 1
    assert output.splitlines() == [
        "ERROR the model gateway refused the embedding call "
        f"(model gateway answered 503; kind {word})"
    ]
    assert len(gateway.requests) == 1
    assert chunk_count(ingesting) == 0


def test_a_canary_in_a_503_body_is_in_neither_the_output_nor_the_audit_log(
    ingesting: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = json.dumps({"detail": f"down {CANARY}", "echo": CANARY}).encode()

    _, output, _ = ingest_against(monkeypatch, httpx.Response(503, content=body))

    assert CANARY not in output
    assert CANARY not in str(owner_rows(ingesting, "SELECT * FROM audit.events"))


def test_the_audit_row_of_a_503_keeps_the_reason_it_had(
    ingesting: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = json.dumps({"detail": "the rate store is unavailable"}).encode()

    ingest_against(monkeypatch, httpx.Response(503, content=body))

    reasons = owner_rows(ingesting, "SELECT reason FROM audit.events")
    assert reasons == [("gateway-failed",)]
