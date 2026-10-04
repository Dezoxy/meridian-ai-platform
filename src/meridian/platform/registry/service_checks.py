"""The cross-file checks of ``services.yaml`` (S055), apart from ``checks.py``,
which imports ``SERVICE_CHECKS`` into its own list; this module imports only
the models. Messages read ``file: path: problem`` like the others.
"""

from collections import Counter
from collections.abc import Callable, Iterable

from meridian.platform.registry.models import Registry, Service

SERVICES = "services.yaml"
TOOLS = "tools.yaml"
# The one service that calls a tool server (T-22: a tool server binds every call
# to a run, and only the runtime has runs).
RUNTIME_SERVICE = "agent-runtime"


def _repeated(values: Iterable[str]) -> list[str]:
    return [value for value, count in Counter(values).items() if count > 1]


def check_service_ids(registry: Registry) -> list[str]:
    return [
        f"{SERVICES}: services: duplicate id {value!r}"
        for value in _repeated(s.id for s in registry.services)
    ]


def _call_errors(registry: Registry, i: int, service: Service) -> list[str]:
    where = f"{SERVICES}: services[{i}].calls"
    servers = {s.id for s in registry.servers}
    errors = [
        f"{where}: service {value!r} is listed twice"
        for value in _repeated(service.calls)
    ]
    for j, callee in enumerate(service.calls):
        if callee == service.id:
            errors.append(
                f"{where}[{j}]: a service cannot call itself ({service.id!r})"
            )
        elif registry.service(callee) is None:
            errors.append(f"{where}[{j}]: unknown service {callee!r}")
        elif callee in servers and service.id != RUNTIME_SERVICE:
            errors.append(
                f"{where}[{j}]: only {RUNTIME_SERVICE!r} may call the tool "
                f"server {callee!r}"
            )
    return errors


def check_service_calls(registry: Registry) -> list[str]:
    return [
        error
        for i, service in enumerate(registry.services)
        for error in _call_errors(registry, i, service)
    ]


def _name_errors(registry: Registry, i: int, service: Service) -> list[str]:
    where = f"{SERVICES}: services[{i}]"
    errors: list[str] = []
    for field, values, exists, kind in (
        ("tenants", service.tenants, registry.tenant, "tenant"),
        ("agents", service.agents, registry.agent, "agent"),
    ):
        errors += [
            f"{where}.{field}: {kind} {value!r} is listed twice"
            for value in _repeated(values)
        ]
        errors += [
            f"{where}.{field}[{j}]: unknown {kind} {value!r}"
            for j, value in enumerate(values)
            if exists(value) is None
        ]
    return errors


def _tenant_errors(registry: Registry, i: int, service: Service) -> list[str]:
    """A named tenant that may run none of the named agents could never be
    served: the entry would be dead or a mistake."""
    return [
        f"{SERVICES}: services[{i}].tenants[{j}]: tenant {tenant!r} may run "
        "none of the agents the service names"
        for j, tenant in enumerate(service.tenants)
        if registry.tenant(tenant) is not None
        and not any(registry.tenant_may_run(tenant, a) for a in service.agents)
    ]


def check_service_names(registry: Registry) -> list[str]:
    return [
        error
        for i, service in enumerate(registry.services)
        for error in _name_errors(registry, i, service)
        + _tenant_errors(registry, i, service)
    ]


def check_runtime_names_the_graph_agents(registry: Registry) -> list[str]:
    """Every graph agent some tenant lists is one the runtime may name: the
    runtime and the gateway refuse a call that names an agent the caller's
    ``agents`` lacks, so an agent missing there passes validation and is
    refused on every call. A job agent has no run, and an agent no tenant lists
    cannot be run (the scaffold's new agent starts so)."""
    listed = {a for tenant in registry.tenants for a in tenant.agents}
    graph_agents = [
        agent.id
        for agent in registry.agents
        if agent.kind == "graph" and agent.id in listed
    ]
    runtime = registry.service(RUNTIME_SERVICE)
    if runtime is None:
        return (
            [
                f"{SERVICES}: services: no service {RUNTIME_SERVICE!r}, which "
                "runs the graph agents"
            ]
            if graph_agents
            else []
        )
    i = registry.services.index(runtime)
    return [
        f"{SERVICES}: services[{i}].agents: graph agent {agent!r} is listed by a "
        f"tenant, so add it to the agents of {RUNTIME_SERVICE!r}: without it "
        "every call for the agent is refused"
        for agent in graph_agents
        if agent not in runtime.agents
    ]


def check_servers_are_services(registry: Registry) -> list[str]:
    return [
        f"{TOOLS}: servers[{i}]: server {server.id!r} has no entry in {SERVICES}"
        for i, server in enumerate(registry.servers)
        if registry.service(server.id) is None
    ]


SERVICE_CHECKS: tuple[Callable[[Registry], list[str]], ...] = (
    check_service_ids,
    check_service_calls,
    check_service_names,
    check_runtime_names_the_graph_agents,
    check_servers_are_services,
)
