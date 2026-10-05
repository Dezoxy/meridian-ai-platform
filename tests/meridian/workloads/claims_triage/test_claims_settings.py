"""Claims API settings."""

import pytest
from pydantic import ValidationError

from meridian.platform.common.env import SettingsError
from meridian.workloads.claims_triage.settings import ClaimsSettings

DSN = "postgresql://claims_api:s3cret-value@db.invalid/meridian"
ENV = {
    "MERIDIAN_RUNTIME_URL": "http://runtime.invalid:8080",
    "MERIDIAN_DATABASE_URL": DSN,
}


def test_the_tenant_defaults_to_claims_triage() -> None:
    built = ClaimsSettings.from_env(ENV)

    assert built.tenant == "claims-triage"
    assert built.runtime_url == "http://runtime.invalid:8080"
    assert "s3cret-value" not in repr(built)


def test_the_tenant_can_be_set() -> None:
    assert ClaimsSettings.from_env(ENV | {"MERIDIAN_TENANT": "evaluation"}).tenant == (
        "evaluation"
    )


@pytest.mark.parametrize("missing", ["MERIDIAN_RUNTIME_URL", "MERIDIAN_DATABASE_URL"])
def test_a_missing_variable_is_named_and_no_value_is_shown(missing: str) -> None:
    environ = {k: v for k, v in ENV.items() if k != missing}

    with pytest.raises(SettingsError) as raised:
        ClaimsSettings.from_env(environ)

    assert missing in str(raised.value)
    assert "s3cret-value" not in str(raised.value)


@pytest.mark.parametrize(
    "url",
    [
        "ftp://runtime.invalid",
        "runtime.invalid:8080",
        "file:///etc/passwd",
        "https:///nohost",
    ],
)
def test_a_runtime_url_that_is_not_http_or_https_is_refused_without_the_value(
    url: str,
) -> None:
    with pytest.raises(ValidationError) as raised:
        ClaimsSettings.from_env({**ENV, "MERIDIAN_RUNTIME_URL": url})

    assert "runtime_url" in str(raised.value)
    assert url not in str(raised.value)
    assert "s3cret-value" not in str(raised.value)


@pytest.mark.parametrize(
    "url",
    [
        "http://operator:s3cret-value@runtime.invalid:8080",
        "http://s3cret-value@runtime.invalid",
        "http://runtime.invalid/v1?token=s3cret-value",
        "http://runtime.invalid/v1#s3cret-value",
        "http://runtime.invalid:99999",
        " http://runtime.invalid",
        "http://runtime.invalid ",
    ],
)
def test_a_runtime_url_with_a_credential_or_a_query_is_refused_without_the_value(
    url: str,
) -> None:
    with pytest.raises(ValidationError) as raised:
        ClaimsSettings.from_env({**ENV, "MERIDIAN_RUNTIME_URL": url})

    assert "runtime_url" in str(raised.value)
    assert "s3cret-value" not in str(raised.value)
    assert "operator" not in str(raised.value)
    assert "runtime.invalid" not in str(raised.value)


@pytest.mark.parametrize(
    "url", ["https://runtime.invalid:8443", "http://runtime.invalid/base"]
)
def test_a_runtime_url_with_a_port_or_a_path_is_accepted(url: str) -> None:
    assert ClaimsSettings.from_env({**ENV, "MERIDIAN_RUNTIME_URL": url}).runtime_url
