"""Gateway settings, built explicitly from environment variables."""

import json
import os
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal, Self
from urllib.parse import unquote, urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ModelWrapValidatorHandler,
    SecretStr,
    field_validator,
    model_validator,
)

from meridian.platform.common.db import DATABASE_URL_ENV
from meridian.platform.common.env import SettingsError, registry_dir_from, require_env
from meridian.platform.common.identity import IdentityPrefix, identity_prefix_from
from meridian.platform.common.tls import (
    CA_FILE_ENV,
    CERT_FILE_ENV,
    KEY_FILE_ENV,
    ClientTls,
)

MODE_ENV = "MERIDIAN_GATEWAY_MODE"
ENVIRONMENT_ENV = "MERIDIAN_ENVIRONMENT"
ENDPOINTS_ENV = "MERIDIAN_AZURE_OPENAI_ENDPOINTS"
CREDENTIAL_ENV = "MERIDIAN_AZURE_CREDENTIAL"
TENANT_ID_ENV = "MERIDIAN_AZURE_TENANT_ID"
RECORDINGS_ENV = "MERIDIAN_GATEWAY_RECORDINGS"
# The shared store of the rate windows (S066): a rediss:// URL with the gateway's
# user and password, on a cluster one key of a Secret. Unset keeps the windows in
# the process.
RATE_STORE_URL_ENV = "MERIDIAN_GATEWAY_RATE_STORE_URL"
REDIS_PORT = 6379

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


@dataclass(frozen=True, slots=True)
class RateStoreAddress:
    """What the store's URL says, the user name and password decoded and out of
    the repr."""

    host: str
    port: int
    username: str = field(repr=False)
    password: str = field(repr=False)


def _decoded(part: str) -> str:
    """A user name or password with its percent sequences decoded as UTF-8; raise
    ``ValueError`` without the text for a sequence that is not (the default would
    put U+FFFD in its place, a wrong password that nothing explains)."""
    try:
        return unquote(part, errors="strict")
    except UnicodeDecodeError:
        raise ValueError("has a user name or password not valid as UTF-8") from None


def parse_rate_store_url(url: str) -> RateStoreAddress:
    """The address's parts; raise ``ValueError`` with a text that never holds the
    address (it carries the store's password) unless it is one the gateway may
    use: a ``rediss`` address (TLS, no plain path and no switch) with a host, a
    user and a password, database 0, and nothing after the address. A query is
    refused because redis-py reads one over the arguments the gateway sets, so
    ``?ssl_cert_reqs=none`` would turn off the check of the server's certificate.
    A host with a percent sign is refused: nothing decodes a host, so it names
    nothing, and the first call would fail instead of the start.
    """
    # urlsplit drops a tab and a line feed without a word, so the text is checked
    # first.
    if any(ch.isspace() or not ch.isprintable() for ch in url):
        raise ValueError("is not a usable URL")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise ValueError("is not a usable URL") from None
    if port == 0 or parts.netloc.endswith(":"):
        raise ValueError("is not a usable URL")
    if parts.scheme != "rediss":
        raise ValueError("must be a rediss:// URL: the store is reached over TLS only")
    if not parts.hostname:
        raise ValueError("must name the store's host")
    if "%" in parts.hostname:
        raise ValueError("must name the store's host without a percent sign")
    if not parts.username or not parts.password:
        raise ValueError("must carry the gateway's user name and password")
    if parts.query or parts.fragment or "?" in url or "#" in url:
        raise ValueError("must not carry a query or a fragment")
    if parts.path not in ("", "/", "/0"):
        raise ValueError("must use database 0")
    return RateStoreAddress(
        host=parts.hostname,
        port=port or REDIS_PORT,
        username=_decoded(parts.username),
        password=_decoded(parts.password),
    )


def _check_rate_store_tls(url: str | SecretStr | None, tls: ClientTls | None) -> None:
    """The store asks for a client certificate, so the gateway's own TLS files
    must be set when it is given one; raise ``ValueError`` naming the variables."""
    if url is not None and tls is None:
        raise ValueError(
            f"{CERT_FILE_ENV}, {KEY_FILE_ENV} and {CA_FILE_ENV} must be set "
            f"when {RATE_STORE_URL_ENV} is: the store asks for a client certificate"
        )


def _rate_store_url_from(
    environ: Mapping[str, str], tls: ClientTls | None
) -> str | None:
    """The address, or ``None`` when the variable is unset; raise
    ``SettingsError`` naming the variable, never showing the address. A variable
    that is set and empty is an error, not "no store": with a second replica
    allowed once the store is on, an empty Secret key must not quietly bring the
    process's own windows back."""
    raw = environ.get(RATE_STORE_URL_ENV)
    if raw is None:
        return None
    if not raw:
        raise SettingsError(
            f"{RATE_STORE_URL_ENV} is set and empty: unset it to keep the rate "
            "windows in the process"
        )
    try:
        parse_rate_store_url(raw)
        _check_rate_store_tls(raw, tls)
    except ValueError as error:
        raise SettingsError(f"{RATE_STORE_URL_ENV} {error}") from None
    return raw


class GatewaySettings(BaseModel):
    """What the gateway starts from.

    ``from_env`` reads the three TLS variables (``MERIDIAN_TLS_CERT_FILE``,
    ``MERIDIAN_TLS_KEY_FILE``, ``MERIDIAN_TLS_CA_FILE``) at every start, with or
    without a rate store, so a partial set of them stops the start (the chart sets
    all three together, ``_helpers.tpl``); none of them is allowed only while no
    store is set. With ``MERIDIAN_GATEWAY_RATE_STORE_URL`` set the three are
    required, and that variable set and empty is an error.

    The store's address carries its password, so it is held as a ``SecretStr``: it
    is in no ``repr``, ``model_dump`` or JSON dump, and (through the wrap
    validator, which must stay the last model validator so it runs first) in no
    ``ValidationError``'s ``errors()`` or ``json()`` either, whether a plain
    string or a ``SecretStr`` was passed in.
    """

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
    # The shared store of the rate windows (S066); none keeps them in the
    # process. The address carries the store's password, so it stays out of the
    # repr and every error. The three TLS files the gateway presents to the store
    # (and the services' CA it verifies the store with) are required with it.
    rate_store_url: SecretStr | None = Field(default=None, repr=False)
    client_tls: ClientTls | None = None

    @field_validator("rate_store_url")
    @classmethod
    def _rate_store_is_an_address_the_gateway_may_use(
        cls, url: SecretStr | None
    ) -> SecretStr | None:
        if url is not None:
            parse_rate_store_url(url.get_secret_value())
        return url

    @model_validator(mode="after")
    def _a_store_needs_the_gateways_tls_files(self) -> Self:
        _check_rate_store_tls(self.rate_store_url, self.client_tls)
        return self

    # Declared after the other model validator on purpose: pydantic runs the last
    # one declared first, so an error of any validator above reports the input
    # with the address already held as a SecretStr, never the plain string a
    # caller passed (an error's ``input`` is what the validator was given).
    @model_validator(mode="wrap")
    @classmethod
    def _hold_the_address_as_a_secret(
        cls, data: Any, handler: ModelWrapValidatorHandler[Self]
    ) -> Self:
        if isinstance(data, dict) and isinstance(data.get("rate_store_url"), str):
            data = {**data, "rate_store_url": SecretStr(data["rate_store_url"])}
        return handler(data)

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
        client_tls = ClientTls.from_env(environ)
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
            rate_store_url=_rate_store_url_from(environ, client_tls),
            client_tls=client_tls,
        )
