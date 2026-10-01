"""The route decision: tenant, agent, then the residency and data-class filter."""

import pytest
from servicesupport import REGISTRY_DIR

from meridian.platform.gateway.routing import RouteDecision, decide, deployment_allows
from meridian.platform.registry import Registry, load_registry
from meridian.platform.registry.models import Deployment

PERSONAL_TENANT = "claims-triage"
SYNTHETIC_TENANT = "development"
AGENT = "claims-triage"


@pytest.fixture(scope="module")
def registry() -> Registry:
    return load_registry(REGISTRY_DIR)


@pytest.fixture(scope="module")
def eu_deployment(registry: Registry) -> Deployment:
    deployment = registry.deployment("aoai-sdc-gpt-4o")
    assert deployment is not None
    return deployment


@pytest.fixture(scope="module")
def global_deployment(eu_deployment: Deployment) -> Deployment:
    # The real registry has no global deployment; one is built in memory.
    return eu_deployment.model_copy(
        update={
            "id": "aoai-global-gpt-4o",
            "residency": "global",
            "data_classes": ("synthetic", "internal", "personal"),
        }
    )


# ── deployment_allows: three conditions, each with a refusing side ───────────
def test_a_class_the_deployment_lists_and_its_label_allows_is_allowed(
    registry: Registry, eu_deployment: Deployment
) -> None:
    assert deployment_allows(registry, eu_deployment, "personal")


def test_a_label_the_class_does_not_allow_is_refused(
    registry: Registry, global_deployment: Deployment
) -> None:
    assert not deployment_allows(registry, global_deployment, "personal")
    assert deployment_allows(registry, global_deployment, "synthetic")


def test_a_class_the_deployment_does_not_list_is_refused(
    registry: Registry, eu_deployment: Deployment
) -> None:
    only_synthetic = eu_deployment.model_copy(update={"data_classes": ("synthetic",)})

    assert not deployment_allows(registry, only_synthetic, "personal")
    assert deployment_allows(registry, only_synthetic, "synthetic")


def test_a_class_the_registry_does_not_know_is_refused(
    registry: Registry, eu_deployment: Deployment
) -> None:
    without_personal = registry.model_copy(
        update={
            "data_classes": tuple(
                c for c in registry.data_classes if c.id != "personal"
            )
        }
    )

    assert not deployment_allows(without_personal, eu_deployment, "personal")


def test_a_class_with_no_allowed_label_reaches_nothing(
    registry: Registry, eu_deployment: Deployment
) -> None:
    special = eu_deployment.model_copy(update={"data_classes": ("special",)})

    assert not deployment_allows(registry, special, "special")


# ── decide ───────────────────────────────────────────────────────────────────
def test_a_personal_tenant_never_gets_a_global_deployment_listed_first(
    registry: Registry, eu_deployment: Deployment, global_deployment: Deployment
) -> None:
    decision = decide(
        registry, (global_deployment, eu_deployment), PERSONAL_TENANT, AGENT
    )

    assert decision.data_class == "personal"
    assert decision.candidates == (eu_deployment,)
    assert decision.refusal is None


def test_a_synthetic_tenant_gets_the_global_deployment_first_in_order(
    registry: Registry, eu_deployment: Deployment, global_deployment: Deployment
) -> None:
    decision = decide(
        registry, (global_deployment, eu_deployment), SYNTHETIC_TENANT, AGENT
    )

    assert decision.data_class == "synthetic"
    assert decision.candidates == (global_deployment, eu_deployment)
    assert decision.refusal is None


def test_the_routes_order_is_kept(
    registry: Registry, eu_deployment: Deployment
) -> None:
    second = eu_deployment.model_copy(update={"id": "aoai-second"})
    third = eu_deployment.model_copy(update={"id": "aoai-third"})

    decision = decide(registry, (third, eu_deployment, second), SYNTHETIC_TENANT, AGENT)

    assert [d.id for d in decision.candidates] == [
        "aoai-third",
        "aoai-sdc-gpt-4o",
        "aoai-second",
    ]


def test_an_unknown_tenant_is_refused_with_no_data_class(
    registry: Registry, eu_deployment: Deployment
) -> None:
    decision = decide(registry, (eu_deployment,), "nobody", AGENT)

    assert decision == RouteDecision(None, (), "unknown-tenant")


def test_an_agent_the_tenant_does_not_list_is_refused(
    registry: Registry, eu_deployment: Deployment
) -> None:
    decision = decide(registry, (eu_deployment,), PERSONAL_TENANT, "other-agent")

    assert decision == RouteDecision("personal", (), "agent-not-allowed")


def test_no_considered_deployment_is_no_route(registry: Registry) -> None:
    decision = decide(registry, (), PERSONAL_TENANT, AGENT)

    assert decision == RouteDecision("personal", (), "no-route")


def test_a_class_no_label_allows_is_no_allowed_deployment(
    registry: Registry, eu_deployment: Deployment
) -> None:
    special_tenant = registry.tenants[0].model_copy(
        update={"id": "special-tenant", "data_class": "special"}
    )
    with_special = registry.model_copy(
        update={"tenants": (*registry.tenants, special_tenant)}
    )
    listed = eu_deployment.model_copy(update={"data_classes": ("special",)})

    decision = decide(with_special, (listed,), "special-tenant", AGENT)

    assert decision == RouteDecision("special", (), "no-allowed-deployment")


def test_only_global_candidates_for_a_personal_tenant_is_no_allowed_deployment(
    registry: Registry, global_deployment: Deployment
) -> None:
    decision = decide(registry, (global_deployment,), PERSONAL_TENANT, AGENT)

    assert decision == RouteDecision("personal", (), "no-allowed-deployment")


@pytest.mark.parametrize(
    ("tenant", "agent", "use_candidates"),
    [
        (PERSONAL_TENANT, AGENT, True),
        (PERSONAL_TENANT, AGENT, False),
        ("nobody", AGENT, True),
        (PERSONAL_TENANT, "other-agent", True),
        (SYNTHETIC_TENANT, AGENT, True),
    ],
)
def test_a_refusal_is_set_exactly_when_there_are_no_candidates(
    registry: Registry,
    eu_deployment: Deployment,
    tenant: str,
    agent: str,
    use_candidates: bool,
) -> None:
    considered = (eu_deployment,) if use_candidates else ()

    decision = decide(registry, considered, tenant, agent)

    assert (decision.refusal is None) == bool(decision.candidates)
