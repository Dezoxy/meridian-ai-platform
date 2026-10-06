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

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    StringConstraints,
    model_serializer,
)

ResidencyLabel = Literal["eu-region", "eu-zone", "global"]
DataClass = Literal["synthetic", "internal", "personal", "special"]
Purpose = Literal["chat", "embedding"]
Sku = Literal["Standard", "DataZoneStandard", "GlobalStandard"]
ProviderKind = Literal["azure-openai", "replay", "recorded"]
ToolEffect = Literal["read", "write", "decision"]
AgentKind = Literal["graph", "job"]
AgentHost = Literal["langgraph", "agent-framework"]

NonEmptyStr = Annotated[str, StringConstraints(min_length=1)]
# Also what a caller's service ID must match when it is read from a certificate
# (``common/identity.py``).
ENTITY_ID_PATTERN = r"^[a-z0-9][a-z0-9-]*$"
# The longest an ID may be. The tool-server kit builds its worker pattern from
# it (``toolserver/wire.py``), so an ID the registry accepts is one the wire
# accepts.
ENTITY_ID_MAX_LENGTH = 64
EntityId = Annotated[
    str,
    StringConstraints(pattern=ENTITY_ID_PATTERN, max_length=ENTITY_ID_MAX_LENGTH),
]
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
# The longest vector pgvector can index in its ``vector`` type (HNSW and
# IVFFlat stop at 2,000 dimensions; its ``halfvec`` type indexes up to 4,000),
# so a deployment that returns more could not be searched through an index on
# that type.
MAX_EMBEDDING_DIMENSIONS = 2000


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
    # Optional in the schema because a replay or recorded deployment has none of
    # these; checks.py requires them for provider kind azure-openai.
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
    # The length of the vectors an embedding deployment returns, asked of the
    # provider and checked on every answer (T-54); checks.py requires it for
    # purpose embedding and refuses it for chat.
    dimensions: Annotated[int, Field(ge=1, le=MAX_EMBEDDING_DIMENSIONS)] | None = None
    # The live deployment whose answers a recorded deployment replays (S050);
    # checks.py requires it for provider kind recorded, refuses it elsewhere and
    # holds the price equal to that deployment's.
    recorded_from: EntityId | None = None
    # The deployment honours a JSON schema for the answer (Azure OpenAI's
    # structured outputs); checks.py refuses it for purpose embedding. A replay
    # deployment accepts a schema and ignores it (simulated).
    structured_outputs: bool = False


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


class Worker(RegistryModel):
    """A part of one agent, with a tool list of its own (S031).

    Not an agent: the run, the tenant's list, the gateway and the budgets all
    know the agent. ``worker_checks.py`` holds a worker's tools to a subset of
    its agent's, and the workers' lists together to the agent's list.
    """

    id: EntityId
    description: NonEmptyStr
    # May be empty: a worker that only asks the model.
    tools: tuple[ToolId, ...]


class Agent(RegistryModel):
    id: EntityId
    description: NonEmptyStr
    # "graph": the Agent Runtime runs the agent's graph. "job": a platform job
    # that calls the gateway under its own identity: its calls are attributed to
    # the job and charged to its tenant's windows and budget; it has no graph and
    # no run row, so checks.py refuses a tool.
    kind: AgentKind = "graph"
    tools: tuple[ToolId, ...]
    # The agent may send the Model Gateway a response schema; the gateway
    # refuses one from an agent that does not declare it.
    structured_outputs: bool = False
    # The parts the agent's graph is split into, each with its own tools; empty
    # for an agent that is not split. worker_checks.py refuses a job's workers.
    workers: tuple[Worker, ...] = ()
    # The framework that runs the agent's entry point: the Agent Runtime picks
    # its host by this field and refuses to start when the entry point's product
    # is not what the host runs. "agent-framework" is Microsoft Agent Framework.
    # checks.py refuses a job that declares it (no host runs a job) and workers
    # on "agent-framework" (only the langgraph host carries them).
    host: AgentHost = "langgraph"

    @model_serializer(mode="wrap")
    def _omit_what_was_not_there_before(
        self, handler: SerializerFunctionWrapHandler
    ) -> dict[str, Any]:
        """An agent without workers, and an agent on the default host, dump as
        they did before those keys existed, so the evaluation's ``tools``
        fingerprint of such an agent does not move."""
        data: dict[str, Any] = handler(self)
        if not self.workers:
            data.pop("workers", None)
        if self.host == "langgraph":
            data.pop("host", None)
        return data

    def worker(self, worker_id: str) -> Worker | None:
        return next((w for w in self.workers if w.id == worker_id), None)


class AgentsFile(RegistryModel):
    agents: tuple[Agent, ...]


class DataClassPolicy(RegistryModel):
    id: DataClass
    residency: tuple[ResidencyLabel, ...]


class Route(RegistryModel):
    purpose: Purpose
    candidates: Candidates


class ReplayRoute(RegistryModel):
    """The deployment the gateway uses for a purpose in replay mode (and, for
    the ``recorded`` list, in recorded mode)."""

    purpose: Purpose
    deployment: EntityId


class PoliciesFile(RegistryModel):
    data_classes: tuple[DataClassPolicy, ...]
    routes: tuple[Route, ...]
    replay: tuple[ReplayRoute, ...]
    # Unlike replay it need not cover every purpose.
    recorded: tuple[ReplayRoute, ...] = ()


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


class Service(RegistryModel):
    """A workload of the platform that serves or calls another (S055).

    ``id`` is also the chart's service name and its ServiceAccount, and the
    last part of the URI in the service's certificate. ``calls`` are the
    services it may call: a callee refuses a caller that does not list it.
    ``tenants`` and ``agents`` are what the service names when it calls (the
    tenant and agent of a run, or of a gateway call); the callee refuses any
    other. They list what the code names today, no more.
    """

    id: EntityId
    description: NonEmptyStr
    calls: tuple[EntityId, ...]
    tenants: tuple[EntityId, ...]
    agents: tuple[EntityId, ...]


class ServicesFile(RegistryModel):
    services: tuple[Service, ...]


# File stem to model: the loader reads these files, schemas.py writes theirs.
FILE_MODELS: Mapping[str, type[RegistryModel]] = MappingProxyType(
    {
        "providers": ProvidersFile,
        "models": ModelsFile,
        "tools": ToolsFile,
        "agents": AgentsFile,
        "policies": PoliciesFile,
        "tenants": TenantsFile,
        "services": ServicesFile,
    }
)


class Registry(RegistryModel):
    """All seven files together, with lookups for the checks and the runtime."""

    providers: tuple[Provider, ...]
    deployments: tuple[Deployment, ...]
    servers: tuple[Server, ...]
    tools: tuple[Tool, ...]
    agents: tuple[Agent, ...]
    data_classes: tuple[DataClassPolicy, ...]
    routes: tuple[Route, ...]
    replay: tuple[ReplayRoute, ...]
    recorded: tuple[ReplayRoute, ...] = ()
    tenants: tuple[Tenant, ...]
    exchange: ExchangeRate
    services: tuple[Service, ...]

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

    def recorded_deployment(self, purpose: str) -> Deployment | None:
        entry = next((r for r in self.recorded if r.purpose == purpose), None)
        return None if entry is None else self.deployment(entry.deployment)

    def tenant(self, tenant_id: str) -> Tenant | None:
        return next((t for t in self.tenants if t.id == tenant_id), None)

    def tenant_may_run(self, tenant_id: str, agent_id: str) -> bool:
        """The tenant exists and lists the agent (the runtime and the gateway
        both ask this)."""
        tenant = self.tenant(tenant_id)
        return tenant is not None and agent_id in tenant.agents

    def service(self, service_id: str) -> Service | None:
        return next((s for s in self.services if s.id == service_id), None)

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
