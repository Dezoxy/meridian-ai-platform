"""The knowledge tool server's settings and its HTTP client (S046): the
gateway's address is checked like the ingestion's, never echoed, and the client
the app builds is closed when the app stops (no database needed)."""

from pathlib import Path

import httpx
import pydantic
import pytest
from servicesupport import REGISTRY_DIR
from starlette.testclient import TestClient

from meridian.platform.common.env import SettingsError
from meridian.platform.knowledge_mcp import app as knowledge_app
from meridian.platform.knowledge_mcp.settings import (
    GATEWAY_URL_ENV,
    KnowledgeServerSettings,
    gateway_url_problem,
)
from meridian.runtime.tool_client import TOOL_TIMEOUT_SECONDS

DSN = "postgresql://role:dsn-fixture-password@db.invalid/meridian"
GATEWAY_URL = "http://gateway.invalid:8080"
SECRET = "gateway-credential-fixture"  # noqa: S105 (a marker, not a credential)
ENVIRONMENT = {
    "MERIDIAN_REGISTRY_DIR": str(REGISTRY_DIR),
    "MERIDIAN_DATABASE_URL": DSN,
    "MERIDIAN_ALLOWED_HOSTS": "knowledge-mcp:8080",
    GATEWAY_URL_ENV: GATEWAY_URL,
}
# Each value breaks one rule; the text after the colon is what must not leak.
REFUSED_URLS = [
    f"http://operator:{SECRET}@gateway.invalid:8080",
    f"https://operator:{SECRET}@gateway.invalid",
    "http://operator@gateway.invalid",
    f"http://:{SECRET}@gateway.invalid",
    f"ftp://{SECRET}.invalid",
    f"{SECRET}:8080",
    "http://",
    f"http://{SECRET}:abc",
    f"http://{SECRET}:99999",
    f"http://[{SECRET}",
]


def settings(gateway_url: str = GATEWAY_URL) -> KnowledgeServerSettings:
    return KnowledgeServerSettings(
        registry_dir=REGISTRY_DIR,
        database_url=DSN,
        allowed_hosts=("knowledge-mcp:8080",),
        gateway_url=gateway_url,
    )


# ── the rule ────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "value", [GATEWAY_URL, "https://gateway.invalid", "http://gateway.invalid/v1"]
)
def test_a_usable_gateway_address_has_no_problem(value: str) -> None:
    assert gateway_url_problem(value) is None


@pytest.mark.parametrize("value", REFUSED_URLS)
def test_a_refused_gateway_address_names_the_rule_and_not_the_value(
    value: str,
) -> None:
    problem = gateway_url_problem(value)

    assert problem
    for fragment in (SECRET, "operator", "gateway.invalid"):
        assert fragment not in problem


# ── the settings ────────────────────────────────────────────────────────────
def test_the_settings_read_the_gateway_address_from_the_environment() -> None:
    read = KnowledgeServerSettings.from_env(ENVIRONMENT)

    assert read.gateway_url == GATEWAY_URL
    assert read.database_url == DSN
    assert read.allowed_hosts == ("knowledge-mcp:8080",)


@pytest.mark.parametrize("value", [None, ""])
def test_a_missing_gateway_address_is_a_settings_error_that_names_it(
    value: str | None,
) -> None:
    environment = {k: v for k, v in ENVIRONMENT.items() if k != GATEWAY_URL_ENV}
    if value is not None:
        environment[GATEWAY_URL_ENV] = value

    with pytest.raises(SettingsError, match=GATEWAY_URL_ENV):
        KnowledgeServerSettings.from_env(environment)


@pytest.mark.parametrize("missing", ["MERIDIAN_DATABASE_URL", "MERIDIAN_ALLOWED_HOSTS"])
def test_the_variables_of_every_tool_server_are_still_required(missing: str) -> None:
    environment = {k: v for k, v in ENVIRONMENT.items() if k != missing}

    with pytest.raises(SettingsError, match=missing):
        KnowledgeServerSettings.from_env(environment)


@pytest.mark.parametrize("value", REFUSED_URLS)
def test_a_refused_address_from_the_environment_names_the_variable_not_the_value(
    value: str,
) -> None:
    with pytest.raises(SettingsError, match=GATEWAY_URL_ENV) as raised:
        KnowledgeServerSettings.from_env({**ENVIRONMENT, GATEWAY_URL_ENV: value})

    for shown in (str(raised.value), repr(raised.value)):
        for fragment in (SECRET, "operator"):
            assert fragment not in shown


@pytest.mark.parametrize("value", REFUSED_URLS)
def test_a_refused_address_given_to_the_constructor_is_not_in_the_error(
    value: str,
) -> None:
    with pytest.raises(pydantic.ValidationError) as raised:
        settings(value)

    assert GATEWAY_URL_ENV in str(raised.value)
    for shown in (str(raised.value), repr(raised.value)):
        for fragment in (SECRET, "operator"):
            assert fragment not in shown


def test_the_repr_of_the_settings_shows_neither_url() -> None:
    shown = repr(settings())

    assert "gateway.invalid" not in shown
    assert "db.invalid" not in shown
    assert "dsn-fixture-password" not in shown


# ── the client the app builds ───────────────────────────────────────────────
def test_the_client_is_built_on_the_gateway_without_proxies_or_redirects() -> None:
    with knowledge_app.make_http_client(GATEWAY_URL) as http:
        assert str(http.base_url).rstrip("/") == GATEWAY_URL
        assert http.trust_env is False
        assert http.follow_redirects is False
        assert http.timeout == knowledge_app.EMBEDDING_TIMEOUT


def test_the_embedding_timeout_leaves_the_tool_call_time_for_the_search() -> None:
    timeout = knowledge_app.EMBEDDING_TIMEOUT

    assert (timeout.connect, timeout.read) == (2.0, 5.0)
    assert timeout.read < TOOL_TIMEOUT_SECONDS


def test_the_client_the_app_builds_is_closed_when_the_lifespan_ends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built: list[httpx.Client] = []
    real = knowledge_app.make_http_client

    def capture(gateway_url: str) -> httpx.Client:
        built.append(real(gateway_url))
        return built[-1]

    monkeypatch.setattr(knowledge_app, "make_http_client", capture)
    app = knowledge_app.create_app(settings())
    (http,) = built

    with TestClient(app.app):
        assert http.is_closed is False

    assert http.is_closed is True


def test_an_injected_client_is_not_closed_by_the_app() -> None:
    with httpx.Client(base_url=GATEWAY_URL) as http:
        app = knowledge_app.create_app(settings(), http=http)

        with TestClient(app.app):
            pass

        assert http.is_closed is False


def test_an_address_the_client_cannot_take_stops_the_start_without_the_value() -> None:
    value = f"http://{SECRET}\x01way:8080"
    assert gateway_url_problem(value) is None  # urlsplit takes it, httpx does not

    with pytest.raises(SettingsError, match=GATEWAY_URL_ENV) as raised:
        knowledge_app.create_app(settings(value))

    assert SECRET not in str(raised.value)


def test_a_client_built_for_a_start_that_fails_is_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    built: list[httpx.Client] = []
    real = knowledge_app.make_http_client

    def capture(gateway_url: str) -> httpx.Client:
        built.append(real(gateway_url))
        return built[-1]

    monkeypatch.setattr(knowledge_app, "make_http_client", capture)
    broken = settings().model_copy(update={"registry_dir": tmp_path})

    with pytest.raises(Exception, match=r"."):  # the registry is not there
        knowledge_app.create_app(broken)

    (http,) = built
    assert http.is_closed is True


def test_create_app_from_env_builds_the_asgi_app(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name, value in ENVIRONMENT.items():
        monkeypatch.setenv(name, value)

    assert knowledge_app.create_app_from_env() is not None
