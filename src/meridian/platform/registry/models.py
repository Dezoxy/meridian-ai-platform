"""The registry's shape: one Pydantic model per file, the single source.

The JSON Schemas under ``config/registry/schemas/`` are generated from these
models (``schemas.py``); nothing else defines the shape of a registry file.
Every model forbids unknown keys and is frozen, and collections are tuples.
One accepted exception: ``Tool.input_schema`` is a plain dict that is treated
as read-only (S013 copies it before use).
"""

from collections.abc import Mapping
from datetime import date
from decimal import Decimal
from types import MappingProxyType
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

ResidencyLabel = Literal["eu-region", "eu-zone", "global"]
DataClass = Literal["synthetic", "internal", "personal", "special"]
Purpose = Literal["chat", "embedding"]
Sku = Literal["Standard", "DataZoneStandard", "GlobalStandard"]
ProviderKind = Literal["azure-openai", "replay"]
ToolEffect = Literal["read", "write", "decision"]

NonEmptyStr = Annotated[str, StringConstraints(min_length=1)]
EntityId = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9-]*$")]
# MCP tool names: lower-case words joined by underscores.
ToolId = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]*$")]
Money = Annotated[Decimal, Field(ge=0)]
# Lower-case Azure region names ("swedencentral"); display names are refused.
Region = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9]*$")]
# "<location-alias>/<model>", the key of Terraform's openai_deployments output.
TerraformKey = Annotated[str, StringConstraints(pattern=r"^[a-z0-9-]+/[a-z0-9.-]+$")]
# "resource:action" words; no wildcard and no empty part.
Scope = Annotated[str, StringConstraints(pattern=r"^[a-z]+(:[a-z]+)+$")]
Candidates = Annotated[tuple[EntityId, ...], Field(min_length=1)]


class RegistryModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Provider(RegistryModel):
    id: EntityId
    kind: ProviderKind
    description: NonEmptyStr


class ProvidersFile(RegistryModel):
    providers: tuple[Provider, ...]


class Price(RegistryModel):
    """USD retail list price per million tokens.

    YAML floats such as ``3.025`` load exactly (Pydantic converts through the
    shortest repr) up to about 15 significant digits; quote longer values.
    """

    currency: Literal["USD"]
    input_per_million_tokens: Money
    output_per_million_tokens: Money | None = None
    source: NonEmptyStr
    checked: date


class RateLimits(RegistryModel):
    """A deployment's own limits at the provider."""

    requests_per_10_seconds: Annotated[int, Field(ge=1)]
    tokens_per_minute: Annotated[int, Field(ge=1)]


class Deployment(RegistryModel):
    id: EntityId
    provider: EntityId
    purpose: Purpose
    model: NonEmptyStr
    version: NonEmptyStr
    # Optional in the schema because a replay deployment has none of these;
    # checks.py requires them for provider kind azure-openai.
    deployment_name: NonEmptyStr | None = None
    sku: Sku | None = None
    region: Region | None = None
    residency: ResidencyLabel
    data_classes: tuple[DataClass, ...]
    retires: date | None = None
    price: Price
    terraform_key: TerraformKey | None = None
    # The deployment's own limits at the provider; checks.py requires them for
    # provider kind azure-openai and refuses them on a replay deployment.
    rate_limits: RateLimits | None = None


class ModelsFile(RegistryModel):
    deployments: tuple[Deployment, ...]


class Server(RegistryModel):
    id: EntityId
    description: NonEmptyStr


class Tool(RegistryModel):
    id: ToolId
    server: EntityId
    description: NonEmptyStr
    effect: ToolEffect
    scope: Scope
    idempotency_key_required: bool = False
    # The tool's effect needs a human's approval first (S013 and S015 enforce it).
    approval_required: bool = False
    # A JSON Schema object; the runtime injects the idempotency key, so it is
    # not a model-facing argument and does not appear here.
    input_schema: dict[str, Any]
    # What the tool returns as structured content: the same closed and bounded
    # subset as the input. Optional until the tool's server exists.
    output_schema: dict[str, Any] | None = None


class ToolsFile(RegistryModel):
    servers: tuple[Server, ...]
    tools: tuple[Tool, ...]


class Agent(RegistryModel):
    id: EntityId
    description: NonEmptyStr
    tools: tuple[ToolId, ...]


class AgentsFile(RegistryModel):
    agents: tuple[Agent, ...]


class DataClassPolicy(RegistryModel):
    id: DataClass
    residency: tuple[ResidencyLabel, ...]


class Route(RegistryModel):
    purpose: Purpose
    candidates: Candidates


class ReplayRoute(RegistryModel):
    """The deployment the gateway uses for a purpose in replay mode."""

    purpose: Purpose
    deployment: EntityId


class PoliciesFile(RegistryModel):
    data_classes: tuple[DataClassPolicy, ...]
    routes: tuple[Route, ...]
    replay: tuple[ReplayRoute, ...]


class TenantLimits(RegistryModel):
    """What a tenant may use. The two rate windows are Azure OpenAI's own."""

    requests_per_10_seconds: Annotated[int, Field(ge=1)]
    tokens_per_minute: Annotated[int, Field(ge=1)]
    tokens_per_day: Annotated[int, Field(ge=1)]
    # Six decimals: the ledger counts micro-EUR, so the limit converts exactly.
    cost_per_month_eur: Annotated[Decimal, Field(gt=0, decimal_places=6)]


class Tenant(RegistryModel):
    id: EntityId
    description: NonEmptyStr
    data_class: DataClass
    agents: tuple[EntityId, ...]
    limits: TenantLimits


class ExchangeRate(RegistryModel):
    """The planning rate the EUR cost quota is computed with."""

    usd_per_eur: Annotated[Decimal, Field(gt=0)]
    source: NonEmptyStr
    checked: date


class TenantsFile(RegistryModel):
    exchange: ExchangeRate
    tenants: tuple[Tenant, ...]


# File stem to model: the loader reads these files, schemas.py writes theirs.
FILE_MODELS: Mapping[str, type[RegistryModel]] = MappingProxyType(
    {
        "providers": ProvidersFile,
        "models": ModelsFile,
        "tools": ToolsFile,
        "agents": AgentsFile,
        "policies": PoliciesFile,
        "tenants": TenantsFile,
    }
)


class Registry(RegistryModel):
    """All six files together, with lookups for the checks and the runtime."""

    providers: tuple[Provider, ...]
    deployments: tuple[Deployment, ...]
    servers: tuple[Server, ...]
    tools: tuple[Tool, ...]
    agents: tuple[Agent, ...]
    data_classes: tuple[DataClassPolicy, ...]
    routes: tuple[Route, ...]
    replay: tuple[ReplayRoute, ...]
    tenants: tuple[Tenant, ...]
    exchange: ExchangeRate

    def provider(self, provider_id: str) -> Provider | None:
        return next((p for p in self.providers if p.id == provider_id), None)

    def deployment(self, deployment_id: str) -> Deployment | None:
        return next((d for d in self.deployments if d.id == deployment_id), None)

    def tool(self, tool_id: str) -> Tool | None:
        return next((t for t in self.tools if t.id == tool_id), None)

    def agent(self, agent_id: str) -> Agent | None:
        return next((a for a in self.agents if a.id == agent_id), None)

    def data_class(self, class_id: str) -> DataClassPolicy | None:
        return next((c for c in self.data_classes if c.id == class_id), None)

    def route(self, purpose: str) -> Route | None:
        return next((r for r in self.routes if r.purpose == purpose), None)

    def replay_deployment(self, purpose: str) -> Deployment | None:
        entry = next((r for r in self.replay if r.purpose == purpose), None)
        return None if entry is None else self.deployment(entry.deployment)

    def tenant(self, tenant_id: str) -> Tenant | None:
        return next((t for t in self.tenants if t.id == tenant_id), None)

    def tenant_may_run(self, tenant_id: str, agent_id: str) -> bool:
        """The tenant exists and lists the agent (the runtime and the gateway
        both ask this)."""
        tenant = self.tenant(tenant_id)
        return tenant is not None and agent_id in tenant.agents

    def has_provider(self, provider_id: str) -> bool:
        return self.provider(provider_id) is not None

    def has_deployment(self, deployment_id: str) -> bool:
        return self.deployment(deployment_id) is not None

    def has_tool(self, tool_id: str) -> bool:
        return self.tool(tool_id) is not None

    def has_agent(self, agent_id: str) -> bool:
        return self.agent(agent_id) is not None

    def has_data_class(self, class_id: str) -> bool:
        return self.data_class(class_id) is not None

    def has_server(self, server_id: str) -> bool:
        return any(s.id == server_id for s in self.servers)
