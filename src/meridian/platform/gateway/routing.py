"""The route decision: who may call, and which deployments may carry the call.

Pure and free of I/O. The residency and data-class filter runs here, once,
before any provider call; whatever walks the candidates afterwards sees only the
ones kept (T-44).

The data class of a request is the higher of its tenant's class and the class
the caller asks for in its request: a request can raise its class and never
lower it (T-11, T-13). Special-category data is refused outright, before any
deployment is looked at, so the refusal says why (``special-data``): no
deployment reaches that class today, and that is a decision, not a gap in the
registry (T-13).
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from meridian.platform.guardrails import higher_class
from meridian.platform.registry import Registry
from meridian.platform.registry.models import DataClass, Deployment, TenantLimits

RefusalReason = Literal[
    "unknown-tenant",
    "agent-not-allowed",
    "special-data",
    "no-route",
    "no-allowed-deployment",
]
SPECIAL: DataClass = "special"


@dataclass(frozen=True, slots=True)
class RouteDecision:
    data_class: str | None  # None only for an unknown tenant
    candidates: tuple[Deployment, ...]  # allowed, in the route's order
    refusal: RefusalReason | None  # set exactly when candidates is empty
    limits: TenantLimits | None = None  # the tenant's, whenever it is known


def deployment_allows(
    registry: Registry, deployment: Deployment, data_class_id: str
) -> bool:
    """The class exists in the registry, the deployment lists it, and the
    deployment's residency label is one the class may reach."""
    policy = registry.data_class(data_class_id)
    return (
        policy is not None
        and data_class_id in deployment.data_classes
        and deployment.residency in policy.residency
    )


def decide(
    registry: Registry,
    considered: Sequence[Deployment],
    tenant_id: str,
    agent_id: str,
    requested: DataClass | None = None,
) -> RouteDecision:
    """Keep, in order, the considered deployments the request's class may reach.

    The class is the tenant's, raised to ``requested`` when that is higher;
    ``None`` means the request names none. Replay passes its one deployment of
    the request's purpose, live mode that purpose's route candidates; an empty
    ``considered`` means the registry has no route.
    """
    tenant = registry.tenant(tenant_id)
    if tenant is None:
        return RouteDecision(None, (), "unknown-tenant")
    limits = tenant.limits
    data_class = (
        tenant.data_class
        if requested is None
        else higher_class(tenant.data_class, requested)
    )
    if not registry.tenant_may_run(tenant_id, agent_id):
        return RouteDecision(data_class, (), "agent-not-allowed", limits)
    if data_class == SPECIAL:
        return RouteDecision(data_class, (), "special-data", limits)
    if not considered:
        return RouteDecision(data_class, (), "no-route", limits)
    kept = tuple(d for d in considered if deployment_allows(registry, d, data_class))
    if not kept:
        return RouteDecision(data_class, (), "no-allowed-deployment", limits)
    return RouteDecision(data_class, kept, None, limits)
