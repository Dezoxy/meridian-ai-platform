# Platform registry

The declarative source of truth for the platform: which models exist, which
data may reach them, which tools exist and which agent may call them, and
which tenant runs which agent, and which service may call which. Status:
**implemented** (S008) as validated configuration; the Model Gateway and the
Agent Runtime load it at startup (S009), and in live mode the gateway routes
each chat call by it: the tenant's data class against every candidate's
residency label and data classes (S010), then the candidates it kept, in the
route's order, until one answers (S042). The design is in the plan's S008
section and in
[ADR 3](../../docs/architecture/decisions/0003-build-a-thin-model-gateway.md).

## Files

| File | Holds |
|---|---|
| `providers.yaml` | Provider accounts: Azure OpenAI, the replay provider and the recorded provider |
| `models.yaml` | Model deployments: model, version, SKU, region, residency label, allowed data classes, price, retirement date, the deployment's own rate limits, the vector length of an embedding deployment |
| `tools.yaml` | MCP servers and their tools: effect, scope, input schema and output schema, idempotency |
| `agents.yaml` | Agents, their kind (`graph` or `job`) and their tool allowlists |
| `policies.yaml` | Data classes with the residency labels they allow, the ordered routes per purpose, the replay deployment per purpose and the recorded one |
| `tenants.yaml` | Tenants with their data class, the agents they may run and their limits, and the exchange rate the cost quota uses |
| `services.yaml` | The platform's services: which ones each may call, and the tenants and agents it names when it calls (S055) |
| `schemas/` | JSON Schemas generated from the Pydantic models; never edited by hand |
| `snapshots/` | Terraform's deployment outputs without account names or endpoints |

Each YAML file names its schema on the first line, so an editor with the
YAML language server checks it as you type.

## Validate

```bash
make registry
```

That runs `meridian registry validate` against the Terraform snapshot and
`meridian registry schemas --check`; CI runs the same target in the `python`
job. Beyond the schemas, validation refuses:

- a reference to anything that does not exist, a repeated ID or YAML key,
  an unknown key or file, an empty ID, name or description, and YAML
  anchors and aliases, which would hide the effective value from a
  reviewer;
- a residency label that does not match the SKU and region: `Standard` in an
  EU region is `eu-region`, `DataZoneStandard` is `eu-zone`,
  `GlobalStandard` is `global`, replay and recorded are `eu-region`;
- a deployment that allows a data class its label does not permit, so
  personal data never reaches a `global` deployment (hard rule 3). The
  ceiling per class is fixed in the validator's code as well as in
  `policies.yaml`, so widening it means changing the check itself;
- a mutating or decision tool without an idempotency key, and a tool input
  schema that is not closed and bounded at every depth: every object refuses
  extra properties, every string has a maximum length or a fixed set of
  values, a string with a pattern always has a maximum length, every number
  a range, and `$ref` is refused (hard rule 6); the only conditional is
  an object's `if` (constants on its own properties) with a `then` (names
  of its own properties that become required), admitted in an output
  schema only, so an answer can promise a field when a flag says so; a
  tool's output schema, where it has one, is held to the same rules;
- a decision tool in any agent's allowlist, and an allowlisted tool whose
  name or scope carries a decision word (decide, approve, reject, decline
  or deny, in any inflection): adjusters decide in the Claims Triage App,
  never through a tool (T-31);
- a `job` agent that lists a tool: a job has no run, and a tool server
  binds every call to a run (T-22);
- a purpose without exactly one route, a route candidate of the wrong
  purpose, and a tenant that no route can serve;
- a replay provider other than `replay`, or a replay deployment of a real
  model;
- a `replay` entry that repeats a purpose, names an unknown deployment, a
  deployment of another purpose or one whose provider is not `replay`, a
  routed purpose without a replay entry, a replay deployment among a
  route's candidates, and a replay deployment that does not allow some
  tenant's data class or whose residency that class may not reach (replay
  must serve every tenant, or a test run would refuse one);
- a recorded provider other than `recorded`; a recorded deployment with a
  deployment name, SKU, region, Terraform key or rate limits, a model that
  does not start with `recorded-`, a purpose other than `chat`, or without
  `recorded_from`; a `recorded_from` that names an unknown deployment, a
  replay or recorded one, or one of another purpose; a recorded price (currency,
  input or output) that differs from that deployment's; and `recorded_from` on
  any other deployment;
- a `recorded` entry that repeats a purpose, names an unknown deployment, a
  deployment of another purpose or one whose provider is not `recorded`, a
  recorded deployment among a route's candidates, and a recorded deployment
  that does not allow some tenant's data class or whose residency that class
  may not reach. Unlike replay, recorded need not cover every purpose;
- an Azure deployment without `rate_limits` and a replay deployment with
  them;
- an embedding deployment without `dimensions` (the length of the vectors it
  returns, 1 to 2000, the most pgvector can index in its `vector` type; its
  `halfvec` type indexes up to 4000) and a chat deployment with it;
- `structured_outputs: true` on an embedding deployment, and, once an agent
  declares it, a chat route candidate, the chat replay deployment or the chat
  recorded one that does not (see below);
- an embedding route whose candidates differ from the first in model, version
  or `dimensions`, and a replay embedding deployment whose `dimensions`
  differ from a candidate's: vectors of different models or sizes are not
  comparable, and nothing would fail when one met another (T-54);
- a repeated service ID; a service that calls one that does not exist, calls
  itself or lists a call twice; a service that names a tenant or agent that
  does not exist, and a named tenant that may run none of the agents the
  service names; a tool server of `tools.yaml` without an entry in
  `services.yaml`; a service other than `agent-runtime` that calls a tool
  server; and a graph agent some tenant lists that `agent-runtime` does not
  name, since every call for it would be refused (S055);
- tenants whose rate limits do not fit together: for each route candidate that
  has `rate_limits`, the sum over all tenants of `requests_per_10_seconds`
  and of `tokens_per_minute` must not exceed the candidate's own value, or
  one tenant could use up a deployment's window and cause a provider 429 for
  the others (T-45, and T-55 for the embedding route);
- an Azure deployment that differs from Terraform's outputs, a deployment
  whose `rate_limits.tokens_per_minute` is not Terraform's `capacity` times
  1,000 (an output without `capacity` is not compared), and a deployed
  model that is not registered (T-12).

Replay is a gateway mode, set per deployment in `policies.yaml`, never a
route candidate: a real outage must not be answered with canned text (chat) or
a simulated vector (embeddings). The replay embedding is simulated: a hashed
bag-of-words vector, no model called, and no meaning of the text.

Recorded is a second gateway mode of the same kind, set per deployment in the
`recorded` list of `policies.yaml` (S050): the gateway answers chat from a
committed file of a real model's answers and sends nothing out. Its provider
is `recorded`, inside the platform like `replay`, and its deployment is
labelled `eu-region`. Unlike replay it need not cover every purpose, and it is
never a route candidate either. `recorded_from` names the live deployment that
answered the recordings, and the recorded deployment carries that
deployment's price, so the ledger of a recorded run charges what the live run
was charged. Status: implemented (S050): the gateway's `recorded` mode
starts only in the `test`, `ci` and `local` environments and answers from
`data/evaluation/recordings/`; the response schema is part of the key an
answer is found by, so a recorded chat deployment declares
`structured_outputs` like the deployment that answered.

`evaluation-judge` is the second `job` agent (S050): it grades whether a
rationale is grounded in the text it was drawn from, calls the gateway under
its own name and lists no tool. Only the tenant `evaluation` may run it.

Every tool call is audited by its tool server (T-14, S013), so a tool has no
audit flag to switch off. `approval_required` marks a tool whose effect
waits for a human: a tool server refuses such a tool (S013) until approvals
exist (S015).

A tool whose server exists also declares its `output_schema`. The server
checks every result against it before answering, and the runtime checks
again before the graph sees the result. What each server publishes is
generated from this registry into [`api/mcp/`](../../api/mcp/README.md), and
`make registry` fails when the two differ. Status: implemented (S013,
S046) for `policy-mcp`, `claims-mcp` and `knowledge-mcp`, proven
in-process; the servers run on kind in S044.

`structured_outputs` (default `false`) is declared twice. A chat deployment
sets it when it honours a JSON schema for the answer (Azure OpenAI's
structured outputs); a replay deployment accepts a schema and ignores it
(simulated). An agent sets it when it may send the Model Gateway a response
schema, and the gateway refuses one from an agent that does not. Status:
implemented (S051): the declarations, their check, the gateway's refusal,
and the Azure adapter, which sends the schema as strict structured outputs
(tried live from a laptop on 2026-10-03).

An agent is of kind `graph` unless it says otherwise: the Agent Runtime
runs its graph, and refuses to start when a graph agent has no published
graph (T-40). An agent of kind `job` is a platform job that calls the Model
Gateway under its own name: its calls are attributed to it in the audit
log and the cost metrics, and charged to the tenant's windows and budget.
The runtime loads no graph for it and refuses a run that names it. The one job
is `knowledge-ingestion` (S012), which embeds the policy wordings for the
knowledge store and may run for the tenant `claims-triage` only.

## Services

`services.yaml` maps each workload of the platform to what it may do as a
caller (S055). Status: **implemented**, in tests and on kind: the Agent
Runtime, the Model Gateway and the tool servers read the calling service
from its certificate (`meridian.platform.common.identity` and `peercert`)
and refuse a caller this file does not map, one that does not list them in
`calls`, and a tenant or agent outside the caller's lists. Nothing runs in
Azure.

Each entry has the service's `id` (also its chart name, its ServiceAccount and
the last part of the URI in its certificate), a `description`, `calls` (the
services it may call; a callee refuses a caller that does not list it) and,
for what it names when it calls, `tenants` and `agents` (a callee refuses a
tenant or an agent outside them). All three lists are required; a service that
calls nobody says `[]`. The lists hold what the code names today, no more:

| Service | Calls | Tenant | Agent | Where the code says so |
|---|---|---|---|---|
| `claims-api` | `agent-runtime` | `claims-triage` | `claims-triage` | `ClaimsSettings.tenant`, default `claims-triage` (`MERIDIAN_TENANT` is set nowhere in the chart or `infra/`); `AGENT` in `claims_triage/triaging.py` |
| `agent-runtime` | `model-gateway` and the three tool servers | `claims-triage` | `claims-triage` | `ModelClient` sends the run's own tenant and agent, which are the Claims API's |
| `model-gateway`, `policy-mcp`, `claims-mcp` | nothing | none | none | no service URL in their chart `env` |
| `knowledge-mcp` | `model-gateway` | `claims-triage` | `claims-triage` | `wording_search` embeds under `binding.tenant` and `binding.agent`, the run's |
| `meridian-ingest` | `model-gateway` | `claims-triage` | `knowledge-ingestion` | `--tenant claims-triage` in the chart's `ingest` Job; `INGESTION_AGENT` in `knowledge_mcp/__init__.py` |

The tenant of `knowledge-mcp` follows the Claims API's: if the Claims API's
tenant changes, both entries change. A caller that is not a service (the
evaluation judge names tenant `evaluation` and agent `evaluation-judge`, from a
laptop or CI) has no entry and no certificate.

## Limits

Each tenant has four limits, each with its own job:

| Limit | Window | Enforced | Job |
|---|---|---|---|
| `requests_per_10_seconds` | sliding 10 s | in the gateway process | Azure OpenAI's own request window; keeps one tenant from causing a provider 429 for all (T-45) |
| `tokens_per_minute` | sliding 60 s | in the gateway process | Azure OpenAI's own token window; same job |
| `tokens_per_day` | UTC calendar day | PostgreSQL | the token budget (QA-12, T-15) |
| `cost_per_month_eur` | UTC calendar month | PostgreSQL | the cost quota (C-04) |

An Azure deployment states its own `rate_limits` (`requests_per_10_seconds`
and `tokens_per_minute`) as Azure reports them for the deployment; replay
deployments have none. The first two tenant limits are the same windows, so
the validation above refuses a registry whose tenants could together ask for
more than the smallest candidate of any route allows.

`exchange` in `tenants.yaml` is the planning rate the EUR quota is computed
with. Prices are in USD, the quota is in EUR, and the gateway converts a
call's cost with `usd_per_eur`. It is a planning value with a source and a
date, not a live rate: update it when the figure has moved enough to matter.
Status: implemented (S011, S045). The gateway holds every chat and embedding
request to its tenant's four limits, in replay mode as in live mode, and
answers 429, or 413 for a request larger than `tokens_per_minute` allows at
all. It counts tokens, cost and calls in OpenTelemetry metrics, proven with
an in-memory reader; a dashboard for them is designed (S043).

## Change it

- **A model deployment:** change Terraform first (`infra/terraform/`), apply,
  then `make registry-snapshot` and edit `models.yaml` in the same pull
  request. Prices and retirement dates are verified values with a source and
  a date, never recalled ones.
- **A service:** edit `services.yaml` with the chart's `env` (who calls whom)
  and the code (what each names) in view; validation refuses a tool server
  without an entry.
- **A tool or an agent:** edit `tools.yaml` or `agents.yaml`. A tool that
  changes state has effect `write` and requires an idempotency key.
  `uv run meridian workload new NAME` appends a new workload's agent to
  `agents.yaml` with no tool, and adds it to no tenant: both are edits a
  person makes (T-81). It also writes the agent into the `agents` of
  `agent-runtime` in `services.yaml`, so the runtime may name it; no
  call is admitted before a tenant lists the agent (S061).
- **The models themselves:** edit `src/meridian/platform/registry/`, then
  regenerate the schemas with `uv run meridian registry schemas`.

Endpoints, account names and credentials never go here: the repository is
public, and they reach the platform at deploy time.
