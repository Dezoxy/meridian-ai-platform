# Platform registry

The declarative source of truth for the platform: which models exist, which
data may reach them, which tools exist and which agent may call them, and
which tenant runs which agent. Status: **implemented** (S008) as validated
configuration; the Model Gateway and the Agent Runtime load it at startup
(S009), and S010 adds routing by it. The design is in the plan's S008
section and in
[ADR 3](../../docs/architecture/decisions/0003-build-a-thin-model-gateway.md).

## Files

| File | Holds |
|---|---|
| `providers.yaml` | Provider accounts: Azure OpenAI and the replay provider |
| `models.yaml` | Model deployments: model, version, SKU, region, residency label, allowed data classes, price, retirement date |
| `tools.yaml` | MCP servers and their tools: effect, scope, input schema, idempotency |
| `agents.yaml` | Agents and their tool allowlists |
| `policies.yaml` | Data classes with the residency labels they allow, the ordered routes per purpose, and the replay deployment per purpose |
| `tenants.yaml` | Tenants with their data class and the agents they may run |
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
  `GlobalStandard` is `global`, replay is `eu-region`;
- a deployment that allows a data class its label does not permit, so
  personal data never reaches a `global` deployment (hard rule 3). The
  ceiling per class is fixed in the validator's code as well as in
  `policies.yaml`, so widening it means changing the check itself;
- a mutating or decision tool without an idempotency key, and a tool input
  schema that is not closed and bounded at every depth: every object refuses
  extra properties, every string has a maximum length or a fixed set of
  values, every number a range, and `$ref` is refused (hard rule 6);
- a decision tool in any agent's allowlist, and an allowlisted tool whose
  name or scope carries a decision word (decide, approve, reject, decline
  or deny, in any inflection): adjusters decide in the Claims Triage App,
  never through a tool (T-31);
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
- an Azure deployment that differs from Terraform's outputs, and a deployed
  model that is not registered (T-12).

Replay is a gateway mode, set per deployment in `policies.yaml`, never a
route candidate: a real outage must not be answered with canned text.

Every tool call will be audited (T-14, from S013), so a tool has no audit
flag to switch off. `approval_required` marks a tool whose effect waits for a human; the
runtime and the MCP servers enforce it (S013, S015). Tenant budgets and
quotas arrive with S011.

## Change it

- **A model deployment:** change Terraform first (`infra/terraform/`), apply,
  then `make registry-snapshot` and edit `models.yaml` in the same pull
  request. Prices and retirement dates are verified values with a source and
  a date, never recalled ones.
- **A tool or an agent:** edit `tools.yaml` or `agents.yaml`. A tool that
  changes state has effect `write` and requires an idempotency key.
- **The models themselves:** edit `src/meridian/platform/registry/`, then
  regenerate the schemas with `uv run meridian registry schemas`.

Endpoints, account names and credentials never go here: the repository is
public, and they reach the platform at deploy time.
