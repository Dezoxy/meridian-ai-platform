# Provider onboarding applied to Azure OpenAI

Status on 2026-10-06 (S034): **designed**, applied once on paper. This is the
[provider onboarding checklist](provider-onboarding.md) filled in for the one
real provider, Azure OpenAI, from the files of this repository on that date.
Nothing contacted Azure to write it: where a row rests on a file, it says
"declared", and a row about Azure's own state needs `make azure-smoke` or
`make registry-snapshot`, which only the owner runs. No tool or gate enforces
the checklist; the rows say which checks already run.

Result: 24 items, **8 met**, **11 partly met**, **5 not met**, 0 not
applicable. Azure OpenAI is registered and routed for synthetic data only
(C-03). It is not cleared for real personal data, and the five items that are
not met include the three that need the insurer's legal function.

## What is true today

- **The foundation is persistent; the compute environment is not built.**
  The Azure foundation (Terraform state, Key Vault, the Azure OpenAI account
  and its deployments, the budget) stays up between demo days, and C-04 keeps
  the compute environment (AKS, PostgreSQL, registry) for a demo day. That
  environment is S020 and does not exist: nothing is deployed to AKS (M2 is
  todo). Meridian's own services run on a local kind cluster and in tests.
  See the [Terraform README](../../infra/terraform/README.md).
- **The subscription is a free trial** created on 2026-09-30, with a
  30-day credit that ends in an upgrade to pay-as-you-go (C-05). On the
  trial, EU quota existed only on regional `Standard` in Sweden Central, so
  there is one account, and the West Europe fallback waits for the upgrade.
- **The gateway has called Azure OpenAI from a laptop** (`make gateway-live`
  and the recording run of S050, 2026-10-03). On kind it answers in replay
  mode and calls no provider; no Meridian service runs in Azure.
- **Data is synthetic only.** The claims-triage tenant carries the class
  `personal` so that it exercises EU routing, and its data is generated.

The three Azure deployments, read from
[`models.yaml`](../../config/registry/models.yaml) on 2026-10-06:

| Deployment | Purpose | Model and version | SKU, region | Label | Retires | Price checked |
|---|---|---|---|---|---|---|
| `aoai-sdc-gpt-4o` | chat | `gpt-4o` 2024-11-20 | `Standard`, `swedencentral` | `eu-region` | 2027-04-14 | 2026-09-30 |
| `aoai-sdc-gpt-4o-b` | chat | `gpt-4o` 2024-11-20 | `Standard`, `swedencentral` | `eu-region` | 2027-04-14 | 2026-09-30 |
| `aoai-sdc-text-embedding-3-large` | embedding | `text-embedding-3-large` 1, 1,024 dimensions | `Standard`, `swedencentral` | `eu-region` | 2028-02-09 | 2026-09-30 |

All three allow the data classes `synthetic`, `internal` and `personal`, and
state the same rate limits: 20 requests per 10 seconds and 20,000 tokens per
minute. The chat route lists the first two in that order, the embedding route
the third.

## The two providers inside the platform

- `replay` is a deterministic stand-in that answers chat with fixed text and
  embeddings with a hashed bag of words; it is simulated, sends nothing out,
  and serves tests and the kind cluster.
- `recorded` answers chat from a committed file of a real model's answers,
  made from `aoai-sdc-gpt-4o`, so CI can replay an evaluation; it sends
  nothing out.

Neither is ever a route candidate, and the process does not apply to them
beyond their registry entry: `meridian registry validate` holds both to the
label `eu-region` and refuses either as a candidate of a route.

## The checklist applied

| ID | Item | Status | Evidence | Note |
|---|---|---|---|---|
| PO-01 | Provider entry | Met | [`providers.yaml`](../../config/registry/providers.yaml), [adapter](../../src/meridian/platform/gateway/providers/azure_openai.py) | The entry `azure-openai` has kind `azure-openai`; the adapter is in the gateway. `make registry` runs in CI |
| PO-02 | Authentication | Partly met | [`openai.tf`](../../infra/terraform/foundation/openai.tf), [Terraform README](../../infra/terraform/README.md#what-make-azure-smoke-proves), [plan, S007](../meridian-plan.md) | Declared: `local_auth_enabled = false`, and `providers.yaml` says Entra ID only. Nothing in tests or CI reads it. One live reading is recorded: the plan's S007 section (started and finished 2026-09-30) has `make azure-smoke` printing "key authentication is off (disableLocalAuth true)". It was read from Azure on that date and not since, and was not run for this document |
| PO-03 | Where secrets live | Partly met | [Threat model](../architecture/security/threat-model.md) (T-18, T-34), [Terraform README](../../infra/terraform/README.md#deliberately-not-here-yet) | No provider key exists: the Key Vault holds no secret and no service reads it. On a laptop the gateway takes a token from the developer's Azure CLI login; workload identity for a service in Azure is S020 and not built. The secret scan is a required check |
| PO-04 | Endpoint | Met | [`settings.py`](../../src/meridian/platform/gateway/settings.py), [settings tests](../../tests/meridian/gateway/test_gateway_settings.py) | No endpoint is committed; the gateway reads one per location from its environment and accepts only `https://oai-meridian-<key>-<suffix>.openai.azure.com` (T-43). Proven for a laptop; the Azure source of the value is S020 |
| PO-05 | Residency label against SKU and region | Met | [`models.yaml`](../../config/registry/models.yaml), [`checks.py`](../../src/meridian/platform/registry/checks.py), [`variables.tf`](../../infra/terraform/foundation/variables.tf) | All three are `Standard` in `swedencentral`, labelled `eu-region`; `Standard` in an EU region is `eu-region` in the validator. Terraform refuses a Global SKU and any region but Sweden Central and West Europe. The mapping is written for Azure's SKUs. T-12 is implemented in part; what it leaves open is the live comparison, which is PO-06, not this item |
| PO-06 | The label against the live provider | Partly met | [Snapshot](../../config/registry/snapshots/terraform-openai-deployments.json), [`python.yml`](../../.github/workflows/python.yml), [threat model](../architecture/security/threat-model.md) (T-12) | CI compares the registry with the committed snapshot, which matches the three deployments. A pull request can change the snapshot with the registry, and the comparison with Azure is a manual `make registry-snapshot`. A live comparison in the pipeline would be S022 (T-12's proposed home; the step's row does not name it) |
| PO-07 | Data classes | Met | [`models.yaml`](../../config/registry/models.yaml), [data classification](../architecture/security/data-classification.md), [registry tests](../../tests/meridian/registry/test_data_classification.py) | Each lists `synthetic`, `internal` and `personal`, none lists `special`. The validator refuses a class its label does not permit; the ceiling is in its code (T-11) |
| PO-08 | Retention and abuse monitoring | Not met | [Threat model](../architecture/security/threat-model.md) (T-20, residual risk), [data classification](../architecture/security/data-classification.md#retention) | The repository records only that the provider may keep prompts for a limited time under its terms, accepted for synthetic data. It records no period, no way to switch it off and no judgement for personal data. Open, and the insurer's legal function's to answer |
| PO-09 | Sub-processors and where prompts go | Not met | [Threat model](../architecture/security/threat-model.md) (T-20) | T-20 names the risk that a sub-processor sees prompts; the repository records no sub-processor and nothing on where else prompts go. Open, and the insurer's legal function's to answer |
| PO-10 | Agreement | Not met | [Constraints](../architecture/requirements/constraints.md) (C-02, C-03) | No agreement with the provider, and no check of where the data goes, is recorded or claimed. C-02 and C-03 say what the project does: personal data in the EU only, and none real in the repository. Open, and the insurer's legal function's to answer |
| PO-11 | Network exposure | Not met | [`openai.tf`](../../infra/terraform/foundation/openai.tf), [`key_vault.tf`](../../infra/terraform/foundation/key_vault.tf), [Terraform README](../../infra/terraform/README.md#deliberately-not-here-yet) | `public_network_access_enabled = true` on the Azure OpenAI account and on the Key Vault; they are protected by Entra ID and RBAC only. Private endpoints and the gateway's egress rule towards the provider are S020's (T-19, T-84) |
| PO-12 | Adapter lives in the gateway only | Met | [`pyproject.toml`](../../pyproject.toml), [import contract tests](../../tests/meridian/test_import_contracts.py) | `openai` and `azure` are forbidden outside the gateway's one adapter, and inside the gateway outside it; `make lint` runs in CI and a test plants violations (T-19). T-19 is implemented in part; what it leaves open is the gateway's egress rule towards the provider, which is PO-11, not this item |
| PO-13 | Adapter behaviour | Partly met | [Adapter](../../src/meridian/platform/gateway/providers/azure_openai.py), [adapter tests](../../tests/meridian/gateway/test_azure_openai_provider.py) | The SDK's own retries are off, redirects and proxy variables are ignored, and a provider's error text stays out of responses and rows. Tested against a mocked Azure and, from a laptop, live. T-45 is implemented in part (a slow provider can still open a circuit for every tenant), so the row is at most partly met |
| PO-14 | Price | Met | [`models.yaml`](../../config/registry/models.yaml) | Each deployment has a retail list price with its source (the Azure Retail Prices API meters) and `checked: 2026-09-30`. A list price, not the invoice (T-47); nothing re-checks the date's age |
| PO-15 | Rate limits | Met | [`models.yaml`](../../config/registry/models.yaml), [`tenants.yaml`](../../config/registry/tenants.yaml) | 20 requests per 10 s and 20,000 tokens per minute each, read from Azure on 2026-10-01 per the comment in the file; the validator compares the tokens per minute with Terraform's capacity and does not compare the requests per 10 s. T-45 is implemented in part; what it leaves open (the token count is an estimate and a 429 stays possible) is about calls, not about whether the declared limits fit, which the validator checks. The three tenants' limits add up to exactly those numbers, so a new tenant needs another's limit lowered |
| PO-16 | Version pin | Met | [`openai.tf`](../../infra/terraform/foundation/openai.tf), [snapshot](../../config/registry/snapshots/terraform-openai-deployments.json) | The pin is declared in Terraform, not read back from Azure: each deployment sets `NoAutoUpgrade`, and the snapshot has no field for it. The registry's model, version, SKU and region equal the snapshot's |
| PO-17 | Retirement | Not met | [`models.yaml`](../../config/registry/models.yaml), [`checks.py`](../../src/meridian/platform/registry/checks.py), [provider onboarding](provider-onboarding.md#when-a-retirement-date-passes) | The dates are recorded (2027-04-14 for both chat deployments, 2028-02-09 for the embedding one) and required. Nothing reads them: a deployment past its date still routes, and `NoAutoUpgrade` means it then stops answering, for both chat candidates on the same day. The plan's backlog has it open with S030 as its home; S030's "done when" does not yet name it |
| PO-18 | Fallback and outage | Partly met | [Provider outage runbook](../operations/runbooks/provider-outage.md), [fallback tests](../../tests/meridian/gateway/test_gateway_fallback.py), [`policies.yaml`](../../config/registry/policies.yaml) | The chat route's second candidate is a second deployment in the same account and region, so it answers a rate limit or a broken deployment and not a regional outage (T-17); the embedding route has no fallback; the West Europe account waits for the upgrade. The residency filter runs before the walk (T-44). The runbook was written from the code and not exercised (S028) |
| PO-19 | Evaluation when the model changes | Partly met | [Evaluation README](../../data/evaluation/README.md), [`recorded.py`](../../src/meridian/platform/gateway/providers/recorded.py), [`compare.py`](../../src/meridian/platform/evaluation/compare.py) | The gate re-grades on a changed prompt, tool contract, recording, screen or golden set. A recording is found by the request alone, and the comparison reads who answered as kind and label, not the model: a model swapped behind an unchanged prompt would replay the old recording and pass. S030 is the nearest step; its row does not name this gap |
| PO-20 | Cost budget | Partly met | [`tenants.yaml`](../../config/registry/tenants.yaml), [Terraform README](../../infra/terraform/README.md#what-the-foundation-creates), [constraints](../architecture/requirements/constraints.md) (C-04) | The tenants' monthly quotas are EUR 10, 5 and 2, EUR 17 in all, against C-04's 60; the ledger refuses a call past a quota (tested against PostgreSQL; unmeasured on a running platform). The subscription budget is EUR 60 with alerts at 50, 80 and 100 %, which detect and do not stop spend; on the trial the spending limit is the hard stop. A demo day's cost is S026 |
| PO-21 | Audit and metrics | Partly met | [Threat model](../architecture/security/threat-model.md) (T-12, T-14, T-49) | Each model call leaves a row with the provider, deployment, SKU, region and label. Those are the registry's facts and not observed on the call, so a wrongly mapped endpoint would still give a confident row; T-14 is implemented in part |
| PO-22 | One pull request | Partly met | [Registry README](../../config/registry/README.md#change-it), [Terraform README](../../infra/terraform/README.md#commands) | The procedure is written and CI compares registry and snapshot, but a pull request can change both, and nothing checks that Terraform, snapshot, registry and a document travel together, and this document cites no step for that check |
| PO-23 | Threat-model rows | Partly met | [Threat model](../architecture/security/threat-model.md) (T-11, T-12, T-18 to T-20, T-43 to T-46) | The rows exist and were compared with the code (S018). The repository does not show that they were written before the code, as the skill asks |
| PO-24 | Exit | Partly met | [Provider onboarding](provider-onboarding.md#removing-a-provider), [Terraform README](../../infra/terraform/README.md#removal) | The steps are written (here, and Terraform's removal in its README) and the registry check compares the registry with the committed snapshot, not with live Terraform: it refuses a registry that drops a deployment the snapshot still lists, and the other direction depends on the owner refreshing the snapshot with `make registry-snapshot` (T-12's residual). Nothing has been run |

## Items not met, and what would meet each

| ID | What would meet it | Step |
|---|---|---|
| PO-08 | The insurer's legal function states the provider's retention and abuse monitoring and whether they suit each data class; this project records the answer | None. The plan has no step, and the answer is not this project's to give |
| PO-09 | The same function lists the sub-processors and where prompts go; this project records them | None |
| PO-10 | The same function records whether an agreement covers sending personal data, and whether where the data goes has been checked | None |
| PO-11 | Private endpoints or address rules on the account, the vault and the state storage, and an egress rule for the gateway towards the provider | S020 (the backlog's proposed home) |
| PO-17 | The gateway or the registry check refuses a deployment past its `retires`, and a successor is onboarded before 2027-04-14 | S030 (the backlog's home for the first); the successor has no step |

Items partly met hold a part that is built and a part that waits: PO-03 for
workload identity in Azure (S020), PO-06 and PO-21 for a live comparison with
Azure (S022, T-12's proposed home; the step's row does not name it), PO-22
for a check that Terraform, snapshot, registry and document travel together
(no step is cited), PO-18 for the West Europe account after the
subscription's upgrade and the game day (S028), PO-19 for S030 (the nearest
step; its row does not name the gap), PO-20 for a demo day's cost (S026),
PO-02 for a reading of Azure's key setting newer than 2026-09-30 (no step is
cited), PO-13 for what T-45 leaves open (see its residual), PO-23 and PO-24
for nothing the plan names.

## What comes next

Mistral (S023, todo) is the next provider this process would be used on. Its
applied document would sit beside this one.
