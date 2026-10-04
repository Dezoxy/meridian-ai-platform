"""The three TLS variables in the settings of the callers (S055): the runtime,
the Claims API and the knowledge server read them; a partial set stops the
start naming the missing variable; none leaves ``client_tls`` empty."""

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from servicesupport import REGISTRY_DIR

from meridian.platform.common.env import SettingsError
from meridian.platform.common.tls import (
    CA_FILE_ENV,
    CERT_FILE_ENV,
    KEY_FILE_ENV,
    ClientTls,
)
from meridian.platform.knowledge_mcp.settings import KnowledgeServerSettings
from meridian.runtime.settings import RuntimeSettings
from meridian.workloads.claims_triage.settings import ClaimsSettings

DSN = "postgresql://role:pw@db.invalid/meridian"
PREFIX = "spiffe://meridian.kind/ns/meridian/sa/"
TLS = {
    CERT_FILE_ENV: "/etc/meridian/tls/tls.crt",
    KEY_FILE_ENV: "/etc/meridian/tls/tls.key",
    CA_FILE_ENV: "/etc/meridian/tls/ca.crt",
}
EXPECTED = ClientTls(
    cert_file=Path(TLS[CERT_FILE_ENV]),
    key_file=Path(TLS[KEY_FILE_ENV]),
    ca_file=Path(TLS[CA_FILE_ENV]),
)
ENVIRONMENTS: dict[str, tuple[Any, dict[str, str]]] = {
    "runtime": (
        RuntimeSettings,
        {
            "MERIDIAN_REGISTRY_DIR": str(REGISTRY_DIR),
            "MERIDIAN_GATEWAY_URL": "https://model-gateway.meridian.svc:8443",
            "MERIDIAN_DATABASE_URL": DSN,
            "MERIDIAN_IDENTITY_PREFIX": PREFIX,
        },
    ),
    "claims-api": (
        ClaimsSettings,
        {
            "MERIDIAN_RUNTIME_URL": "https://agent-runtime.meridian.svc:8443",
            "MERIDIAN_DATABASE_URL": DSN,
        },
    ),
    "knowledge-server": (
        KnowledgeServerSettings,
        {
            "MERIDIAN_REGISTRY_DIR": str(REGISTRY_DIR),
            "MERIDIAN_DATABASE_URL": DSN,
            "MERIDIAN_ALLOWED_HOSTS": "knowledge-mcp:8443",
            "MERIDIAN_GATEWAY_URL": "https://model-gateway.meridian.svc:8443",
            "MERIDIAN_IDENTITY_PREFIX": PREFIX,
        },
    ),
}


def settings_from(kind: str, extra: Mapping[str, str]) -> Any:
    cls, environ = ENVIRONMENTS[kind]
    return cls.from_env({**environ, **extra})


@pytest.mark.parametrize("kind", ENVIRONMENTS)
def test_the_three_variables_reach_the_settings(kind: str) -> None:
    assert settings_from(kind, TLS).client_tls == EXPECTED


@pytest.mark.parametrize("kind", ENVIRONMENTS)
def test_no_variable_leaves_the_settings_without_client_tls(kind: str) -> None:
    assert settings_from(kind, {}).client_tls is None


@pytest.mark.parametrize("kind", ENVIRONMENTS)
@pytest.mark.parametrize("missing", [CERT_FILE_ENV, KEY_FILE_ENV, CA_FILE_ENV])
def test_a_partial_set_stops_the_start_naming_the_missing_variable(
    kind: str, missing: str
) -> None:
    partial = {k: v for k, v in TLS.items() if k != missing}

    with pytest.raises(SettingsError, match=missing):
        settings_from(kind, partial)
