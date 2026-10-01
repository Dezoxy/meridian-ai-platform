"""The route decision: who may call, and which deployments may carry the call.

Pure and free of I/O. The residency and data-class filter runs here, once,
before any provider call; whatever walks the candidates afterwards sees only the
ones kept (T-44). The data class of a request is its tenant's class (a
per-request class is S014).
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from meridian.platform.registry import Registry
from meridian.platform.registry.models import Deployment

RefusalReason = Literal[
    "unknown-tenant", "agent-not-allowed", "no-route", "no-allowed-deployment"
]


@dataclass(frozen=True, slots=True)
class RouteDecision:
    data_class: str | None  # None only for an unknown tenant
    candidates: tuple[Deployment, ...]  # allowed, in the route's order
    refusal: RefusalReason | None  # set exactly when candidates is empty


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
) -> RouteDecision:
    """Keep, in order, the considered deployments the tenant's class may reach.

    Replay passes its one deployment, live mode the chat route's candidates;
    an empty ``considered`` means the registry has no route.
    """
    tenant = registry.tenant(tenant_id)
    if tenant is None:
        return RouteDecision(None, (), "unknown-tenant")
    data_class = tenant.data_class
    if not registry.tenant_may_run(tenant_id, agent_id):
        return RouteDecision(data_class, (), "agent-not-allowed")
    if not considered:
        return RouteDecision(data_class, (), "no-route")
    kept = tuple(d for d in considered if deployment_allows(registry, d, data_class))
    if not kept:
        return RouteDecision(data_class, (), "no-allowed-deployment")
    return RouteDecision(data_class, kept, None)
