# Governance

Two processes that the scope promises: how a model provider is onboarded, and
when a workload counts as accepted on the platform. Status on 2026-10-06
(S034, S037): both are **designed**. They are written, and the provider process
was applied once and the acceptance process twice, on paper, to what the
repository holds today. No tool or gate runs them; where a check that already
exists enforces an item (`meridian registry validate`, the import contracts,
the evaluation gate, the secret scan), the checklist names it and every other
item rests on a person reading the document.

| Document | What it holds | Status |
|---|---|---|
| [Provider onboarding](provider-onboarding.md) | The process for adding, changing, retiring and removing a model provider or a deployment of one, and a checklist of 24 items (`PO-01` to `PO-24`) | Designed |
| [Provider onboarding applied to Azure OpenAI](provider-onboarding-azure-openai.md) | The checklist applied to the one real provider: 8 met, 11 partly met, 5 not met | Designed, applied once on paper |
| [Service acceptance](service-acceptance.md) | The process by which a workload is called accepted, and a checklist of 30 items (`SA-01` to `SA-30`) | Designed |
| [Service acceptance applied to claims triage](service-acceptance-claims-triage.md) | The checklist applied to the reference workload: 9 met, 15 partly met, 6 not met | Designed, applied once on paper; the workload is not called accepted |
| [Service acceptance applied to the claim brief](service-acceptance-claim-brief.md) | The checklist applied to the second workload, on the second agent framework ([ADR 9](../architecture/decisions/0009-run-a-second-agent-framework-behind-the-same-host-protocol.md)): 6 met, 15 partly met, 9 not met, from tests and one run on kind under replay (what was not seen is listed in the document) | Designed, the second applied example on paper; the workload is not called accepted |

The applied documents are mostly not green on purpose. The platform runs on a
local kind cluster and in tests with synthetic data; the Azure compute
environment is not built (M2); the runbooks were not exercised; the
objectives are proposals nobody measured; there is one maintainer and no
review is required on a pull request (T-35). A row says "met" only for
evidence in tests, in CI or on kind, and never for Azure.

## How they relate to the rest

Each fact has one owner here, and these documents link to it instead of
copying it.

| Source | Owns | Used by |
|---|---|---|
| [The registry](../../config/registry/README.md) | Providers, deployments, labels, prices, retirement dates, agents, tools, tenants, services | Both checklists; `meridian registry validate` enforces part of them |
| [The threat model](../architecture/security/threat-model.md) | The risks and their status; T-20 sends a provider to the onboarding checklist | Both |
| [Data classification](../architecture/security/data-classification.md) | The classes, the inventory and the retention statement | Both |
| [Constraints](../architecture/requirements/constraints.md) and [quality attributes](../architecture/requirements/quality-attributes.md) | The fixed limits (C-02 to C-05) and the targets, with their measurements | Both |
| [Operations](../operations/README.md) | The objectives, the alerts and the runbooks | Service acceptance, provider onboarding (outage, rotation) |
| [The evaluation files](../../data/evaluation/README.md) | What the gate grades and what is simulated | Service acceptance, provider onboarding (a model change) |
| [The Terraform README](../../infra/terraform/README.md) | The Azure foundation, its subscription and its removal | Provider onboarding |

The regulatory statements of this repository are in the constraints (C-02),
the [scope](../architecture/overview/02-scope.md) and the data
classification. These documents add none. Where an item needs a legal
judgement (what a provider retains, an agreement, where data goes, a
retention period), it is the insurer's legal function's to answer, and the
applied documents record it as open.

## Where they are not yet used

Mistral (S023, todo) is the next provider the onboarding process would be
used on. The acceptance checklist has been applied to two workloads, claims
triage and the claim brief (S037); a third would use it again, and the scaffold
that starts one is `meridian workload new` (it writes a workload on the first
host, LangGraph).
