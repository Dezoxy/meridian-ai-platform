"""How long the services' certificates last, and when cert-manager renews them
(S062).

``certificate.duration`` and ``certificate.renewBefore`` in the chart's values
set the two fields of every Certificate the chart renders. The defaults render
what the chart rendered before the values existed: ``duration: 2160h`` and no
``renewBefore`` (cert-manager's default, a third of the lifetime). The template
refuses a duration above what the issuer's policy signs
(``infra/kind/manifests/certificate-policy.yaml``), below cert-manager's
shortest (one hour), a ``renewBefore`` that is not shorter than the duration
and one under five minutes (cert-manager's webhook refuses it); each message
names the value. The rest of the certificates is pinned
in ``test_helm_identity.py``.
"""

import re
from pathlib import Path

import pytest
import yaml
from chartsupport import helm_arguments, render, rendered_chart, run_helm
from servicesupport import REPO_ROOT

POLICY_FILE = REPO_ROOT / "infra" / "kind" / "manifests" / "certificate-policy.yaml"
SERVICES_POLICY = "meridian-services"
# cert-manager's shortest certificate lifetime, in minutes.
MINIMUM_MINUTES = 60
# The shortest renewBefore cert-manager's webhook accepts, in minutes.
MINIMUM_RENEW_BEFORE_MINUTES = 5
# One Certificate per service (six) and one for the ingestion Job.
CERTIFICATES = 7


def minutes_of(duration: str) -> int:
    """A duration of hours and minutes (``2160h``, ``1h30m``) in minutes."""
    match = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?", duration)
    assert match and duration, duration
    return int(match.group(1) or 0) * 60 + int(match.group(2) or 0)


def policy_cap_minutes(path: Path = POLICY_FILE) -> int:
    """What the issuer's policy for the services' certificates signs at most,
    read from the manifest."""
    policies = [d for d in yaml.safe_load_all(path.read_text(encoding="utf-8")) if d]
    (policy,) = [p for p in policies if p["metadata"]["name"] == SERVICES_POLICY]
    return minutes_of(policy["spec"]["constraints"]["maxDuration"])


def certificates(*values: str) -> list[dict]:
    """The Certificates of a render with each ``name=value`` of ``values`` set
    as a string."""
    arguments = helm_arguments()
    for value in values:
        arguments += ["--set-string", value]
    return [d for d in render(arguments) if d["kind"] == "Certificate"]


def refusal(*values: str) -> str:
    """The message of a render that fails; fails the test if it succeeds."""
    arguments = helm_arguments()
    for value in values:
        arguments += ["--set-string", value]
    done = run_helm(arguments)
    assert done.returncode != 0, "the render should have been refused"
    return done.stderr


def test_the_default_lifetime_is_ninety_days_and_cert_managers_own_renewal() -> None:
    found = [d for d in rendered_chart() if d["kind"] == "Certificate"]

    assert len(found) == CERTIFICATES
    for certificate in found:
        assert certificate["spec"]["duration"] == "2160h"
        assert "renewBefore" not in certificate["spec"]


def test_the_rendered_certificates_stay_inside_the_policys_cap() -> None:
    found = [d for d in rendered_chart() if d["kind"] == "Certificate"]

    assert found
    for certificate in found:
        assert minutes_of(certificate["spec"]["duration"]) <= policy_cap_minutes()


def test_an_hour_renders_on_every_certificate() -> None:
    found = certificates("certificate.duration=1h")

    assert len(found) == CERTIFICATES
    for certificate in found:
        assert certificate["spec"]["duration"] == "1h"
        assert "renewBefore" not in certificate["spec"]


def test_a_renew_before_renders_on_every_certificate() -> None:
    found = certificates("certificate.duration=1h", "certificate.renewBefore=30m")

    assert len(found) == CERTIFICATES
    for certificate in found:
        assert certificate["spec"]["duration"] == "1h"
        assert certificate["spec"]["renewBefore"] == "30m"


@pytest.mark.parametrize("empty", ["", "null"])
def test_an_empty_renew_before_leaves_the_field_out(empty: str) -> None:
    arguments = [*helm_arguments(), "--set", f"certificate.renewBefore={empty}"]

    found = [d for d in render(arguments) if d["kind"] == "Certificate"]

    assert len(found) == CERTIFICATES
    for certificate in found:
        assert "renewBefore" not in certificate["spec"]


def test_the_duration_the_policy_signs_at_most_renders() -> None:
    cap = policy_cap_minutes()

    found = certificates(f"certificate.duration={cap // 60}h")

    assert minutes_of(found[0]["spec"]["duration"]) == cap


def test_a_duration_above_the_policys_cap_fails_and_names_the_value() -> None:
    cap = policy_cap_minutes()

    above_by_an_hour = refusal(f"certificate.duration={cap // 60 + 1}h")
    above_by_a_minute = refusal(f"certificate.duration={cap // 60}h1m")

    for message in (above_by_an_hour, above_by_a_minute):
        assert "certificate.duration is" in message
        assert f"above {cap // 60}h" in message
        assert "certificate-policy.yaml" in message


def test_the_charts_cap_is_the_policys_cap() -> None:
    cap = policy_cap_minutes()

    hours = cap // 60
    inside = certificates(f"certificate.duration={hours - 1}h59m")
    outside = refusal(f"certificate.duration={hours}h1m")

    assert minutes_of(inside[0]["spec"]["duration"]) == cap - 1
    assert f"above {hours}h" in outside


@pytest.mark.parametrize("duration", ["59m", "30m", "0h", "1m"])
def test_a_duration_below_cert_managers_minimum_fails_and_names_the_value(
    duration: str,
) -> None:
    assert minutes_of(duration) < MINIMUM_MINUTES

    message = refusal(f"certificate.duration={duration}")

    assert "certificate.duration is" in message
    assert "below 1h" in message


def test_a_duration_of_exactly_the_minimum_renders() -> None:
    assert certificates("certificate.duration=60m")[0]["spec"]["duration"] == "60m"


@pytest.mark.parametrize(
    "renew_before", ["1h", "60m", "61m", "2h", "720h", "1h30m", "2161h"]
)
def test_a_renew_before_not_shorter_than_the_duration_fails_and_names_it(
    renew_before: str,
) -> None:
    assert minutes_of(renew_before) >= MINIMUM_MINUTES

    message = refusal(
        "certificate.duration=1h", f"certificate.renewBefore={renew_before}"
    )

    assert "certificate.renewBefore is" in message
    assert "not shorter than certificate.duration" in message


@pytest.mark.parametrize("renew_before", ["1m", "4m", "0m", "0h"])
def test_a_renew_before_under_five_minutes_fails_and_names_the_value_and_the_floor(
    renew_before: str,
) -> None:
    # cert-manager's webhook refused 1m and 4m ("certificate renewBefore must
    # be greater than 5m0s") and accepted 5m, asked by a server-side dry run
    # on 2026-10-06. 4m59s is not expressible: the value takes hours and minutes.
    assert minutes_of(renew_before) < MINIMUM_RENEW_BEFORE_MINUTES

    message = refusal(
        "certificate.duration=1h", f"certificate.renewBefore={renew_before}"
    )

    assert f"certificate.renewBefore is {renew_before}" in message
    assert "below 5m" in message
    assert "cert-manager" in message


def test_a_renew_before_of_exactly_five_minutes_renders() -> None:
    found = certificates("certificate.duration=1h", "certificate.renewBefore=5m")

    assert len(found) == CERTIFICATES
    for certificate in found:
        assert certificate["spec"]["renewBefore"] == "5m"


def test_the_floor_applies_to_the_default_duration_too() -> None:
    message = refusal("certificate.renewBefore=4m")

    assert "below 5m" in message
    assert certificates("certificate.renewBefore=5m")[0]["spec"]["renewBefore"] == "5m"


def test_a_renew_before_one_minute_shorter_than_the_duration_renders() -> None:
    found = certificates("certificate.duration=1h", "certificate.renewBefore=59m")

    assert found[0]["spec"]["renewBefore"] == "59m"


def test_a_renew_before_is_checked_against_the_default_duration_too() -> None:
    message = refusal("certificate.renewBefore=2160h")

    assert "certificate.renewBefore is" in message


@pytest.mark.parametrize("duration", ["90d", "1.5h", "3600s", "abc", "-1h", " 1h"])
def test_a_duration_that_is_not_hours_and_minutes_fails_and_names_the_value(
    duration: str,
) -> None:
    message = refusal(f"certificate.duration={duration}")

    assert "certificate.duration" in message
    assert "hours and minutes" in message


def test_an_empty_duration_fails_because_the_policy_cannot_evaluate_none() -> None:
    message = refusal("certificate.duration=")

    assert "certificate.duration" in message


@pytest.mark.parametrize("renew_before", ["30d", "x", "1s"])
def test_a_renew_before_that_is_not_hours_and_minutes_fails_and_names_the_value(
    renew_before: str,
) -> None:
    message = refusal(f"certificate.renewBefore={renew_before}")

    assert "certificate.renewBefore" in message
    assert "hours and minutes" in message


def test_a_value_with_a_line_break_cannot_add_a_field_to_a_certificate() -> None:
    message = refusal("certificate.duration=1h\nissuerRef: elsewhere")

    assert "certificate.duration" in message
