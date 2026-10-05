"""Claims API settings."""

import pytest
from pydantic import ValidationError

from meridian.platform.common.env import SettingsError
from meridian.workloads.claims_triage.lifecycle import (
    DOCUMENTS_DEADLINE_DAYS,
    DOCUMENTS_DEADLINE_ENV,
)
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


def test_the_documents_deadline_defaults_to_the_lifecycles() -> None:
    built = ClaimsSettings.from_env(ENV)

    assert built.documents_deadline_days == DOCUMENTS_DEADLINE_DAYS == 14


@pytest.mark.parametrize("value", ["1", "30", "365"])
def test_the_documents_deadline_is_read_from_the_variable_the_sweep_reads(
    value: str,
) -> None:
    assert DOCUMENTS_DEADLINE_ENV == "MERIDIAN_SWEEP_DOCUMENTS_DEADLINE_DAYS"

    built = ClaimsSettings.from_env(ENV | {DOCUMENTS_DEADLINE_ENV: value})

    assert built.documents_deadline_days == int(value)


@pytest.mark.parametrize(
    "value", ["0", "366", "-1", "1.5", "14 days", "", " 14", "fourteen", "١٤", "1e1"]
)
def test_a_documents_deadline_the_sweep_would_refuse_is_refused_the_same_way(
    value: str,
) -> None:
    with pytest.raises(SettingsError) as refused:
        ClaimsSettings.from_env(ENV | {DOCUMENTS_DEADLINE_ENV: value})

    assert str(refused.value) == (
        f"{DOCUMENTS_DEADLINE_ENV} must be a whole number of days from 1 to 365"
    )


@pytest.mark.parametrize(
    ("days", "rule"),
    [
        (0, "greater_than_equal"),
        (-1, "greater_than_equal"),
        (366, "less_than_equal"),
    ],
)
def test_settings_built_with_a_deadline_out_of_bounds_are_refused(
    days: int, rule: str
) -> None:
    with pytest.raises(ValidationError) as raised:
        ClaimsSettings(
            runtime_url="http://runtime.invalid:8080",
            database_url=DSN,
            documents_deadline_days=days,
        )

    (error,) = raised.value.errors()
    assert (error["loc"], error["type"]) == (("documents_deadline_days",), rule)
    assert "s3cret-value" not in str(raised.value)


@pytest.mark.parametrize("days", [1, 365])
def test_settings_built_with_a_deadline_on_a_bound_are_taken(days: int) -> None:
    built = ClaimsSettings(
        runtime_url="http://runtime.invalid:8080",
        database_url=DSN,
        documents_deadline_days=days,
    )

    assert built.documents_deadline_days == days
