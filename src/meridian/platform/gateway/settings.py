"""Gateway settings, built explicitly from environment variables."""

import json
import os
import re
import uuid
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Literal, Self
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

from meridian.platform.common.db import DATABASE_URL_ENV
from meridian.platform.common.env import SettingsError, registry_dir_from, require_env
from meridian.platform.common.identity import IdentityPrefix, identity_prefix_from

MODE_ENV = "MERIDIAN_GATEWAY_MODE"
ENVIRONMENT_ENV = "MERIDIAN_ENVIRONMENT"
ENDPOINTS_ENV = "MERIDIAN_AZURE_OPENAI_ENDPOINTS"
CREDENTIAL_ENV = "MERIDIAN_AZURE_CREDENTIAL"
TENANT_ID_ENV = "MERIDIAN_AZURE_TENANT_ID"
RECORDINGS_ENV = "MERIDIAN_GATEWAY_RECORDINGS"

GatewayMode = Literal["replay", "recorded", "live"]
# "local" is a developer's laptop outside any cluster.
Environment = Literal["local", "test", "ci", "kind", "azure"]
AzureCredential = Literal["azure-cli"]

HTTPS_PORT = 443
# The name infra/terraform/foundation/openai.tf gives an account is
# ``oai-meridian-<location key>-<suffix>``; a test fails when it drifts.
ACCOUNT_NAME_PREFIX = "oai-meridian-"
ACCOUNT_SUFFIX = "[a-z0-9]+"
AZURE_OPENAI_DOMAIN = ".openai.azure.com"
LOCATION_KEY = re.compile(r"^[a-z0-9-]+$")
FORBIDDEN_ENDPOINT_CHARS = re.compile(r"[\s\\?#]")


def _check_endpoint(location: str, endpoint: str) -> str:
    """The canonical ``https://<hostname>`` of ``location``'s account; raise
    ``ValueError`` without the value unless the endpoint is exactly that
    project's account address and nothing more (T-43)."""
    # urlsplit drops leading control characters and tabs and newlines, so the
    # raw text is checked first.
    if not endpoint.isprintable() or FORBIDDEN_ENDPOINT_CHARS.search(endpoint):
        raise ValueError("has a character that may not appear in an address")
    try:
        parts = urlsplit(endpoint)
        port = parts.port
    except ValueError:
        raise ValueError("is not a valid URL") from None
    host = parts.hostname or ""
    account_host = (
        re.escape(f"{ACCOUNT_NAME_PREFIX}{location}-")
        + ACCOUNT_SUFFIX
        + re.escape(AZURE_OPENAI_DOMAIN)
    )
    if (
        parts.scheme != "https"
        or "@" in parts.netloc
        or re.fullmatch(account_host, host) is None
        or parts.path not in ("", "/")
        or port not in (None, HTTPS_PORT)
    ):
        raise ValueError(
            "must be an https address of this project's account for its location "
            f"key: https://{ACCOUNT_NAME_PREFIX}<location key>-<suffix>"
            f"{AZURE_OPENAI_DOMAIN}"
        )
    return f"https://{host}"


def _check_endpoints(endpoints: Mapping[str, str]) -> Mapping[str, str]:
    """The endpoints in canonical form, read-only; raise ``ValueError``."""
    canonical: dict[str, str] = {}
    for key, endpoint in endpoints.items():
        if LOCATION_KEY.fullmatch(key) is None:
            raise ValueError("has a location key outside lowercase letters, digits, -")
        canonical[key] = _check_endpoint(key, endpoint)
    return MappingProxyType(canonical)


def _check_tenant_id(tenant_id: str) -> str:
    """The canonical form of a tenant UUID; raise ``ValueError`` without the
    value."""
    try:
        return str(uuid.UUID(tenant_id))
    except ValueError:
        raise ValueError("is not a UUID") from None


def _endpoints_from(environ: Mapping[str, str]) -> Mapping[str, str]:
    """Parse the JSON object; raise ``SettingsError`` naming the variable, never
    showing its text (it holds the account addresses)."""
    raw = environ.get(ENDPOINTS_ENV)
    if not raw:
        return {}
    try:
        endpoints = json.loads(raw)
    except ValueError:
        raise SettingsError(f"{ENDPOINTS_ENV} is not valid JSON") from None
    if not isinstance(endpoints, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in endpoints.items()
    ):
        raise SettingsError(f"{ENDPOINTS_ENV} must be a JSON object of strings")
    try:
        return _check_endpoints(endpoints)
    except ValueError as error:
        raise SettingsError(f"{ENDPOINTS_ENV} {error}") from None


def _tenant_id_from(environ: Mapping[str, str]) -> str | None:
    raw = environ.get(TENANT_ID_ENV)
    if not raw:
        return None
    try:
        return _check_tenant_id(raw)
    except ValueError as error:
        raise SettingsError(f"{TENANT_ID_ENV} {error}") from None


class GatewaySettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    registry_dir: Path
    mode: GatewayMode
    environment: Environment
    database_url: str = Field(repr=False)
    # Live mode only (S010); the addresses, the credential source and the Entra
    # tenant stay out of the repr.
    azure_openai_endpoints: Mapping[str, str] = Field(default_factory=dict, repr=False)
    azure_credential: AzureCredential | None = Field(default=None, repr=False)
    azure_tenant_id: str | None = Field(default=None, repr=False)
    # Recorded mode only (S050): the file whose answers the gateway replays.
    recordings: Path | None = None
    # The prefix of the callers' certificate URIs (S055); none only for an app
    # built in code, which then has no caller check. ``from_env`` requires it.
    identity_prefix: IdentityPrefix | None = None

    @field_validator("azure_openai_endpoints")
    @classmethod
    def _endpoints_are_this_projects_accounts(
        cls, endpoints: Mapping[str, str]
    ) -> Mapping[str, str]:
        return _check_endpoints(endpoints)

    @field_validator("azure_tenant_id")
    @classmethod
    def _tenant_is_a_uuid(cls, tenant_id: str | None) -> str | None:
        return None if tenant_id is None else _check_tenant_id(tenant_id)

    @classmethod
    def from_env(cls, environ: Mapping[str, str] = os.environ) -> Self:
        """Read the variables; raise ``SettingsError`` naming a missing one."""
        return cls(
            registry_dir=registry_dir_from(environ),
            mode=require_env(environ, MODE_ENV),
            environment=require_env(environ, ENVIRONMENT_ENV),
            database_url=require_env(environ, DATABASE_URL_ENV),
            azure_openai_endpoints=_endpoints_from(environ),
            azure_credential=environ.get(CREDENTIAL_ENV) or None,
            azure_tenant_id=_tenant_id_from(environ),
            recordings=Path(raw) if (raw := environ.get(RECORDINGS_ENV)) else None,
            identity_prefix=identity_prefix_from(environ),
        )
