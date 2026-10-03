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

A request that carries a response schema (S051) is refused unless its agent
declares ``structured_outputs`` in the registry (``schema-not-allowed``), and
it may use only the deployments that declare it too: after the class filter, a
deployment that cannot honour a schema is left out before anything is called,
as a deployment the class may not reach is (T-44), so a fallback never answers
a request that asked for a shape with free text. When the class filter keeps
deployments and none declares it, the refusal is ``no-schema-deployment``. A
request without a schema is decided exactly as before.
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
    "schema-not-allowed",
    "no-route",
    "no-allowed-deployment",
    "no-schema-deployment",
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


def _agent_declares_schema(registry: Registry, agent_id: str) -> bool:
    """The agent is in the registry and declares ``structured_outputs``."""
    agent = registry.agent(agent_id)
    return agent is not None and agent.structured_outputs


def decide(
    registry: Registry,
    considered: Sequence[Deployment],
    tenant_id: str,
    agent_id: str,
    requested: DataClass | None = None,
    *,
    wants_schema: bool = False,
) -> RouteDecision:
    """Keep, in order, the considered deployments the request's class may reach.

    The class is the tenant's, raised to ``requested`` when that is higher;
    ``None`` means the request names none. Replay passes its one deployment of
    the request's purpose, live mode that purpose's route candidates; an empty
    ``considered`` means the registry has no route. ``wants_schema`` is true for
    a chat request that carries a response schema: the agent must declare it,
    and ``candidates`` holds only deployments that honour one, in the route's
    order.
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
    if wants_schema and not _agent_declares_schema(registry, agent_id):
        return RouteDecision(data_class, (), "schema-not-allowed", limits)
    if not considered:
        return RouteDecision(data_class, (), "no-route", limits)
    kept = tuple(d for d in considered if deployment_allows(registry, d, data_class))
    if not kept:
        return RouteDecision(data_class, (), "no-allowed-deployment", limits)
    if wants_schema:
        kept = tuple(d for d in kept if d.structured_outputs)
        if not kept:
            return RouteDecision(data_class, (), "no-schema-deployment", limits)
    return RouteDecision(data_class, kept, None, limits)
