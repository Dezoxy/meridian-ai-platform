"""Cross-file checks that a schema cannot express; each returns messages.

``run_checks`` runs them all and collects every message, so one run reports
every problem. Messages read ``file: path: problem`` like the loader's.
"""

import re
from collections.abc import Callable, Iterable, Mapping
from types import MappingProxyType
from typing import assert_never, get_args

from meridian.platform.registry.models import (
    DataClass,
    Deployment,
    ProviderKind,
    Purpose,
    Registry,
    ReplayRoute,
    ResidencyLabel,
)
from meridian.platform.registry.service_checks import SERVICE_CHECKS
from meridian.platform.registry.tool_schema import input_schema_errors

# Azure regions inside the EU. Switzerland, Norway and the UK are not in the
# EU, so switzerlandnorth, norwayeast and uksouth are deliberately absent.
EU_AZURE_REGIONS = frozenset(
    {
        "swedencentral",
        "swedensouth",
        "westeurope",
        "northeurope",
        "francecentral",
        "francesouth",
        "germanywestcentral",
        "germanynorth",
        "polandcentral",
        "italynorth",
        "spaincentral",
    }
)
# Hard rule 3 (see docs/architecture/security/data-classification.md): the
# residency labels each data class may ever reach. policies.yaml may narrow a
# class but never widen it; widening needs a change to this constant, which a
# reviewer sees, not to a YAML file.
RESIDENCY_CEILING: Mapping[DataClass, frozenset[ResidencyLabel]] = MappingProxyType(
    {
        "synthetic": frozenset({"eu-region", "eu-zone", "global"}),
        "internal": frozenset({"eu-region", "eu-zone"}),
        "personal": frozenset({"eu-region", "eu-zone"}),
        "special": frozenset(),
    }
)
AZURE_REQUIRED = (
    "sku",
    "region",
    "deployment_name",
    "retires",
    "terraform_key",
    "rate_limits",
)
REPLAY_FORBIDDEN = ("sku", "region", "terraform_key", "rate_limits")
RECORDED_FORBIDDEN = ("deployment_name", *REPLAY_FORBIDDEN)
# Prices a recorded deployment must share with the deployment it was recorded from.
RECORDED_PRICE_FIELDS = (
    "currency",
    "input_per_million_tokens",
    "output_per_million_tokens",
)
RECORDED_PURPOSE = "chat"
# What a deployment's rate_limits and a tenant's limits both name, compared by
# check_tenant_limits.
SHARED_RATE_FIELDS = ("requests_per_10_seconds", "tokens_per_minute")
EMBEDDING_PURPOSE = "embedding"
CHAT_PURPOSE = "chat"
# What must be equal for two vectors to be comparable (T-54).
VECTOR_FIELDS = ("model", "version", "dimensions")
REPLAY_PROVIDER_ID = "replay"
REPLAY_MODEL_PREFIX = "replay-"
RECORDED_PROVIDER_ID = "recorded"
RECORDED_MODEL_PREFIX = "recorded-"
# Provider kinds that run inside the platform, each with the one id it may have.
IN_PLATFORM_PROVIDER_IDS = {
    "replay": REPLAY_PROVIDER_ID,
    "recorded": RECORDED_PROVIDER_ID,
}
# T-31, second signal: a word in a tool's id or scope that says it decides.
# Inflections count (approved, declined, decided ...). "approval" and
# "approvals" are the one exception: request_approval only asks a human.
DECISION_PREFIXES = ("decid", "decision", "reject", "declin")
APPROVAL_WORDS = frozenset({"approval", "approvals"})
DENY_WORDS = frozenset({"deny", "denied", "denies", "denial"})
TOKEN_SPLIT = re.compile(r"[_:\-]")

MODELS = "models.yaml"
TOOLS = "tools.yaml"
AGENTS = "agents.yaml"
POLICIES = "policies.yaml"
TENANTS = "tenants.yaml"
PROVIDERS = "providers.yaml"


def _duplicates(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    repeated: list[str] = []
    for value in values:
        if value in seen and value not in repeated:
            repeated.append(value)
        seen.add(value)
    return repeated


def _list_errors(
    where: str,
    values: tuple[str, ...],
    exists: Callable[[str], bool],
    kind: str,
) -> list[str]:
    """Unknown and repeated entries of one reference list."""
    unknown = [
        f"{where}[{i}]: unknown {kind} {value!r}"
        for i, value in enumerate(values)
        if not exists(value)
    ]
    repeated = [f"{where}: {kind} {v!r} is listed twice" for v in _duplicates(values)]
    return unknown + repeated


def _ref_error(
    where: str, value: str, exists: Callable[[str], bool], kind: str
) -> list[str]:
    return [] if exists(value) else [f"{where}: unknown {kind} {value!r}"]


def check_unique_ids(registry: Registry) -> list[str]:
    groups = (
        (f"{PROVIDERS}: providers", "id", [p.id for p in registry.providers]),
        (f"{MODELS}: deployments", "id", [d.id for d in registry.deployments]),
        (f"{TOOLS}: servers", "id", [s.id for s in registry.servers]),
        (f"{TOOLS}: tools", "id", [t.id for t in registry.tools]),
        (f"{AGENTS}: agents", "id", [a.id for a in registry.agents]),
        (f"{TENANTS}: tenants", "id", [t.id for t in registry.tenants]),
        (f"{POLICIES}: data_classes", "id", [c.id for c in registry.data_classes]),
        (f"{POLICIES}: routes", "purpose", [r.purpose for r in registry.routes]),
        (f"{POLICIES}: replay", "purpose", [r.purpose for r in registry.replay]),
        (f"{POLICIES}: recorded", "purpose", [r.purpose for r in registry.recorded]),
    )
    errors = [
        f"{where}: duplicate {field} {value!r}"
        for where, field, ids in groups
        for value in _duplicates(ids)
    ]
    present = {c.id for c in registry.data_classes}
    errors += [
        f"{POLICIES}: data_classes: data class {name!r} is missing"
        for name in get_args(DataClass)
        if name not in present
    ]
    return errors


def check_deployment_uniqueness(registry: Registry) -> list[str]:
    """One Terraform key and one Azure deployment name per deployment (T-12)."""
    keys = [d.terraform_key for d in registry.deployments if d.terraform_key]
    slots = [
        f"{d.provider}/{d.region}/{d.deployment_name}"
        for d in registry.deployments
        if d.region and d.deployment_name
    ]
    return [
        f"{MODELS}: deployments: duplicate terraform_key {key!r}"
        for key in _duplicates(keys)
    ] + [
        f"{MODELS}: deployments: duplicate deployment name (provider/region/name) "
        f"{slot!r}"
        for slot in _duplicates(slots)
    ]


def _deployment_references(registry: Registry) -> list[str]:
    errors: list[str] = []
    for i, dep in enumerate(registry.deployments):
        where = f"{MODELS}: deployments[{i}]"
        errors += _ref_error(
            f"{where}.provider", dep.provider, registry.has_provider, "provider"
        )
        errors += _list_errors(
            f"{where}.data_classes",
            dep.data_classes,
            registry.has_data_class,
            "data class",
        )
    return errors


def _tool_and_agent_references(registry: Registry) -> list[str]:
    errors: list[str] = []
    for i, tool in enumerate(registry.tools):
        errors += _ref_error(
            f"{TOOLS}: tools[{i}].server", tool.server, registry.has_server, "server"
        )
    for i, agent in enumerate(registry.agents):
        errors += _list_errors(
            f"{AGENTS}: agents[{i}].tools", agent.tools, registry.has_tool, "tool"
        )
    return errors


def _tenant_and_policy_references(registry: Registry) -> list[str]:
    errors: list[str] = []
    for i, tenant in enumerate(registry.tenants):
        where = f"{TENANTS}: tenants[{i}]"
        errors += _ref_error(
            f"{where}.data_class",
            tenant.data_class,
            registry.has_data_class,
            "data class",
        )
        errors += _list_errors(
            f"{where}.agents", tenant.agents, registry.has_agent, "agent"
        )
    for i, route in enumerate(registry.routes):
        errors += _list_errors(
            f"{POLICIES}: routes[{i}].candidates",
            route.candidates,
            registry.has_deployment,
            "deployment",
        )
    for i, policy in enumerate(registry.data_classes):
        errors += [
            f"{POLICIES}: data_classes[{i}].residency: label {label!r} is listed twice"
            for label in _duplicates(policy.residency)
        ]
    return errors


def check_references(registry: Registry) -> list[str]:
    return (
        _deployment_references(registry)
        + _tool_and_agent_references(registry)
        + _tenant_and_policy_references(registry)
    )


def check_policy_ceiling(registry: Registry) -> list[str]:
    """Hard rule 3 in code: policies.yaml cannot widen a class past its ceiling."""
    return [
        f"{POLICIES}: data_classes[{i}].residency[{j}]: label {label!r} is outside "
        f"the ceiling for data class {policy.id!r}; widening this needs a change "
        "to the check itself (hard rule 3)"
        for i, policy in enumerate(registry.data_classes)
        for j, label in enumerate(policy.residency)
        if label not in RESIDENCY_CEILING[policy.id]
    ]


def check_providers(registry: Registry) -> list[str]:
    return [
        f"{PROVIDERS}: providers[{i}].id: a provider of kind {provider.kind!r} "
        f"must have id {IN_PLATFORM_PROVIDER_IDS[provider.kind]!r}"
        for i, provider in enumerate(registry.providers)
        if provider.kind in IN_PLATFORM_PROVIDER_IDS
        and provider.id != IN_PLATFORM_PROVIDER_IDS[provider.kind]
    ]


def _azure_field_errors(dep: Deployment, where: str) -> list[str]:
    return [
        f"{where}.{name}: required for provider kind 'azure-openai' "
        f"(deployment {dep.id!r})"
        for name in AZURE_REQUIRED
        if getattr(dep, name) is None
    ]


def _replay_field_errors(dep: Deployment, where: str) -> list[str]:
    errors = [
        f"{where}.{name}: must not be set for provider kind 'replay' "
        f"(deployment {dep.id!r})"
        for name in REPLAY_FORBIDDEN
        if getattr(dep, name) is not None
    ]
    if not dep.model.startswith(REPLAY_MODEL_PREFIX):
        errors.append(
            f"{where}.model: a replay deployment's model must start with "
            f"{REPLAY_MODEL_PREFIX!r} (deployment {dep.id!r})"
        )
    return errors


def _recorded_price_errors(
    dep: Deployment, source: Deployment, where: str
) -> list[str]:
    """A recorded run is charged what the live run was charged."""
    return [
        f"{where}.price.{name}: {ours} differs from {theirs} of deployment "
        f"{source.id!r} (recorded_from); a recorded run is charged what the live "
        f"run was charged (deployment {dep.id!r})"
        for name in RECORDED_PRICE_FIELDS
        if (ours := getattr(dep.price, name)) != (theirs := getattr(source.price, name))
    ]


def _recorded_class_errors(
    dep: Deployment, source: Deployment, where: str
) -> list[str]:
    """A class is never asserted over a recording made on a deployment that
    never allowed it (T-78)."""
    beyond = sorted(set(dep.data_classes) - set(source.data_classes))
    if not beyond:
        return []
    return [
        f"{where}.data_classes: {', '.join(beyond)} not allowed by deployment "
        f"{source.id!r} (recorded_from); a class is never asserted over a "
        "recording made on a deployment that never allowed it "
        f"(deployment {dep.id!r})"
    ]


def _recorded_from_errors(registry: Registry, dep: Deployment, where: str) -> list[str]:
    if dep.recorded_from is None:
        return [
            f"{where}.recorded_from: required for provider kind 'recorded' "
            f"(deployment {dep.id!r})"
        ]
    ref = f"{where}.recorded_from"
    source = registry.deployment(dep.recorded_from)
    if source is None:
        return [f"{ref}: unknown deployment {dep.recorded_from!r}"]
    provider = registry.provider(source.provider)
    if provider is not None and provider.kind != "azure-openai":
        return [
            f"{ref}: deployment {source.id!r} is a {provider.kind} deployment; "
            "a recording is of a real model"
        ]
    errors: list[str] = []
    if source.purpose != dep.purpose:
        errors.append(
            f"{ref}: deployment {source.id!r} has purpose {source.purpose!r}, "
            f"the recorded deployment's is {dep.purpose!r}"
        )
    return (
        errors
        + _recorded_class_errors(dep, source, where)
        + _recorded_price_errors(dep, source, where)
    )


def _recorded_field_errors(
    registry: Registry, dep: Deployment, where: str
) -> list[str]:
    errors = [
        f"{where}.{name}: must not be set for provider kind 'recorded' "
        f"(deployment {dep.id!r})"
        for name in RECORDED_FORBIDDEN
        if getattr(dep, name) is not None
    ]
    if not dep.model.startswith(RECORDED_MODEL_PREFIX):
        errors.append(
            f"{where}.model: a recorded deployment's model must start with "
            f"{RECORDED_MODEL_PREFIX!r} (deployment {dep.id!r})"
        )
    if dep.purpose != RECORDED_PURPOSE:
        errors.append(
            f"{where}.purpose: a recorded deployment's purpose must be "
            f"{RECORDED_PURPOSE!r}, not {dep.purpose!r} (deployment {dep.id!r})"
        )
    return errors + _recorded_from_errors(registry, dep, where)


def check_provider_fields(registry: Registry) -> list[str]:
    errors: list[str] = []
    for i, dep in enumerate(registry.deployments):
        provider = registry.provider(dep.provider)
        if provider is None:
            continue
        where = f"{MODELS}: deployments[{i}]"
        if provider.kind != "recorded" and dep.recorded_from is not None:
            errors.append(
                f"{where}.recorded_from: must not be set for provider kind "
                f"{provider.kind!r} (deployment {dep.id!r})"
            )
        match provider.kind:
            case "azure-openai":
                errors += _azure_field_errors(dep, where)
            case "replay":
                errors += _replay_field_errors(dep, where)
            case "recorded":
                errors += _recorded_field_errors(registry, dep, where)
            case unreachable:
                assert_never(unreachable)
    return errors


def check_dimensions(registry: Registry) -> list[str]:
    """An embedding deployment states the length of its vectors, and a chat one
    has none (T-54)."""
    errors: list[str] = []
    for i, dep in enumerate(registry.deployments):
        where = f"{MODELS}: deployments[{i}].dimensions"
        if dep.purpose == EMBEDDING_PURPOSE and dep.dimensions is None:
            errors.append(
                f"{where}: required for purpose {dep.purpose!r} (deployment {dep.id!r})"
            )
        elif dep.purpose != EMBEDDING_PURPOSE and dep.dimensions is not None:
            errors.append(
                f"{where}: must not be set for purpose {dep.purpose!r} "
                f"(deployment {dep.id!r})"
            )
    return errors


def _allowed_labels(sku: str, region: str) -> tuple[str, ...]:
    """The residency labels an Azure SKU in a region supports."""
    if sku == "GlobalStandard" or region not in EU_AZURE_REGIONS:
        return ("global",)
    return ("eu-zone",) if sku == "DataZoneStandard" else ("eu-region",)


def _label_facts(
    dep: Deployment, kind: ProviderKind
) -> tuple[tuple[str, ...], str] | None:
    """The labels the facts allow and how to say the facts; None if unknown."""
    match kind:
        case "replay" | "recorded":
            return ("eu-region",), f"the {kind} provider runs inside the platform"
        case "azure-openai":
            if dep.sku is None or dep.region is None:
                return None  # reported by check_provider_fields
            facts = f"sku {dep.sku!r} in region {dep.region!r}"
            return _allowed_labels(dep.sku, dep.region), facts
        case unreachable:
            assert_never(unreachable)


def check_residency_labels(registry: Registry) -> list[str]:
    errors: list[str] = []
    for i, dep in enumerate(registry.deployments):
        provider = registry.provider(dep.provider)
        found = _label_facts(dep, provider.kind) if provider else None
        if found is None:
            continue
        allowed, facts = found
        if dep.residency not in allowed:
            errors.append(
                f"{MODELS}: deployments[{i}].residency: label {dep.residency!r} "
                f"does not match {facts}; expected "
                f"{' or '.join(map(repr, allowed))} (deployment {dep.id!r})"
            )
    return errors


def check_data_classes_vs_label(registry: Registry) -> list[str]:
    """Hard rule 3: a deployment serves only the classes its label permits."""
    errors: list[str] = []
    for i, dep in enumerate(registry.deployments):
        for j, name in enumerate(dep.data_classes):
            policy = registry.data_class(name)
            if policy is not None and dep.residency not in policy.residency:
                errors.append(
                    f"{MODELS}: deployments[{i}].data_classes[{j}]: data class "
                    f"{name!r} does not allow residency label {dep.residency!r} "
                    f"(deployment {dep.id!r})"
                )
    return errors


def check_tools(registry: Registry) -> list[str]:
    errors: list[str] = []
    for i, tool in enumerate(registry.tools):
        where = f"{TOOLS}: tools[{i}]"
        suffix = f"(tool {tool.id!r})"
        if tool.effect in {"write", "decision"} and not tool.idempotency_key_required:
            errors.append(
                f"{where}.idempotency_key_required: a {tool.effect} tool needs an "
                f"idempotency key {suffix}"
            )
        if tool.approval_required and tool.effect != "write":
            errors.append(
                f"{where}.approval_required: only a write tool can require "
                f"approval {suffix}"
            )
        errors += [
            f"{where}.{message} {suffix}"
            for message in input_schema_errors(tool.input_schema)
        ]
        if tool.output_schema is not None:
            errors += [
                f"{where}.{message} {suffix}"
                for message in input_schema_errors(
                    tool.output_schema, root="output_schema", allow_conditionals=True
                )
            ]
    return errors


def _is_decision_word(token: str) -> bool:
    if token in DENY_WORDS:
        return True
    if token.startswith("approv"):
        return token not in APPROVAL_WORDS
    return token.startswith(DECISION_PREFIXES)


def _decision_token(*texts: str) -> str | None:
    for text in texts:
        for token in TOKEN_SPLIT.split(text.lower()):
            if _is_decision_word(token):
                return token
    return None


def check_no_decision_tools(registry: Registry) -> list[str]:
    """T-31: an agent never holds a tool that decides a claim."""
    errors: list[str] = []
    for i, agent in enumerate(registry.agents):
        for j, name in enumerate(agent.tools):
            tool = registry.tool(name)
            if tool is None:
                continue
            where = f"{AGENTS}: agents[{i}].tools[{j}]: agent {agent.id!r} lists"
            token = _decision_token(tool.id, tool.scope)
            if tool.effect == "decision":
                errors.append(
                    f"{where} decision tool {name!r}; only humans decide (T-31)"
                )
            elif token is not None:
                errors.append(
                    f"{where} tool {name!r} whose id or scope contains the "
                    f"decision word {token!r}; only humans decide (T-31)"
                )
    return errors


def check_job_agents(registry: Registry) -> list[str]:
    """A job has no run row, so no tool server could bind its call."""
    return [
        f"{AGENTS}: agents[{i}].tools[{j}]: job agent {agent.id!r} lists tool "
        f"{name!r}; a job has no run row, so no tool server could bind its call"
        for i, agent in enumerate(registry.agents)
        if agent.kind == "job"
        for j, name in enumerate(agent.tools)
    ]


def check_routes(registry: Registry) -> list[str]:
    errors: list[str] = []
    for i, route in enumerate(registry.routes):
        for j, name in enumerate(route.candidates):
            dep = registry.deployment(name)
            if dep is not None and dep.purpose != route.purpose:
                errors.append(
                    f"{POLICIES}: routes[{i}].candidates[{j}]: deployment "
                    f"{name!r} has purpose {dep.purpose!r}, the route is "
                    f"{route.purpose!r}"
                )
    present = {r.purpose for r in registry.routes}
    errors += [
        f"{POLICIES}: routes: no route for purpose {purpose!r}"
        for purpose in get_args(Purpose)
        if purpose not in present
    ]
    return errors


def check_embedding_route(registry: Registry) -> list[str]:
    """Vectors of different models, versions or sizes are not comparable, and a
    fallback inside the route would mix them without any error (T-54): every
    candidate of the embedding route matches the first one."""
    errors: list[str] = []
    for i, route in enumerate(registry.routes):
        if route.purpose != EMBEDDING_PURPOSE:
            continue
        first = registry.deployment(route.candidates[0])
        if first is None:
            continue  # an unknown candidate is reported by check_references
        for j, name in enumerate(route.candidates[1:], start=1):
            dep = registry.deployment(name)
            if dep is None:
                continue
            for field in VECTOR_FIELDS:
                value, expected = getattr(dep, field), getattr(first, field)
                if value is None or expected is None or value == expected:
                    continue  # a missing size is reported by check_dimensions
                errors.append(
                    f"{POLICIES}: routes[{i}].candidates[{j}]: deployment {name!r} "
                    f"has {field} {value!r}, but the route's first candidate "
                    f"{first.id!r} has {expected!r}; vectors of different models "
                    "or sizes are not comparable (T-54)"
                )
    return errors


def _is_replay(registry: Registry, deployment: Deployment) -> bool:
    provider = registry.provider(deployment.provider)
    return provider is not None and provider.kind == "replay"


def _is_recorded(registry: Registry, deployment: Deployment) -> bool:
    provider = registry.provider(deployment.provider)
    return provider is not None and provider.kind == "recorded"


def check_replay(registry: Registry) -> list[str]:
    """Replay is a gateway mode: one replay deployment per routed purpose.

    A replay deployment is never a route candidate, so a real outage cannot be
    answered with canned text.
    """
    errors: list[str] = []
    for i, entry in enumerate(registry.replay):
        where = f"{POLICIES}: replay[{i}].deployment"
        dep = registry.deployment(entry.deployment)
        if dep is None:
            errors.append(f"{where}: unknown deployment {entry.deployment!r}")
            continue
        provider = registry.provider(dep.provider)
        if provider is not None and provider.kind != "replay":
            errors.append(
                f"{where}: deployment {dep.id!r} is not a replay deployment "
                f"(provider kind {provider.kind!r})"
            )
        if dep.purpose != entry.purpose:
            errors.append(
                f"{where}: deployment {dep.id!r} has purpose {dep.purpose!r}, "
                f"the replay entry is {entry.purpose!r}"
            )
    for i, route in enumerate(registry.routes):
        errors += [
            f"{POLICIES}: routes[{i}].candidates[{j}]: deployment {name!r} is a "
            "replay deployment; replay is a gateway mode, never a route candidate"
            for j, name in enumerate(route.candidates)
            if (dep := registry.deployment(name)) and _is_replay(registry, dep)
        ]
    covered = {r.purpose for r in registry.replay}
    errors += [
        f"{POLICIES}: replay: no replay deployment for purpose {route.purpose!r}"
        for route in registry.routes
        if route.purpose not in covered
    ]
    errors += _mode_tenant_errors(registry, registry.replay, "replay")
    return errors


def check_recorded(registry: Registry) -> list[str]:
    """Recorded is a gateway mode: answers recorded from a real model.

    Unlike replay it need not cover every purpose. A recorded deployment is
    never a route candidate, so a real outage cannot be answered from a file;
    it is never a replay entry either (check_replay reports that).
    """
    errors: list[str] = []
    for i, entry in enumerate(registry.recorded):
        where = f"{POLICIES}: recorded[{i}].deployment"
        dep = registry.deployment(entry.deployment)
        if dep is None:
            errors.append(f"{where}: unknown deployment {entry.deployment!r}")
            continue
        provider = registry.provider(dep.provider)
        if provider is not None and provider.kind != "recorded":
            errors.append(
                f"{where}: deployment {dep.id!r} is not a recorded deployment "
                f"(provider kind {provider.kind!r})"
            )
        if dep.purpose != entry.purpose:
            errors.append(
                f"{where}: deployment {dep.id!r} has purpose {dep.purpose!r}, "
                f"the recorded entry is {entry.purpose!r}"
            )
    for i, route in enumerate(registry.routes):
        errors += [
            f"{POLICIES}: routes[{i}].candidates[{j}]: deployment {name!r} is a "
            "recorded deployment; recorded is a gateway mode, never a route "
            "candidate"
            for j, name in enumerate(route.candidates)
            if (dep := registry.deployment(name)) and _is_recorded(registry, dep)
        ]
    errors += _mode_tenant_errors(registry, registry.recorded, "recorded")
    return errors


def check_replay_dimensions(registry: Registry) -> list[str]:
    """Simulated vectors have the length of the real route's, or a store that
    holds both would compare unlike vectors (T-54)."""
    route = registry.route(EMBEDDING_PURPOSE)
    errors: list[str] = []
    for i, entry in enumerate(registry.replay):
        dep = registry.deployment(entry.deployment)
        if entry.purpose != EMBEDDING_PURPOSE or route is None or dep is None:
            continue
        for name in route.candidates:
            candidate = registry.deployment(name)
            if candidate is None or None in (candidate.dimensions, dep.dimensions):
                continue  # reported by check_references or check_dimensions
            if candidate.dimensions != dep.dimensions:
                errors.append(
                    f"{POLICIES}: replay[{i}].deployment: deployment {dep.id!r} "
                    f"has dimensions {dep.dimensions}, but route candidate "
                    f"{name!r} has {candidate.dimensions}; simulated vectors must "
                    "be the size of the route's (T-54)"
                )
    return errors


def _mode_tenant_errors(
    registry: Registry, entries: tuple[ReplayRoute, ...], mode: str
) -> list[str]:
    """Replay (and recorded mode) stands in for the route of a purpose, so it
    must serve every tenant the route serves: its classes must include the
    tenant's class and its label must be one that class may reach."""
    errors: list[str] = []
    routed = {route.purpose for route in registry.routes}
    for i, entry in enumerate(entries):
        dep = registry.deployment(entry.deployment)
        if dep is None or entry.purpose not in routed:
            continue
        where = f"{POLICIES}: {mode}[{i}]: deployment {dep.id!r}"
        for tenant in registry.tenants:
            policy = registry.data_class(tenant.data_class)
            who = f"data class {tenant.data_class!r} of tenant {tenant.id!r}"
            if tenant.data_class not in dep.data_classes:
                errors.append(
                    f"{where} does not allow {who}, so {mode} mode could not serve it"
                )
            if policy is not None and dep.residency not in policy.residency:
                errors.append(
                    f"{where} has residency {dep.residency!r}, which {who} "
                    "may not reach"
                )
    return errors


def check_tenant_coverage(registry: Registry) -> list[str]:
    """Every tenant must be servable for every purpose that has a route."""
    errors: list[str] = []
    for i, route in enumerate(registry.routes):
        candidates = [d for n in route.candidates if (d := registry.deployment(n))]
        errors += [
            f"{POLICIES}: routes[{i}]: no candidate allows data class "
            f"{tenant.data_class!r} for purpose {route.purpose!r}, so tenant "
            f"{tenant.id!r} can never be served"
            for tenant in registry.tenants
            if not any(tenant.data_class in d.data_classes for d in candidates)
        ]
    return errors


def _chat_deployments(registry: Registry) -> list[tuple[Deployment, str]]:
    """The chat route's candidates, the chat replay deployment and the chat
    recorded one, each with how to say its role; a name that resolves to
    nothing is skipped."""
    found: list[tuple[Deployment, str]] = []
    route = registry.route(CHAT_PURPOSE)
    for name in () if route is None else route.candidates:
        if dep := registry.deployment(name):
            found.append((dep, f"a candidate of the {CHAT_PURPOSE!r} route"))
    if dep := registry.replay_deployment(CHAT_PURPOSE):
        found.append((dep, f"the replay deployment for purpose {CHAT_PURPOSE!r}"))
    if dep := registry.recorded_deployment(CHAT_PURPOSE):
        found.append((dep, "a recorded deployment"))
    return found


def check_structured_outputs(registry: Registry) -> list[str]:
    """A schema for the answer needs a deployment that can honour it (S051).

    An embedding deployment has no answer to shape. When an agent may send a
    schema, every chat deployment the gateway could pick, replay and recorded
    included, must declare it, or a run would meet the refusal only at the call.
    """
    index = {dep.id: i for i, dep in enumerate(registry.deployments)}
    errors = [
        f"{MODELS}: deployments[{i}].structured_outputs: must not be set for "
        f"purpose {dep.purpose!r} (deployment {dep.id!r})"
        for i, dep in enumerate(registry.deployments)
        if dep.structured_outputs and dep.purpose != CHAT_PURPOSE
    ]
    asking = [agent.id for agent in registry.agents if agent.structured_outputs]
    if not asking:
        return errors
    who = ("agent " if len(asking) == 1 else "agents ") + " and ".join(
        map(repr, asking)
    )
    verb = "declares" if len(asking) == 1 else "declare"
    reported: set[str] = set()
    for dep, role in _chat_deployments(registry):
        if dep.structured_outputs or dep.id in reported:
            continue
        reported.add(dep.id)
        errors.append(
            f"{MODELS}: deployments[{index[dep.id]}].structured_outputs: required "
            f"because {who} {verb} structured_outputs and this deployment "
            f"is {role} (deployment {dep.id!r})"
        )
    return errors


def check_tenant_limits(registry: Registry) -> list[str]:
    """The tenants' rate limits must fit in every route's candidates.

    Every tenant may use its whole share at once, so the sum over all tenants
    of each rate limit must not pass any candidate's own limit at the provider;
    otherwise one tenant could cause a 429 for the others (T-45, and T-55 for
    the embedding route, whose requests count against the same windows).
    """
    totals = {
        field: sum(getattr(t.limits, field) for t in registry.tenants)
        for field in SHARED_RATE_FIELDS
    }
    errors: list[str] = []
    for i, route in enumerate(registry.routes):
        for j, name in enumerate(route.candidates):
            dep = registry.deployment(name)
            if dep is None or dep.rate_limits is None:
                continue  # reported by check_references or check_provider_fields
            for field in SHARED_RATE_FIELDS:
                limit = getattr(dep.rate_limits, field)
                if totals[field] > limit:
                    errors.append(
                        f"{POLICIES}: routes[{i}].candidates[{j}]: deployment "
                        f"{name!r} allows {limit} {field} but the tenants' limits "
                        f"add up to {totals[field]}"
                    )
    return errors


CHECKS: tuple[Callable[[Registry], list[str]], ...] = (
    check_unique_ids,
    check_deployment_uniqueness,
    check_references,
    check_policy_ceiling,
    check_providers,
    check_provider_fields,
    check_dimensions,
    check_residency_labels,
    check_data_classes_vs_label,
    check_tools,
    check_no_decision_tools,
    check_job_agents,
    check_routes,
    check_embedding_route,
    check_replay,
    check_recorded,
    check_replay_dimensions,
    check_structured_outputs,
    check_tenant_coverage,
    check_tenant_limits,
    *SERVICE_CHECKS,
)


def run_checks(registry: Registry) -> tuple[str, ...]:
    """Every check's messages, in a fixed order."""
    return tuple(message for check in CHECKS for message in check(registry))
