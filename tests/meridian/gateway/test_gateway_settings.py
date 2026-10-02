"""Gateway settings and the startup refusals (T-39, ADR 3)."""

import json
import os
import re
from collections.abc import Callable
from pathlib import Path
from types import MappingProxyType

import pytest
from azure.core.exceptions import ClientAuthenticationError
from azure.identity import CredentialUnavailableError
from fastapi.testclient import TestClient
from pydantic import ValidationError
from servicesupport import REGISTRY_DIR, REPO_ROOT

from meridian.platform.common.env import SettingsError
from meridian.platform.gateway import app as gateway_app
from meridian.platform.gateway.app import create_app
from meridian.platform.gateway.providers import azure_openai
from meridian.platform.gateway.settings import (
    ACCOUNT_NAME_PREFIX,
    GatewaySettings,
)
from meridian.platform.registry import Registry, RegistryError, load_registry

DSN = "postgresql://model_gateway:s3cret-value@db.invalid/meridian"


def settings(**overrides: object) -> GatewaySettings:
    values = {
        "registry_dir": REGISTRY_DIR,
        "mode": "replay",
        "environment": "test",
        "database_url": DSN,
    } | overrides
    return GatewaySettings(**values)


ENV = {
    "MERIDIAN_GATEWAY_MODE": "replay",
    "MERIDIAN_ENVIRONMENT": "ci",
    "MERIDIAN_DATABASE_URL": DSN,
}


def test_settings_are_built_from_the_environment_with_a_default_registry() -> None:
    built = GatewaySettings.from_env(ENV)

    assert (built.mode, built.environment) == ("replay", "ci")
    assert built.registry_dir == Path("config/registry")
    assert built.database_url == DSN


@pytest.mark.parametrize(
    "missing",
    ["MERIDIAN_GATEWAY_MODE", "MERIDIAN_ENVIRONMENT", "MERIDIAN_DATABASE_URL"],
)
def test_a_missing_variable_is_named_and_no_value_is_shown(missing: str) -> None:
    environ = {k: v for k, v in ENV.items() if k != missing}

    with pytest.raises(SettingsError) as raised:
        GatewaySettings.from_env(environ)

    assert missing in str(raised.value)
    assert "s3cret-value" not in str(raised.value)


def test_a_malformed_value_never_shows_the_other_values() -> None:
    with pytest.raises(ValidationError) as raised:
        GatewaySettings.from_env({**ENV, "MERIDIAN_ENVIRONMENT": "prod"})

    assert "s3cret-value" not in str(raised.value)


def test_the_repr_hides_the_database_url() -> None:
    assert "s3cret-value" not in repr(settings())


# ── live mode settings (S010) ────────────────────────────────────────────────
ENDPOINTS_ENV = "MERIDIAN_AZURE_OPENAI_ENDPOINTS"
CREDENTIAL_ENV = "MERIDIAN_AZURE_CREDENTIAL"
TENANT_ENV = "MERIDIAN_AZURE_TENANT_ID"
ACCOUNT_HOST = "oai-meridian-sdc-a1b2c3.openai.azure.com"
GOOD_ENDPOINT = f"https://{ACCOUNT_HOST}"
TENANT_ID = "0f1e2d3c-aaaa-bbbb-cccc-0123456789ab"


def endpoints_env(endpoints: object) -> dict[str, str]:
    return {**ENV, ENDPOINTS_ENV: json.dumps(endpoints)}


def test_the_three_azure_variables_are_optional() -> None:
    built = GatewaySettings.from_env(ENV)

    assert dict(built.azure_openai_endpoints) == {}
    assert built.azure_credential is None
    assert built.azure_tenant_id is None


def test_local_is_an_accepted_environment() -> None:
    built = GatewaySettings.from_env({**ENV, "MERIDIAN_ENVIRONMENT": "local"})

    assert built.environment == "local"


def test_the_azure_variables_are_read_from_the_environment() -> None:
    environ = {
        **endpoints_env({"sdc": GOOD_ENDPOINT}),
        CREDENTIAL_ENV: "azure-cli",
        TENANT_ENV: TENANT_ID,
    }

    built = GatewaySettings.from_env(environ)

    assert dict(built.azure_openai_endpoints) == {"sdc": GOOD_ENDPOINT}
    assert built.azure_credential == "azure-cli"
    assert built.azure_tenant_id == TENANT_ID


@pytest.mark.parametrize(
    "endpoint",
    [
        GOOD_ENDPOINT,
        GOOD_ENDPOINT + "/",
        GOOD_ENDPOINT + ":443",
        GOOD_ENDPOINT + ":443/",
        GOOD_ENDPOINT.replace("https", "HTTPS"),
        GOOD_ENDPOINT.upper().replace("HTTPS", "https"),
    ],
    ids=["bare", "slash", "port", "port-slash", "upper-case-scheme", "upper-case-host"],
)
def test_an_accepted_endpoint_is_stored_as_https_and_the_host_only(
    endpoint: str,
) -> None:
    built = GatewaySettings.from_env(endpoints_env({"sdc": endpoint}))

    assert dict(built.azure_openai_endpoints) == {"sdc": GOOD_ENDPOINT}


def test_the_stored_endpoints_are_read_only() -> None:
    built = settings(azure_openai_endpoints={"sdc": GOOD_ENDPOINT})

    assert isinstance(built.azure_openai_endpoints, MappingProxyType)
    with pytest.raises(TypeError):
        built.azure_openai_endpoints["gwc"] = GOOD_ENDPOINT  # type: ignore[index]


def test_endpoints_given_directly_are_stored_in_canonical_form() -> None:
    built = settings(azure_openai_endpoints={"sdc": GOOD_ENDPOINT + ":443/"})

    assert dict(built.azure_openai_endpoints) == {"sdc": GOOD_ENDPOINT}


@pytest.mark.parametrize(
    "endpoint",
    [
        pytest.param(f"http://{ACCOUNT_HOST}", id="http"),
        pytest.param(ACCOUNT_HOST, id="no-scheme"),
        pytest.param("https://evil.example.com", id="other-host"),
        pytest.param("https://openai.azure.com", id="no-account-label"),
        pytest.param("https://.openai.azure.com", id="empty-account-label"),
        pytest.param(f"https://{ACCOUNT_HOST}.evil.example", id="suffix-trick"),
        pytest.param(f"https://evil.example/{ACCOUNT_HOST}", id="host-in-path"),
        pytest.param(f"https://{ACCOUNT_HOST}@evil.example", id="userinfo-host"),
        pytest.param(f"https://user:pw@{ACCOUNT_HOST}", id="userinfo"),
        pytest.param(f"https://user@{ACCOUNT_HOST}", id="user-only"),
        pytest.param(f"https://{ACCOUNT_HOST}/?k=v", id="query"),
        pytest.param(f"https://{ACCOUNT_HOST}/?", id="empty-query"),
        pytest.param(f"https://{ACCOUNT_HOST}/#frag", id="fragment"),
        pytest.param(f"https://{ACCOUNT_HOST}/#", id="empty-fragment"),
        pytest.param(f"https://{ACCOUNT_HOST}/openai", id="path"),
        pytest.param(f"https://{ACCOUNT_HOST}:8443", id="port"),
        pytest.param(f"https://{ACCOUNT_HOST}:0", id="port-zero"),
        pytest.param(f"https://{ACCOUNT_HOST}:abc", id="port-not-a-number"),
        pytest.param(f"https://{ACCOUNT_HOST}\\@evil.example", id="backslash"),
        pytest.param(f"https://{ACCOUNT_HOST} /", id="space"),
        pytest.param("", id="empty"),
        # This project's accounts only (T-43): the name Terraform gives one.
        pytest.param("https://other-account.openai.azure.com", id="other-account"),
        pytest.param(
            "https://meridian-sdc-a1b2c3.openai.azure.com", id="no-oai-prefix"
        ),
        pytest.param("https://oai-meridian-sdc.openai.azure.com", id="no-suffix"),
        pytest.param("https://oai-meridian-sdc-.openai.azure.com", id="empty-suffix"),
        pytest.param(
            "https://oai-meridian-gwc-a1b2c3.openai.azure.com", id="other-location"
        ),
        pytest.param(
            "https://oai-meridian-sdc-a1-b2.openai.azure.com", id="hyphen-in-suffix"
        ),
        pytest.param(f"https://x.{ACCOUNT_HOST}", id="extra-label"),
        pytest.param(f"https://{ACCOUNT_HOST}.", id="trailing-dot"),
        # Non-printable characters anywhere in the raw value.
        pytest.param("\x01" + GOOD_ENDPOINT, id="leading-control-character"),
        pytest.param("\x1f" + GOOD_ENDPOINT, id="leading-unit-separator"),
        pytest.param(GOOD_ENDPOINT + "\n", id="trailing-newline"),
        pytest.param(GOOD_ENDPOINT + "\x00", id="trailing-nul"),
        pytest.param(GOOD_ENDPOINT.replace("https://", "https://\t"), id="tab"),
        pytest.param(GOOD_ENDPOINT + "/\u200b", id="zero-width-space"),
        pytest.param(GOOD_ENDPOINT + "\x7f", id="delete"),
    ],
)
def test_an_endpoint_that_breaks_a_rule_is_refused_naming_the_variable(
    endpoint: str,
) -> None:
    with pytest.raises(SettingsError) as raised:
        GatewaySettings.from_env(endpoints_env({"sdc": endpoint}))

    assert ENDPOINTS_ENV in str(raised.value)
    assert endpoint not in str(raised.value) or endpoint == ""


@pytest.mark.parametrize("key", ["SDC", "sdc/gpt", "sdc_1", "", "sdc "])
def test_a_location_key_outside_lowercase_letters_digits_and_hyphen_is_refused(
    key: str,
) -> None:
    endpoint = f"https://{ACCOUNT_NAME_PREFIX}{key}-a1b2c3.openai.azure.com"

    with pytest.raises(SettingsError, match=ENDPOINTS_ENV):
        GatewaySettings.from_env(endpoints_env({key: endpoint}))


def test_a_location_key_with_a_hyphen_and_digits_names_its_account() -> None:
    endpoint = "https://oai-meridian-gwc-2-0f9e8d.openai.azure.com"

    built = GatewaySettings.from_env(endpoints_env({"gwc-2": endpoint}))

    assert dict(built.azure_openai_endpoints) == {"gwc-2": endpoint}


def test_the_account_name_prefix_is_the_one_terraform_gives_the_accounts() -> None:
    terraform = (REPO_ROOT / "infra/terraform/foundation/openai.tf").read_text(
        encoding="utf-8"
    )

    # The account's own block: a deployment's name is not an account name.
    account = re.search(
        r'^resource "azurerm_cognitive_account" "openai" \{\n(.*?)^\}',
        terraform,
        re.M | re.S,
    )
    assert account is not None
    names = re.findall(
        r'^\s*(?:name|custom_subdomain_name)\s*=\s*"([^"]*)"', account.group(1), re.M
    )

    expected = ACCOUNT_NAME_PREFIX + "${each.key}-"
    assert len(names) == 2
    assert all(name.startswith(expected) for name in names), names


@pytest.mark.parametrize(
    "raw",
    ["{not json", "[]", '"sdc"', "null", '{"sdc": 1}', '{"sdc": null}'],
)
def test_a_value_that_is_not_a_json_object_of_strings_is_refused(raw: str) -> None:
    with pytest.raises(SettingsError) as raised:
        GatewaySettings.from_env({**ENV, ENDPOINTS_ENV: raw})

    assert ENDPOINTS_ENV in str(raised.value)


def test_the_error_never_shows_the_value_or_the_other_values() -> None:
    secret_host = "https://secret-account.example.com"  # noqa: S105

    with pytest.raises(SettingsError) as raised:
        GatewaySettings.from_env(endpoints_env({"sdc": secret_host}))

    message = str(raised.value)
    assert "secret-account" not in message
    assert "s3cret-value" not in message
    assert raised.value.__cause__ is None


def test_an_error_in_the_json_never_shows_the_text() -> None:
    with pytest.raises(SettingsError) as raised:
        GatewaySettings.from_env({**ENV, ENDPOINTS_ENV: "{secret-account"})

    assert "secret-account" not in str(raised.value)
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None or raised.value.__suppress_context__


def test_a_bad_endpoint_given_directly_is_refused_without_the_value() -> None:
    with pytest.raises(ValidationError) as raised:
        settings(azure_openai_endpoints={"sdc": "https://secret-account.example.com"})

    assert "secret-account" not in str(raised.value)


def test_a_tenant_id_is_stored_in_canonical_uuid_form() -> None:
    braced = "{" + TENANT_ID.upper() + "}"

    built = GatewaySettings.from_env({**ENV, TENANT_ENV: braced})

    assert built.azure_tenant_id == TENANT_ID


@pytest.mark.parametrize(
    "tenant",
    [
        "not-a-uuid",
        "contoso.onmicrosoft.com",
        TENANT_ID + "0",
        "--help",
        " ",
        "x" * 500,
    ],
)
def test_a_tenant_id_that_is_not_a_uuid_is_refused_naming_the_variable_only(
    tenant: str,
) -> None:
    with pytest.raises(SettingsError) as raised:
        GatewaySettings.from_env({**ENV, TENANT_ENV: tenant})

    assert TENANT_ENV in str(raised.value)
    assert tenant.strip() not in str(raised.value) or not tenant.strip()
    assert raised.value.__cause__ is None


def test_a_tenant_id_given_directly_that_is_not_a_uuid_is_refused_unseen() -> None:
    with pytest.raises(ValidationError) as raised:
        settings(azure_tenant_id="secret-tenant")

    assert "secret-tenant" not in str(raised.value)


def test_an_unknown_credential_source_is_refused() -> None:
    with pytest.raises(ValidationError) as raised:
        GatewaySettings.from_env({**ENV, CREDENTIAL_ENV: "default"})

    assert "s3cret-value" not in str(raised.value)


def test_the_repr_hides_the_endpoints_the_credential_and_the_tenant() -> None:
    built = settings(
        azure_openai_endpoints={"sdc": GOOD_ENDPOINT},
        azure_credential="azure-cli",
        azure_tenant_id=TENANT_ID,
    )

    shown = repr(built)

    assert "meridian-sdc" not in shown
    assert TENANT_ID not in shown
    assert "azure-cli" not in shown


def test_replay_in_the_azure_environment_is_refused() -> None:
    with pytest.raises(SettingsError, match="replay"):
        create_app(settings(environment="azure"))


@pytest.mark.parametrize("environment", ["test", "ci", "kind"])
def test_replay_starts_in_the_three_safe_environments(environment: str) -> None:
    create_app(settings(environment=environment))


ALL_ENVIRONMENTS = ["local", "test", "ci", "kind", "azure"]
NON_LOCAL_ENVIRONMENTS = ["test", "ci", "kind", "azure"]
ACCOUNT = GOOD_ENDPOINT
AZURE_CLI = {"azure_credential": "azure-cli", "azure_tenant_id": TENANT_ID}
LOCAL_LIVE = {
    "mode": "live",
    "environment": "local",
    "azure_openai_endpoints": {"sdc": ACCOUNT},
    **AZURE_CLI,
}


class FakeProvider:
    def chat(self, deployment: object, request: object) -> object:
        raise AssertionError("no request is made in a start test")


class TokenSource:
    """Stands in for ``azure_cli_token_provider``: never runs ``az``."""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.tenants: list[str | None] = []
        self.fetches = 0

    def __call__(self, tenant_id: str | None):
        self.tenants.append(tenant_id)

        def token() -> str:
            self.fetches += 1
            if self.error is not None:
                raise self.error
            return "fake-entra-token"

        return token


SDK_ENVIRONMENT_PREFIXES = ("OPENAI_", "AZURE_OPENAI_")


@pytest.fixture(autouse=True)
def no_sdk_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """A developer's own OPENAI_* variables must not decide these tests."""
    for name in list(os.environ):
        if name.startswith(SDK_ENVIRONMENT_PREFIXES):
            monkeypatch.delenv(name)


@pytest.fixture
def token_source(monkeypatch: pytest.MonkeyPatch) -> TokenSource:
    source = TokenSource()
    monkeypatch.setattr(azure_openai, "azure_cli_token_provider", source)
    return source


@pytest.mark.parametrize("environment", ALL_ENVIRONMENTS)
def test_live_mode_with_injected_providers_starts_in_every_environment(
    environment: str,
) -> None:
    app = create_app(
        settings(mode="live", environment=environment),
        providers={"azure-openai": FakeProvider()},
    )

    assert app.title == "Meridian Model Gateway"


@pytest.mark.parametrize("environment", ALL_ENVIRONMENTS)
def test_live_mode_without_a_credential_is_refused(environment: str) -> None:
    with pytest.raises(SettingsError, match="MERIDIAN_AZURE_CREDENTIAL"):
        create_app(settings(mode="live", environment=environment))


@pytest.mark.parametrize("environment", NON_LOCAL_ENVIRONMENTS)
def test_live_mode_with_the_azure_cli_credential_is_refused_outside_local(
    environment: str, token_source: TokenSource
) -> None:
    with pytest.raises(SettingsError, match="local environment") as raised:
        create_app(
            settings(
                mode="live",
                environment=environment,
                azure_openai_endpoints={"sdc": ACCOUNT},
                **AZURE_CLI,
            )
        )

    assert token_source.fetches == 0
    assert TENANT_ID not in str(raised.value)


def test_live_mode_starts_in_local_after_one_token_fetch(
    token_source: TokenSource,
) -> None:
    app = create_app(settings(**LOCAL_LIVE))

    assert app.title == "Meridian Model Gateway"
    assert token_source.tenants == [TENANT_ID]
    assert token_source.fetches == 1


def test_live_mode_without_a_tenant_id_is_refused_before_a_token(
    token_source: TokenSource,
) -> None:
    # The az default account must not decide which tenant a token is for.
    without_tenant = {k: v for k, v in LOCAL_LIVE.items() if k != "azure_tenant_id"}

    with pytest.raises(SettingsError, match="MERIDIAN_AZURE_TENANT_ID"):
        create_app(settings(**without_tenant))

    assert token_source.tenants == []
    assert token_source.fetches == 0


@pytest.mark.parametrize(
    "name", ["OPENAI_ORG_ID", "OPENAI_PROJECT_ID", "OPENAI_CUSTOM_HEADERS"]
)
def test_live_start_is_refused_when_an_sdk_variable_is_set(
    monkeypatch: pytest.MonkeyPatch, token_source: TokenSource, name: str
) -> None:
    monkeypatch.setenv(name, "value-that-must-not-show")

    with pytest.raises(SettingsError, match=name) as raised:
        create_app(settings(**LOCAL_LIVE))

    assert "value-that-must-not-show" not in str(raised.value)
    assert token_source.fetches == 0


def test_a_live_gateway_builds_no_replay_provider(
    monkeypatch: pytest.MonkeyPatch, token_source: TokenSource
) -> None:
    def forbidden() -> None:
        raise AssertionError("a live gateway must not build the replay provider")

    monkeypatch.setattr(gateway_app, "ReplayProvider", forbidden)

    create_app(settings(**LOCAL_LIVE))


@pytest.mark.parametrize("injected", [False, True], ids=["built", "injected"])
def test_a_chat_route_candidate_of_provider_kind_replay_is_refused_in_live_mode(
    monkeypatch: pytest.MonkeyPatch, token_source: TokenSource, injected: bool
) -> None:
    # The registry checks refuse it, so the registry is changed in memory.
    registry = load_registry(REGISTRY_DIR)
    replay_route = tuple(
        route.model_copy(update={"candidates": ("aoai-sdc-gpt-4o", "replay-chat")})
        if route.purpose == "chat"
        else route
        for route in registry.routes
    )
    monkeypatch.setattr(
        gateway_app,
        "load_registry",
        lambda _: registry.model_copy(update={"routes": replay_route}),
    )
    providers = (
        {"azure-openai": FakeProvider(), "replay": FakeProvider()} if injected else None
    )
    live = LOCAL_LIVE if not injected else {"mode": "live", "environment": "test"}

    with pytest.raises(SettingsError, match="replay"):
        create_app(settings(**live), providers=providers)

    assert token_source.fetches == 0


@pytest.mark.parametrize("injected", [False, True], ids=["built", "injected"])
def test_an_embedding_route_candidate_of_provider_kind_replay_is_refused_in_live_mode(
    monkeypatch: pytest.MonkeyPatch, token_source: TokenSource, injected: bool
) -> None:
    # The registry checks refuse it, so the registry is changed in memory.
    registry = load_registry(REGISTRY_DIR)
    replay_route = tuple(
        route.model_copy(
            update={
                "candidates": ("aoai-sdc-text-embedding-3-large", "replay-embedding")
            }
        )
        if route.purpose == "embedding"
        else route
        for route in registry.routes
    )
    monkeypatch.setattr(
        gateway_app,
        "load_registry",
        lambda _: registry.model_copy(update={"routes": replay_route}),
    )
    providers = (
        {"azure-openai": FakeProvider(), "replay": FakeProvider()} if injected else None
    )
    live = LOCAL_LIVE if not injected else {"mode": "live", "environment": "test"}

    with pytest.raises(SettingsError, match="the embedding route has a replay"):
        create_app(settings(**live), providers=providers)

    assert token_source.fetches == 0


def narrowed_registry(update: Callable[[Registry], dict]) -> Registry:
    """The real registry changed in memory, as the registry checks would refuse."""
    registry = load_registry(REGISTRY_DIR)
    return registry.model_copy(update=update(registry))


def with_deployment_changed(deployment_id: str, **changes: object) -> Registry:
    return narrowed_registry(
        lambda r: {
            "deployments": tuple(
                d.model_copy(update=changes) if d.id == deployment_id else d
                for d in r.deployments
            )
        }
    )


def test_a_routed_embedding_deployment_without_an_endpoint_is_refused_before_a_token(
    monkeypatch: pytest.MonkeyPatch, token_source: TokenSource
) -> None:
    # Chat is in the one configured location; the embedding deployment is not.
    elsewhere = with_deployment_changed(
        "aoai-sdc-text-embedding-3-large", terraform_key="gwc/text-embedding-3-large"
    )
    monkeypatch.setattr(gateway_app, "load_registry", lambda _: elsewhere)

    with pytest.raises(
        SettingsError, match="aoai-sdc-text-embedding-3-large"
    ) as raised:
        create_app(settings(**LOCAL_LIVE))

    assert "ENDPOINTS" in str(raised.value)
    assert "oai-meridian" not in str(raised.value)
    assert token_source.fetches == 0


@pytest.mark.parametrize(
    ("missing", "message"),
    [
        ("terraform_key", "terraform_key or deployment_name"),
        ("deployment_name", "terraform_key or deployment_name"),
        ("dimensions", "no dimensions"),
    ],
)
def test_a_routed_embedding_deployment_missing_a_field_is_refused_at_the_start(
    monkeypatch: pytest.MonkeyPatch,
    token_source: TokenSource,
    missing: str,
    message: str,
) -> None:
    broken = with_deployment_changed(
        "aoai-sdc-text-embedding-3-large", **{missing: None}
    )
    monkeypatch.setattr(gateway_app, "load_registry", lambda _: broken)

    with pytest.raises(SettingsError, match=message):
        create_app(settings(**LOCAL_LIVE))

    assert token_source.fetches == 0


def test_an_embedding_route_naming_a_deployment_the_registry_lacks_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ghost = narrowed_registry(
        lambda r: {
            "routes": tuple(
                route.model_copy(update={"candidates": ("ghost-deployment",)})
                if route.purpose == "embedding"
                else route
                for route in r.routes
            )
        }
    )
    monkeypatch.setattr(gateway_app, "load_registry", lambda _: ghost)

    with pytest.raises(SettingsError, match="the embedding route names a deployment"):
        create_app(
            settings(mode="live", environment="test"),
            providers={"azure-openai": FakeProvider()},
        )


def test_replay_mode_without_a_replay_embedding_deployment_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    without = narrowed_registry(
        lambda r: {"replay": tuple(x for x in r.replay if x.purpose != "embedding")}
    )
    monkeypatch.setattr(gateway_app, "load_registry", lambda _: without)

    with pytest.raises(SettingsError, match="no replay deployment for embedding"):
        create_app(settings())


def test_a_live_start_asks_nothing_of_a_provider_that_only_chats() -> None:
    # No method of the injected provider is called, or looked for, at the start:
    # a fake with ``chat`` alone is a valid provider until an embedding arrives.
    app = create_app(
        settings(mode="live", environment="test"),
        providers={"azure-openai": FakeProvider()},
    )

    assert "/v1/embeddings" in app.openapi()["paths"]


def test_the_providers_the_app_built_are_closed_when_it_stops(
    monkeypatch: pytest.MonkeyPatch, token_source: TokenSource
) -> None:
    closed: list[str] = []

    class SpyProvider:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def close(self) -> None:
            closed.append("closed")

    monkeypatch.setattr(azure_openai, "AzureOpenAIProvider", SpyProvider)
    app = create_app(settings(**LOCAL_LIVE))
    assert closed == []

    with TestClient(app):
        assert closed == []

    assert closed == ["closed"]


def test_a_provider_the_caller_injected_is_not_closed_by_the_app() -> None:
    class Injected(FakeProvider):
        closed = False

        def close(self) -> None:
            self.closed = True

    injected = Injected()
    app = create_app(
        settings(mode="live", environment="test"), providers={"azure-openai": injected}
    )

    with TestClient(app):
        pass

    assert injected.closed is False


def test_a_routed_azure_deployment_without_an_endpoint_is_refused_before_a_token(
    token_source: TokenSource,
) -> None:
    other_location = {"gwc": "https://oai-meridian-gwc-a1b2c3.openai.azure.com"}

    with pytest.raises(SettingsError, match="aoai-sdc-gpt-4o") as raised:
        create_app(settings(**LOCAL_LIVE | {"azure_openai_endpoints": other_location}))

    assert "ENDPOINTS" in str(raised.value)
    assert "oai-meridian" not in str(raised.value)
    assert token_source.fetches == 0


@pytest.mark.parametrize("missing", ["terraform_key", "deployment_name"])
def test_a_routed_azure_deployment_missing_a_provider_field_is_refused(
    monkeypatch: pytest.MonkeyPatch, token_source: TokenSource, missing: str
) -> None:
    # The registry checks require both fields, so the registry is changed in
    # memory after it was loaded.
    registry = load_registry(REGISTRY_DIR)
    broken = registry.model_copy(
        update={
            "deployments": tuple(
                d.model_copy(update={missing: None}) if d.id == "aoai-sdc-gpt-4o" else d
                for d in registry.deployments
            )
        }
    )
    monkeypatch.setattr(gateway_app, "load_registry", lambda _: broken)

    with pytest.raises(SettingsError, match="terraform_key or deployment_name"):
        create_app(settings(**LOCAL_LIVE))

    assert token_source.fetches == 0


@pytest.mark.parametrize(
    "error",
    [
        CredentialUnavailableError(f"az failed for tenant {TENANT_ID} at {ACCOUNT}"),
        ClientAuthenticationError(f"AADSTS for {TENANT_ID}"),
    ],
)
def test_a_failing_token_fetch_stops_the_start_with_az_login_and_no_cause(
    token_source: TokenSource, error: Exception
) -> None:
    token_source.error = error

    with pytest.raises(SettingsError, match="az login") as raised:
        create_app(settings(**LOCAL_LIVE))

    assert TENANT_ID not in str(raised.value)
    assert ACCOUNT not in str(raised.value)
    assert raised.value.__cause__ is None
    assert raised.value.__suppress_context__


def test_a_routed_provider_kind_with_no_injected_provider_is_refused() -> None:
    with pytest.raises(SettingsError, match="no adapter"):
        create_app(
            settings(mode="live", environment="test"),
            providers={"replay": FakeProvider()},
        )


def test_replay_in_the_local_environment_is_refused() -> None:
    with pytest.raises(SettingsError, match="replay"):
        create_app(settings(environment="local"))


def test_a_registry_that_fails_to_load_stops_the_start(tmp_path: Path) -> None:
    (tmp_path / "models.yaml").write_text("deployments: nonsense\n", encoding="utf-8")

    with pytest.raises(RegistryError):
        create_app(settings(registry_dir=tmp_path))


def test_a_missing_registry_directory_stops_the_start(tmp_path: Path) -> None:
    with pytest.raises(RegistryError):
        create_app(settings(registry_dir=tmp_path / "absent"))
