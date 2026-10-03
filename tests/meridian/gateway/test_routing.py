"""The route decision: tenant, agent, then the residency and data-class filter."""

import pytest
from servicesupport import REGISTRY_DIR

from meridian.platform.gateway.routing import RouteDecision, decide, deployment_allows
from meridian.platform.guardrails import DATA_CLASS_ORDER
from meridian.platform.registry import Registry, load_registry
from meridian.platform.registry.models import DataClass, Deployment, TenantLimits

PERSONAL_TENANT = "claims-triage"
SYNTHETIC_TENANT = "development"
CLASS_TENANT = "class-tenant"
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
def limits_of(registry: Registry, tenant_id: str) -> TenantLimits:
    tenant = registry.tenant(tenant_id)
    assert tenant is not None
    return tenant.limits


def test_every_decision_for_a_known_tenant_carries_its_limits(
    registry: Registry, eu_deployment: Deployment
) -> None:
    expected = limits_of(registry, PERSONAL_TENANT)

    allowed = decide(registry, (eu_deployment,), PERSONAL_TENANT, AGENT)
    no_agent = decide(registry, (eu_deployment,), PERSONAL_TENANT, "other-agent")
    no_route = decide(registry, (), PERSONAL_TENANT, AGENT)

    assert allowed.limits == no_agent.limits == no_route.limits == expected


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

    assert decision == RouteDecision(
        "personal", (), "agent-not-allowed", limits_of(registry, PERSONAL_TENANT)
    )


def test_no_considered_deployment_is_no_route(registry: Registry) -> None:
    decision = decide(registry, (), PERSONAL_TENANT, AGENT)

    assert decision == RouteDecision(
        "personal", (), "no-route", limits_of(registry, PERSONAL_TENANT)
    )


def with_tenant_class(registry: Registry, data_class: DataClass) -> Registry:
    """The registry plus a tenant, ``class-tenant``, of ``data_class``."""
    tenant = registry.tenants[0].model_copy(
        update={"id": CLASS_TENANT, "data_class": data_class}
    )
    return registry.model_copy(update={"tenants": (*registry.tenants, tenant)})


def test_a_special_tenant_is_special_data_even_where_a_deployment_lists_special(
    registry: Registry, eu_deployment: Deployment
) -> None:
    with_special = with_tenant_class(registry, "special")
    listed = eu_deployment.model_copy(update={"data_classes": ("special",)})

    decision = decide(with_special, (listed,), CLASS_TENANT, AGENT)

    assert decision == RouteDecision(
        "special", (), "special-data", limits_of(with_special, CLASS_TENANT)
    )


# ── a request's own class: a header may raise the tenant's class, never lower it
@pytest.mark.parametrize("requested", [None, *DATA_CLASS_ORDER])
@pytest.mark.parametrize("tenant_class", DATA_CLASS_ORDER)
def test_the_class_used_is_the_higher_of_the_tenants_and_the_requests(
    registry: Registry,
    eu_deployment: Deployment,
    tenant_class: DataClass,
    requested: DataClass | None,
) -> None:
    with_class = with_tenant_class(registry, tenant_class)
    expected = max(
        (tenant_class, requested or tenant_class), key=DATA_CLASS_ORDER.index
    )

    decision = decide(with_class, (eu_deployment,), CLASS_TENANT, AGENT, requested)

    assert decision.data_class == expected
    if expected == "special":
        assert (decision.candidates, decision.refusal) == ((), "special-data")
    else:
        assert decision.candidates == (eu_deployment,)
        assert decision.refusal is None


def test_a_lower_request_class_is_not_an_error_and_the_tenants_class_is_used(
    registry: Registry, eu_deployment: Deployment
) -> None:
    decision = decide(
        registry, (eu_deployment,), PERSONAL_TENANT, AGENT, requested="synthetic"
    )

    assert decision.data_class == "personal"
    assert decision.refusal is None


def test_no_request_class_is_the_tenants_class(
    registry: Registry, eu_deployment: Deployment
) -> None:
    assert decide(registry, (eu_deployment,), PERSONAL_TENANT, AGENT, None) == decide(
        registry, (eu_deployment,), PERSONAL_TENANT, AGENT
    )


def test_a_synthetic_tenant_that_sends_personal_is_filtered_as_personal(
    registry: Registry, eu_deployment: Deployment, global_deployment: Deployment
) -> None:
    decision = decide(
        registry,
        (global_deployment, eu_deployment),
        SYNTHETIC_TENANT,
        AGENT,
        requested="personal",
    )

    assert decision.data_class == "personal"
    assert decision.candidates == (eu_deployment,)  # the global one is skipped


def test_a_synthetic_tenant_that_sends_personal_to_a_global_route_is_refused(
    registry: Registry, global_deployment: Deployment
) -> None:
    decision = decide(
        registry, (global_deployment,), SYNTHETIC_TENANT, AGENT, requested="personal"
    )

    assert (decision.data_class, decision.refusal) == (
        "personal",
        "no-allowed-deployment",
    )


def test_a_special_request_from_a_known_tenant_is_special_data_before_the_filter(
    registry: Registry, eu_deployment: Deployment
) -> None:
    decision = decide(
        registry, (eu_deployment,), SYNTHETIC_TENANT, AGENT, requested="special"
    )

    assert decision == RouteDecision(
        "special", (), "special-data", limits_of(registry, SYNTHETIC_TENANT)
    )


def test_who_is_refused_comes_before_special_data(
    registry: Registry, eu_deployment: Deployment
) -> None:
    unknown = decide(registry, (eu_deployment,), "nobody", AGENT, "special")
    other_agent = decide(
        registry, (eu_deployment,), PERSONAL_TENANT, "other-agent", "special"
    )

    assert unknown == RouteDecision(None, (), "unknown-tenant")
    # The class used is still said, so the audit row shows what was asked.
    assert (other_agent.data_class, other_agent.refusal) == (
        "special",
        "agent-not-allowed",
    )


def test_only_global_candidates_for_a_personal_tenant_is_no_allowed_deployment(
    registry: Registry, global_deployment: Deployment
) -> None:
    decision = decide(registry, (global_deployment,), PERSONAL_TENANT, AGENT)

    assert decision == RouteDecision(
        "personal", (), "no-allowed-deployment", limits_of(registry, PERSONAL_TENANT)
    )


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


# ── a request with a response schema (S051) ──────────────────────────────────
def plain(deployment: Deployment, deployment_id: str | None = None) -> Deployment:
    """The deployment without the declaration: it cannot honour a schema."""
    return deployment.model_copy(
        update={
            "id": deployment_id or deployment.id,
            "structured_outputs": False,
        }
    )


def honouring(deployment: Deployment, deployment_id: str) -> Deployment:
    return deployment.model_copy(
        update={"id": deployment_id, "structured_outputs": True}
    )


def with_agent_declaring(registry: Registry, declares: bool) -> Registry:
    """The registry with the agent's declaration set to ``declares``."""
    agents = tuple(
        a.model_copy(update={"structured_outputs": declares}) if a.id == AGENT else a
        for a in registry.agents
    )
    return registry.model_copy(update={"agents": agents})


def without_agent(registry: Registry) -> Registry:
    """The agent is gone from the registry; the tenants still list it."""
    agents = tuple(a for a in registry.agents if a.id != AGENT)
    return registry.model_copy(update={"agents": agents})


def test_the_real_registry_declares_the_triage_agent_and_its_deployment(
    registry: Registry, eu_deployment: Deployment
) -> None:
    agent = registry.agent(AGENT)

    assert agent is not None
    assert agent.structured_outputs
    assert eu_deployment.structured_outputs


def test_a_schema_request_from_a_declaring_agent_keeps_the_honouring_candidates(
    registry: Registry, eu_deployment: Deployment
) -> None:
    decision = decide(
        registry, (eu_deployment,), PERSONAL_TENANT, AGENT, wants_schema=True
    )

    assert decision == RouteDecision(
        "personal", (eu_deployment,), None, limits_of(registry, PERSONAL_TENANT)
    )


def test_an_agent_that_does_not_declare_is_refused(
    registry: Registry, eu_deployment: Deployment
) -> None:
    changed = with_agent_declaring(registry, False)

    decision = decide(
        changed, (eu_deployment,), PERSONAL_TENANT, AGENT, wants_schema=True
    )

    assert decision == RouteDecision(
        "personal", (), "schema-not-allowed", limits_of(changed, PERSONAL_TENANT)
    )


def test_an_agent_missing_from_the_registry_is_refused(
    registry: Registry, eu_deployment: Deployment
) -> None:
    changed = without_agent(registry)

    decision = decide(
        changed, (eu_deployment,), PERSONAL_TENANT, AGENT, wants_schema=True
    )

    assert decision == RouteDecision(
        "personal", (), "schema-not-allowed", limits_of(changed, PERSONAL_TENANT)
    )


def test_a_non_declaring_agent_is_refused_even_when_no_deployment_is_listed(
    registry: Registry,
) -> None:
    changed = with_agent_declaring(registry, False)

    decision = decide(changed, (), PERSONAL_TENANT, AGENT, wants_schema=True)

    assert decision.refusal == "schema-not-allowed"


def test_a_declaring_agent_with_no_route_is_no_route(registry: Registry) -> None:
    decision = decide(registry, (), PERSONAL_TENANT, AGENT, wants_schema=True)

    assert decision.refusal == "no-route"


def test_the_two_new_refusals_come_after_who_and_what_class(
    registry: Registry, eu_deployment: Deployment
) -> None:
    undeclared = with_agent_declaring(registry, False)

    unknown = decide(
        undeclared, (eu_deployment,), "nobody", AGENT, "special", wants_schema=True
    )
    other = decide(
        undeclared,
        (eu_deployment,),
        PERSONAL_TENANT,
        "other-agent",
        "special",
        wants_schema=True,
    )
    special = decide(
        undeclared,
        (eu_deployment,),
        PERSONAL_TENANT,
        AGENT,
        "special",
        wants_schema=True,
    )

    assert unknown.refusal == "unknown-tenant"
    assert other.refusal == "agent-not-allowed"
    assert special.refusal == "special-data"


def test_a_missing_declaration_is_refused_before_the_class_filter(
    registry: Registry, global_deployment: Deployment
) -> None:
    undeclared = with_agent_declaring(registry, False)

    decision = decide(
        undeclared, (global_deployment,), PERSONAL_TENANT, AGENT, wants_schema=True
    )

    assert decision.refusal == "schema-not-allowed"  # not no-allowed-deployment


def test_the_class_filter_refuses_before_the_schema_filter(
    registry: Registry, global_deployment: Deployment
) -> None:
    decision = decide(
        registry, (global_deployment,), PERSONAL_TENANT, AGENT, wants_schema=True
    )

    assert decision.refusal == "no-allowed-deployment"  # not no-schema-deployment


def test_no_honouring_candidate_is_no_schema_deployment(
    registry: Registry, eu_deployment: Deployment
) -> None:
    candidates = (plain(eu_deployment), plain(eu_deployment, "aoai-second"))

    decision = decide(registry, candidates, PERSONAL_TENANT, AGENT, wants_schema=True)

    assert decision == RouteDecision(
        "personal", (), "no-schema-deployment", limits_of(registry, PERSONAL_TENANT)
    )


def test_a_candidate_the_class_filter_dropped_does_not_count_as_honouring(
    registry: Registry, eu_deployment: Deployment, global_deployment: Deployment
) -> None:
    # The global one honours a schema, but a personal request cannot reach it.
    assert global_deployment.structured_outputs
    candidates = (global_deployment, plain(eu_deployment))

    decision = decide(registry, candidates, PERSONAL_TENANT, AGENT, wants_schema=True)

    assert decision.refusal == "no-schema-deployment"


def test_a_candidate_that_cannot_honour_a_schema_is_left_out_in_the_routes_order(
    registry: Registry, eu_deployment: Deployment
) -> None:
    first = plain(eu_deployment, "aoai-first")
    second = honouring(eu_deployment, "aoai-second")
    third = plain(eu_deployment, "aoai-third")
    fourth = honouring(eu_deployment, "aoai-fourth")

    decision = decide(
        registry,
        (first, fourth, second, third),
        SYNTHETIC_TENANT,
        AGENT,
        wants_schema=True,
    )

    assert [d.id for d in decision.candidates] == ["aoai-fourth", "aoai-second"]
    assert decision.refusal is None


def test_a_global_deployment_that_honours_a_schema_stays_ahead_for_synthetic_data(
    registry: Registry, eu_deployment: Deployment, global_deployment: Deployment
) -> None:
    decision = decide(
        registry,
        (global_deployment, eu_deployment),
        SYNTHETIC_TENANT,
        AGENT,
        wants_schema=True,
    )

    assert decision.candidates == (global_deployment, eu_deployment)


def test_without_a_schema_nothing_changes_for_any_agent_or_deployment(
    registry: Registry, eu_deployment: Deployment
) -> None:
    undeclared = with_agent_declaring(registry, False)
    candidates = (plain(eu_deployment, "aoai-first"), eu_deployment)

    default = decide(undeclared, candidates, PERSONAL_TENANT, AGENT)
    explicit = decide(undeclared, candidates, PERSONAL_TENANT, AGENT, None)
    said = decide(undeclared, candidates, PERSONAL_TENANT, AGENT, wants_schema=False)

    assert default == explicit == said
    assert default.candidates == candidates
    assert default.refusal is None


def test_the_schema_flag_is_keyword_only(
    registry: Registry, eu_deployment: Deployment
) -> None:
    with pytest.raises(TypeError):
        decide(registry, (eu_deployment,), PERSONAL_TENANT, AGENT, None, True)  # type: ignore[misc]


@pytest.mark.parametrize("declares", [True, False])
@pytest.mark.parametrize("kind", ["none", "plain", "honouring"])
def test_a_refusal_with_a_schema_is_set_exactly_when_there_are_no_candidates(
    registry: Registry, eu_deployment: Deployment, declares: bool, kind: str
) -> None:
    candidates = {
        "none": (),
        "plain": (plain(eu_deployment),),
        "honouring": (eu_deployment,),
    }[kind]

    decision = decide(
        with_agent_declaring(registry, declares),
        candidates,
        PERSONAL_TENANT,
        AGENT,
        wants_schema=True,
    )

    assert (decision.refusal is None) == bool(decision.candidates)
