"""Each app hands its settings' ``client_tls`` to the client it builds (S055).

``test_client_tls.py`` proves a client given a context presents it; this proves
``create_app`` gives it one when the settings carry the TLS files, and the
default verification (never off) when they do not. The factories are replaced
by name with a capture, as ``test_knowledge_settings.py`` does.
"""

import ssl
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest
from dbsupport import DatabaseHandle
from servicesupport import REGISTRY_DIR
from starlette.testclient import TestClient
from tlsserver import Pki
from tlssupport import PREFIX, make_ca, spiffe, uri_san

from meridian.platform.common.telemetry import make_tracer_provider
from meridian.platform.knowledge_mcp import app as knowledge_module
from meridian.platform.knowledge_mcp.settings import KnowledgeServerSettings
from meridian.runtime import app as runtime_module
from meridian.runtime.settings import RuntimeSettings
from meridian.workloads.claims_triage import app as claims_module
from meridian.workloads.claims_triage.settings import ClaimsSettings

UNUSED_DSN = "postgresql://role:pw@db.invalid/meridian"
GATEWAY_URL = "https://model-gateway.meridian.svc:8443"
RUN = {
    "agent": "claims-triage",
    "tenant": "claims-triage",
    "reference": "CLM-0001",
    "input": {"claim": {"n": 1}},
}


@pytest.fixture(scope="module")
def pki(tmp_path_factory: pytest.TempPathFactory) -> Pki:
    directory: Path = tmp_path_factory.mktemp("wiring-tls")
    ca = make_ca(directory, "ca")
    service = ca.issue("agent-runtime", "agent-runtime", [uri_san(spiffe("x"))])
    return Pki(
        ca=ca,
        other_ca=make_ca(directory, "other-ca"),
        server=service,
        services={"agent-runtime": service},
    )


class Capture:
    """What a factory was called with, in order: its positional arguments and
    its keyword arguments."""

    def __init__(self, real: Any) -> None:
        self._real = real
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def __call__(self, *args: Any, **kwargs: Any) -> httpx.Client:
        self.calls.append((args, kwargs))
        return self._real(*args, **kwargs)


def runtime_settings(dsn: str, pki: Pki | None) -> RuntimeSettings:
    return RuntimeSettings(
        registry_dir=REGISTRY_DIR,
        gateway_url=GATEWAY_URL,
        database_url=dsn,
        client_tls=None if pki is None else pki.tls_of("agent-runtime"),
    )


def test_the_runtime_gives_its_gateway_client_the_context_of_its_settings(
    monkeypatch: pytest.MonkeyPatch, pki: Pki
) -> None:
    capture = Capture(runtime_module.make_gateway_client)
    monkeypatch.setattr(runtime_module, "make_gateway_client", capture)

    runtime_module.create_app(
        runtime_settings(UNUSED_DSN, pki),
        tracer_provider=make_tracer_provider("agent-runtime"),
    )

    ((args, kwargs),) = capture.calls
    assert not kwargs and isinstance(args[1], ssl.SSLContext)


def test_the_runtime_without_tls_files_gives_it_the_default_verification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture = Capture(runtime_module.make_gateway_client)
    monkeypatch.setattr(runtime_module, "make_gateway_client", capture)

    runtime_module.create_app(
        runtime_settings(UNUSED_DSN, None),
        tracer_provider=make_tracer_provider("agent-runtime"),
    )

    ((args, _),) = capture.calls
    assert args[1] is True


@pytest.mark.parametrize("with_tls", [True, False])
def test_the_runtime_gives_the_tool_client_of_a_run_the_same_context(
    monkeypatch: pytest.MonkeyPatch,
    pki: Pki,
    fresh_database: DatabaseHandle,
    with_tls: bool,
) -> None:
    gateway = Capture(runtime_module.make_gateway_client)
    tools: list[Any] = []

    def refuse_to_go_on(*args: Any, **kwargs: Any) -> Any:
        tools.append(kwargs)
        raise RuntimeError("the capture ends the run")

    monkeypatch.setattr(runtime_module, "make_gateway_client", gateway)
    monkeypatch.setattr(runtime_module, "tool_client_for", refuse_to_go_on)
    app = runtime_module.create_app(
        runtime_settings(
            fresh_database.dsn("agent_runtime"), pki if with_tls else None
        ),
        tracer_provider=make_tracer_provider("agent-runtime"),
    )

    with TestClient(app, raise_server_exceptions=False) as client:
        client.post("/runs", json={**RUN, "reference": str(uuid.uuid4())[:8]})

    ((args, _),) = gateway.calls
    (tool_kwargs,) = tools
    assert tool_kwargs["verify"] is args[1]
    assert isinstance(args[1], ssl.SSLContext) is with_tls


def test_the_claims_api_gives_its_runtime_client_the_context_of_its_settings(
    monkeypatch: pytest.MonkeyPatch, pki: Pki
) -> None:
    capture = Capture(claims_module.make_runtime_client)
    monkeypatch.setattr(claims_module, "make_runtime_client", capture)
    with_tls = ClaimsSettings(
        runtime_url=GATEWAY_URL,
        database_url=UNUSED_DSN,
        client_tls=pki.tls_of("agent-runtime"),
    )
    without = ClaimsSettings(runtime_url=GATEWAY_URL, database_url=UNUSED_DSN)

    claims_module.create_app(with_tls)
    claims_module.create_app(without)

    ((first, _), (second, _)) = capture.calls
    assert isinstance(first[1], ssl.SSLContext)
    assert second[1] is True


def test_the_knowledge_server_gives_its_embedding_client_the_context_of_its_settings(
    monkeypatch: pytest.MonkeyPatch, pki: Pki
) -> None:
    capture = Capture(knowledge_module.make_http_client)
    monkeypatch.setattr(knowledge_module, "make_http_client", capture)
    common: dict[str, Any] = {
        "registry_dir": REGISTRY_DIR,
        "database_url": UNUSED_DSN,
        "allowed_hosts": ("knowledge-mcp:8443",),
        "gateway_url": GATEWAY_URL,
        "identity_prefix": PREFIX,
    }

    knowledge_module.create_app(
        KnowledgeServerSettings(**common, client_tls=pki.tls_of("agent-runtime"))
    )
    knowledge_module.create_app(KnowledgeServerSettings(**common))

    ((_, first), (_, second)) = capture.calls
    assert isinstance(first["verify"], ssl.SSLContext)
    assert "verify" not in second  # the call is the one made before S055
