"""The chart bounds its rollback history and spreads the restarts at a renewal
(S073).

Every Deployment keeps ``revisionHistoryLimit`` old ReplicaSets (2 by default),
one value, refused below 1. Every service gets
``MERIDIAN_TLS_RESTART_SHARE``, its place in the services' list over their count
(the order Helm lists a map in: sorted by name), which ``certlife`` turns into a
restart that much of a margin earlier than before. These tests render the chart
and feed the rendered values to ``certlife`` with the clock never read: they do
not show a restart on a cluster.
"""

from datetime import UTC, datetime, timedelta
from itertools import pairwise

import pytest
from chartsupport import (
    RATE_STORE,
    SERVICES,
    helm_arguments,
    render,
    rendered_chart,
    run_helm,
)

from meridian.platform.common.certlife import (
    RESTART_SHARE_ENV,
    LoadedCertificate,
    _restart_share,
)

START = datetime(2026, 1, 1, tzinfo=UTC)


def share_read(text: str) -> float:
    """What a service makes of the rendered text: the product's own reading of
    the variable, which refuses what it cannot use (a text that ``float`` takes
    and the product does not would pass a bare ``float``)."""
    return _restart_share({RESTART_SHARE_ENV: text})


def deployments(*values: str) -> list[dict]:
    """The Deployments of a render with each ``name=value`` of ``values`` set."""
    arguments = helm_arguments()
    for value in values:
        arguments += ["--set", value]
    return [d for d in render(arguments) if d["kind"] == "Deployment"]


def refusal(*values: str) -> str:
    arguments = helm_arguments()
    for value in values:
        arguments += ["--set", value]
    done = run_helm(arguments)
    assert done.returncode != 0, "the render should have been refused"
    return done.stderr


def shares() -> dict[str, str]:
    """The text of the share each service's container is given, by service."""
    found: dict[str, str] = {}
    for document in rendered_chart():
        if document["kind"] != "Deployment":
            continue
        (container,) = document["spec"]["template"]["spec"]["containers"]
        values = [
            item["value"]
            for item in container.get("env", [])
            if item["name"] == RESTART_SHARE_ENV
        ]
        if values:
            found[document["metadata"]["name"]] = values[0]
    return found


# ── the rollback history ─────────────────────────────────────────────────────
def test_every_deployment_keeps_two_old_replica_sets_by_default() -> None:
    found = [d for d in rendered_chart() if d["kind"] == "Deployment"]

    assert {d["metadata"]["name"] for d in found} == {*SERVICES, RATE_STORE}
    for deployment in found:
        assert deployment["spec"]["revisionHistoryLimit"] == 2


def test_the_rate_stores_recreate_deployment_has_the_limit_too() -> None:
    (store,) = [
        d
        for d in rendered_chart()
        if d["kind"] == "Deployment" and d["metadata"]["name"] == RATE_STORE
    ]

    assert store["spec"]["strategy"] == {"type": "Recreate"}
    assert store["spec"]["revisionHistoryLimit"] == 2


def test_one_value_sets_the_limit_of_every_deployment() -> None:
    found = deployments("revisionHistoryLimit=5")

    assert len(found) == len(SERVICES) + 1
    assert {d["spec"]["revisionHistoryLimit"] for d in found} == {5}


def test_a_limit_of_one_renders() -> None:
    found = deployments("revisionHistoryLimit=1")

    assert {d["spec"]["revisionHistoryLimit"] for d in found} == {1}


@pytest.mark.parametrize("limit", ["0", "-1", "1.5", "abc", "null", "''"])
def test_a_limit_below_one_or_not_a_whole_number_is_refused_by_name(
    limit: str,
) -> None:
    message = refusal(f"revisionHistoryLimit={limit}")

    assert "revisionHistoryLimit" in message
    assert "at least 1" in message


# ── the restarts' share of the margin ────────────────────────────────────────
def test_each_service_gets_its_place_in_the_sorted_list_over_the_count() -> None:
    ordered = sorted(SERVICES)

    found = shares()

    assert set(found) == set(SERVICES)
    for place, name in enumerate(ordered):
        # Helm prints a quotient with sixteen digits: 0.1666666666666667.
        assert share_read(found[name]) == pytest.approx(place / len(ordered), abs=1e-15)
    assert found[ordered[0]] == "0"


def test_the_shares_are_distinct_and_below_one() -> None:
    values = [share_read(text) for text in shares().values()]

    assert len(set(values)) == len(SERVICES)
    assert all(0 <= value < 1 for value in values)


def test_neither_the_rate_store_nor_a_job_is_given_a_share() -> None:
    for document in rendered_chart():
        template = document.get("spec", {}).get("template") or document.get(
            "spec", {}
        ).get("jobTemplate", {}).get("spec", {}).get("template")
        if not template or document["metadata"]["name"] in SERVICES:
            continue
        for container in template["spec"]["containers"]:
            names = [item["name"] for item in container.get("env", [])]
            assert RESTART_SHARE_ENV not in names, document["metadata"]["name"]


def test_every_service_is_a_place_when_one_is_added() -> None:
    # The count is the services' count, not a number written in the chart.
    arguments = [
        *helm_arguments(),
        "--set-json",
        'services.aaa-extra={"dbSecret":"claims-api-db","replicas":1,'
        '"command":["true"],"env":[],"resources":{"requests":{"cpu":"1m",'
        '"memory":"1Mi"},"limits":{"memory":"1Mi"}}}',
    ]
    found = [d for d in render(arguments) if d["kind"] == "Deployment"]

    values = {}
    for document in found:
        (container,) = document["spec"]["template"]["spec"]["containers"]
        for item in container.get("env", []):
            if item["name"] == RESTART_SHARE_ENV:
                values[document["metadata"]["name"]] = share_read(item["value"])

    assert len(values) == len(SERVICES) + 1
    assert values["aaa-extra"] == 0.0
    assert values["agent-runtime"] == pytest.approx(1 / (len(SERVICES) + 1), abs=1e-15)


def loaded_by(name: str, lifetime: timedelta) -> LoadedCertificate:
    """The certificate the service ``name`` loaded at ``START``, with the share
    the chart rendered for it, read the way ``certlife`` reads the variable."""
    return LoadedCertificate(
        not_before=START,
        not_after=START + lifetime,
        share=share_read(shares()[name]),
    )


@pytest.mark.parametrize(
    ("lifetime", "apart"),
    [
        (timedelta(hours=1), timedelta(seconds=100)),
        (timedelta(days=90), timedelta(hours=4)),
    ],
)
def test_the_rendered_shares_put_the_restarts_a_sixth_of_a_margin_apart(
    lifetime: timedelta, apart: timedelta
) -> None:
    ordered = sorted(SERVICES)

    times = [loaded_by(name, lifetime).restart_at for name in ordered]

    assert {earlier - later for earlier, later in pairwise(times)} == {apart}


@pytest.mark.parametrize(
    "lifetime", [timedelta(hours=1), timedelta(days=3), timedelta(days=90)]
)
def test_with_the_rendered_shares_every_service_restarts_after_the_default_renewal(
    lifetime: timedelta,
) -> None:
    renewal = START + lifetime - lifetime / 3  # cert-manager's default

    for name in SERVICES:
        assert loaded_by(name, lifetime).restart_at > renewal, name
