"""The switch that turns the Claims API's sign-in on (S021, D3): off unless the
variable says ``staff``, and any other word stops the start without being
echoed."""

import pytest

from meridian.platform.common.env import SettingsError
from meridian.workloads.claims_triage.settings import (
    SIGNIN_ENV,
    ClaimsSettings,
    signin_of,
)

ENV = {
    "MERIDIAN_RUNTIME_URL": "http://runtime.invalid:8080",
    "MERIDIAN_DATABASE_URL": "postgresql://claims_api@db.invalid/meridian",
}


def test_the_variable_is_named_meridian_signin() -> None:
    assert SIGNIN_ENV == "MERIDIAN_SIGNIN"


@pytest.mark.parametrize(("raw", "expected"), [(None, "off"), ("off", "off")])
def test_unset_and_off_are_off(raw: str | None, expected: str) -> None:
    assert signin_of(raw) == expected


def test_staff_is_on() -> None:
    assert signin_of("staff") == "staff"


@pytest.mark.parametrize(
    "raw", ["", "on", "true", "1", "Staff", "STAFF", "staff ", " staff", "claimant"]
)
def test_any_other_word_stops_the_start_and_is_not_echoed(raw: str) -> None:
    with pytest.raises(SettingsError) as raised:
        signin_of(raw)

    assert SIGNIN_ENV in str(raised.value)
    assert repr(raw) not in str(raised.value)
    # "claimant" is no part of the sentence, so a copy of the value would show.
    assert "claimant" not in str(raised.value)


def test_the_settings_are_off_by_default() -> None:
    assert ClaimsSettings.from_env(ENV).signin == "off"


def test_the_settings_read_the_variable() -> None:
    assert ClaimsSettings.from_env(ENV | {SIGNIN_ENV: "staff"}).signin == "staff"
    assert ClaimsSettings.from_env(ENV | {SIGNIN_ENV: "off"}).signin == "off"


def test_a_word_that_is_not_a_setting_stops_from_env_naming_the_variable() -> None:
    with pytest.raises(SettingsError) as raised:
        ClaimsSettings.from_env(ENV | {SIGNIN_ENV: "everyone"})

    assert SIGNIN_ENV in str(raised.value)
    assert "everyone" not in str(raised.value)
