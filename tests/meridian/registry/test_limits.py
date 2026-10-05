"""Tenant limits, the exchange rate and the deployments' own rate limits (S011)."""

import copy
import json
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from registrysupport import add_field, apply_changes, remove_field, set_field

from meridian.platform.registry import compare_with_terraform, load_registry
from meridian.platform.registry.models import Registry

Plant = Callable[..., Path]
LoadErrors = Callable[[Path], tuple[str, ...]]
Edit = tuple[str, str, str]

GPT4O = "sdc/gpt-4o"
# The deployments these tests edit, found by their keys (registrysupport).
FIRST_AZURE = "aoai-sdc-gpt-4o"
SECOND_AZURE = "aoai-sdc-gpt-4o-b"
RATE_LIMITS = "{requests_per_10_seconds: 20, tokens_per_minute: 20000}"
CLAIMS_LIMITS = (
    "    limits:\n"
    "      requests_per_10_seconds: 10\n"
    "      tokens_per_minute: 10000\n"
    "      tokens_per_day: 300000\n"
    "      cost_per_month_eur: 10\n"
)
EXCHANGE = (
    "exchange:\n"
    "  usd_per_eur: 1.1355\n"
    "  source: ECB euro foreign exchange reference rate\n"
    "  checked: 2026-09-30\n"
)
LIMIT_FIELDS = (
    "requests_per_10_seconds",
    "tokens_per_minute",
    "tokens_per_day",
    "cost_per_month_eur",
)


def raise_limit(field: str, old: str, new: str) -> Edit:
    """Change the first tenant's (claims-triage) value of one limit."""
    return ("tenants.yaml", f"      {field}: {old}\n", f"      {field}: {new}\n")


@pytest.fixture
def registry(real_registry: Path) -> Registry:
    return load_registry(real_registry)


@pytest.fixture
def outputs(snapshot_path: Path) -> dict[str, Any]:
    return json.loads(snapshot_path.read_text(encoding="utf-8"))


# ── the real registry ───────────────────────────────────────────────────────
def test_every_seeded_tenant_has_the_four_limits(registry: Registry) -> None:
    claims = registry.tenants[0].limits

    assert (
        claims.requests_per_10_seconds,
        claims.tokens_per_minute,
        claims.tokens_per_day,
        claims.cost_per_month_eur,
    ) == (10, 10_000, 300_000, Decimal(10))
    assert [t.limits.requests_per_10_seconds for t in registry.tenants] == [10, 6, 4]
    assert [t.limits.cost_per_month_eur for t in registry.tenants] == [10, 5, 2]


def test_the_exchange_rate_loads_exactly(registry: Registry) -> None:
    assert registry.exchange.usd_per_eur == Decimal("1.1355")
    assert registry.exchange.source
    assert str(registry.exchange.checked) == "2026-09-30"


def test_azure_deployments_carry_their_rate_limits_and_replay_does_not(
    registry: Registry,
) -> None:
    by_id = {d.id: d for d in registry.deployments}

    for name in (
        "aoai-sdc-gpt-4o",
        "aoai-sdc-gpt-4o-b",
        "aoai-sdc-text-embedding-3-large",
    ):
        limits = by_id[name].rate_limits
        assert limits is not None, name
        assert (limits.requests_per_10_seconds, limits.tokens_per_minute) == (
            20,
            20_000,
        )
    assert by_id["replay-chat"].rate_limits is None
    assert by_id["replay-embedding"].rate_limits is None


# ── the shape: a tenant needs limits, the file needs an exchange rate ───────
def test_a_tenant_without_limits_fails_to_load(
    plant: Plant, load_errors: LoadErrors
) -> None:
    errors = load_errors(plant(("tenants.yaml", CLAIMS_LIMITS, "")))

    assert errors == ("tenants.yaml: tenants[0].limits: Field required",)


@pytest.mark.parametrize("field", LIMIT_FIELDS)
def test_a_tenant_limit_missing_is_reported_at_its_path(
    plant: Plant, load_errors: LoadErrors, field: str
) -> None:
    line = next(ln for ln in CLAIMS_LIMITS.splitlines(True) if field in ln)

    errors = load_errors(plant(("tenants.yaml", line, "")))

    assert errors == (f"tenants.yaml: tenants[0].limits.{field}: Field required",)


@pytest.mark.parametrize("bad", ["0", "-1"])
@pytest.mark.parametrize(
    ("field", "seeded"),
    [
        ("requests_per_10_seconds", "10"),
        ("tokens_per_minute", "10000"),
        ("tokens_per_day", "300000"),
        ("cost_per_month_eur", "10"),
    ],
)
def test_a_limit_of_zero_or_below_fails_to_load(
    plant: Plant, load_errors: LoadErrors, field: str, seeded: str, bad: str
) -> None:
    errors = load_errors(plant(raise_limit(field, seeded, bad)))

    assert len(errors) == 1
    assert errors[0].startswith(f"tenants.yaml: tenants[0].limits.{field}: ")


def test_a_limit_of_one_is_accepted(plant: Plant) -> None:
    directory = plant(raise_limit("tokens_per_day", "300000", "1"))

    assert load_registry(directory).tenants[0].limits.tokens_per_day == 1


def test_a_monthly_cost_of_six_decimals_is_accepted_exactly(plant: Plant) -> None:
    directory = plant(raise_limit("cost_per_month_eur", "10", "0.000001"))

    limits = load_registry(directory).tenants[0].limits

    assert limits.cost_per_month_eur == Decimal("0.000001")


def test_a_monthly_cost_of_seven_decimals_fails_to_load(
    plant: Plant, load_errors: LoadErrors
) -> None:
    errors = load_errors(plant(raise_limit("cost_per_month_eur", "10", "0.0000001")))

    assert len(errors) == 1
    assert errors[0].startswith("tenants.yaml: tenants[0].limits.cost_per_month_eur: ")
    assert "decimal places" in errors[0]


def test_an_unknown_limit_key_fails_to_load(
    plant: Plant, load_errors: LoadErrors
) -> None:
    errors = load_errors(
        plant(("tenants.yaml", CLAIMS_LIMITS, CLAIMS_LIMITS + "      burst: 5\n"))
    )

    assert errors == (
        "tenants.yaml: tenants[0].limits.burst: Extra inputs are not permitted",
    )


def test_a_missing_exchange_fails_to_load(
    plant: Plant, load_errors: LoadErrors
) -> None:
    errors = load_errors(plant(("tenants.yaml", EXCHANGE, "")))

    assert errors == ("tenants.yaml: exchange: Field required",)


@pytest.mark.parametrize("rate", ["0", "-1.1"])
def test_an_exchange_rate_of_zero_or_below_fails_to_load(
    plant: Plant, load_errors: LoadErrors, rate: str
) -> None:
    errors = load_errors(
        plant(("tenants.yaml", "usd_per_eur: 1.1355", f"usd_per_eur: {rate}"))
    )

    assert len(errors) == 1
    assert errors[0].startswith("tenants.yaml: exchange.usd_per_eur: ")


# ── deployments: Azure has rate limits, replay has none ─────────────────────
def test_an_azure_deployment_without_rate_limits_is_one_message(
    plant: Plant, load_errors: LoadErrors
) -> None:
    errors = load_errors(
        apply_changes(plant(), remove_field(FIRST_AZURE, "rate_limits"))
    )

    assert errors == (
        "models.yaml: deployments[0].rate_limits: required for provider kind "
        "'azure-openai' (deployment 'aoai-sdc-gpt-4o')",
    )


def test_a_replay_deployment_with_rate_limits_is_one_message(
    plant: Plant, load_errors: LoadErrors
) -> None:
    errors = load_errors(
        apply_changes(plant(), add_field("replay-chat", "rate_limits", RATE_LIMITS))
    )

    assert errors == (
        "models.yaml: deployments[3].rate_limits: must not be set for provider "
        "kind 'replay' (deployment 'replay-chat')",
    )


@pytest.mark.parametrize("field", ["requests_per_10_seconds", "tokens_per_minute"])
def test_a_deployment_rate_limit_of_zero_fails_to_load(
    plant: Plant, load_errors: LoadErrors, field: str
) -> None:
    zero = set_field(FIRST_AZURE, f"rate_limits.{field}", "0")

    errors = load_errors(apply_changes(plant(), zero))

    assert len(errors) == 1
    assert errors[0].startswith(f"models.yaml: deployments[0].rate_limits.{field}: ")


# ── the tenants' rate limits must fit the smallest candidate of every route ─
# (S045, T-55: the embedding route's candidate is compared like the chat ones)
def test_tenant_sums_exactly_at_the_candidates_limits_give_no_message(
    registry: Registry,
) -> None:
    # The seeded sums are 20 requests and 20,000 tokens against 20 and 20,000.
    assert sum(t.limits.requests_per_10_seconds for t in registry.tenants) == 20
    assert sum(t.limits.tokens_per_minute for t in registry.tenants) == 20_000


def test_tenant_requests_one_above_the_candidate_are_one_message(
    plant: Plant, load_errors: LoadErrors
) -> None:
    errors = load_errors(plant(raise_limit("requests_per_10_seconds", "10", "11")))

    assert errors == (
        "policies.yaml: routes[0].candidates[0]: deployment 'aoai-sdc-gpt-4o' "
        "allows 20 requests_per_10_seconds but the tenants' limits add up to 21",
        "policies.yaml: routes[0].candidates[1]: deployment 'aoai-sdc-gpt-4o-b' "
        "allows 20 requests_per_10_seconds but the tenants' limits add up to 21",
        "policies.yaml: routes[1].candidates[0]: deployment "
        "'aoai-sdc-text-embedding-3-large' allows 20 requests_per_10_seconds but "
        "the tenants' limits add up to 21",
    )


def test_tenant_tokens_one_above_the_candidate_are_one_message_per_candidate(
    plant: Plant, load_errors: LoadErrors
) -> None:
    errors = load_errors(plant(raise_limit("tokens_per_minute", "10000", "10001")))

    assert errors == (
        "policies.yaml: routes[0].candidates[0]: deployment 'aoai-sdc-gpt-4o' "
        "allows 20000 tokens_per_minute but the tenants' limits add up to 20001",
        "policies.yaml: routes[0].candidates[1]: deployment 'aoai-sdc-gpt-4o-b' "
        "allows 20000 tokens_per_minute but the tenants' limits add up to 20001",
        "policies.yaml: routes[1].candidates[0]: deployment "
        "'aoai-sdc-text-embedding-3-large' allows 20000 tokens_per_minute but "
        "the tenants' limits add up to 20001",
    )


def test_both_fields_over_give_one_message_per_field_and_candidate(
    plant: Plant, load_errors: LoadErrors
) -> None:
    errors = load_errors(
        plant(
            raise_limit("requests_per_10_seconds", "10", "11"),
            raise_limit("tokens_per_minute", "10000", "10001"),
        )
    )

    assert len(errors) == 6  # two chat candidates and one embedding candidate


def test_only_the_candidate_with_the_smaller_limit_is_named(
    plant: Plant, load_errors: LoadErrors
) -> None:
    smaller = set_field(SECOND_AZURE, "rate_limits.requests_per_10_seconds", "19")

    errors = load_errors(apply_changes(plant(), smaller))

    assert errors == (
        "policies.yaml: routes[0].candidates[1]: deployment 'aoai-sdc-gpt-4o-b' "
        "allows 19 requests_per_10_seconds but the tenants' limits add up to 20",
    )


def test_a_candidate_without_rate_limits_is_not_compared(
    plant: Plant, load_errors: LoadErrors
) -> None:
    # Only the missing-field message; the sum check has nothing to compare.
    directory = plant(raise_limit("requests_per_10_seconds", "10", "11"))
    errors = load_errors(
        apply_changes(directory, remove_field(FIRST_AZURE, "rate_limits"))
    )

    assert [e for e in errors if "add up to" in e] == [
        "policies.yaml: routes[0].candidates[1]: deployment 'aoai-sdc-gpt-4o-b' "
        "allows 20 requests_per_10_seconds but the tenants' limits add up to 21",
        "policies.yaml: routes[1].candidates[0]: deployment "
        "'aoai-sdc-text-embedding-3-large' allows 20 requests_per_10_seconds but "
        "the tenants' limits add up to 21",
    ]
    assert len(errors) == 3


def test_the_sum_covers_every_tenant_not_only_those_that_use_the_route(
    plant: Plant, load_errors: LoadErrors
) -> None:
    # The development tenant is the third one; its 4 requests are in the sum.
    errors = load_errors(
        plant(
            (
                "tenants.yaml",
                "requests_per_10_seconds: 4\n",
                "requests_per_10_seconds: 5\n",
            )
        )
    )

    assert len(errors) == 3  # both chat candidates and the embedding one
    assert all("add up to 21" in e for e in errors)


# ── Terraform: tokens_per_minute is capacity x 1000 ─────────────────────────
def test_the_snapshot_capacity_matches_the_registry_rate_limits(
    registry: Registry, outputs: dict[str, Any]
) -> None:
    assert {entry["capacity"] for entry in outputs.values()} == {20}
    assert compare_with_terraform(registry, outputs) == ()


def test_a_capacity_that_does_not_give_the_registered_tokens_is_reported(
    registry: Registry, outputs: dict[str, Any]
) -> None:
    changed = copy.deepcopy(outputs)
    changed[GPT4O]["capacity"] = 30

    messages = compare_with_terraform(registry, changed)

    assert messages == (
        "models.yaml: deployment 'aoai-sdc-gpt-4o': rate_limits.tokens_per_minute "
        "is 20000 in the registry but 30000 in Terraform (capacity 30 x 1000; "
        "key 'sdc/gpt-4o')",
    )


def test_a_registered_tokens_per_minute_that_is_not_capacity_times_1000_is_reported(
    registry: Registry, outputs: dict[str, Any]
) -> None:
    edited = tuple(
        d.model_copy(
            update={
                "rate_limits": d.rate_limits.model_copy(
                    update={"tokens_per_minute": 20_001}
                )
            }
        )
        if d.id == "aoai-sdc-gpt-4o" and d.rate_limits
        else d
        for d in registry.deployments
    )

    messages = compare_with_terraform(
        registry.model_copy(update={"deployments": edited}), outputs
    )

    assert len(messages) == 1
    assert "is 20001 in the registry but 20000 in Terraform" in messages[0]


def test_an_output_entry_without_capacity_is_not_compared(
    registry: Registry, outputs: dict[str, Any]
) -> None:
    changed = copy.deepcopy(outputs)
    del changed[GPT4O]["capacity"]

    assert compare_with_terraform(registry, changed) == ()


def test_a_capacity_that_is_not_a_whole_number_is_reported(
    registry: Registry, outputs: dict[str, Any]
) -> None:
    changed = copy.deepcopy(outputs)
    changed[GPT4O]["capacity"] = "twenty"

    messages = compare_with_terraform(registry, changed)

    assert messages == (
        "models.yaml: deployment 'aoai-sdc-gpt-4o': capacity is 'twenty' in "
        "Terraform, not a whole number (key 'sdc/gpt-4o')",
    )
