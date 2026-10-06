# Service acceptance

Status on 2026-10-06 (S034): **designed**. This is a written process and a
checklist of 30 items for a workload that wants to be called accepted on the
platform. It was applied once, on paper, to the reference workload in
[the claims-triage document](service-acceptance-claims-triage.md). No tool or
gate runs it. Some items are enforced by a check that already runs; the last
column of the checklist names it. The others rest on a person reading this
document and writing the answer down.

## What is accepted

A **workload** is a use case built on the platform: its agent, its graph, its
tools, its tenant, its golden set and its pages or API. Today there is one,
claims triage. "Accepted" means that the checklist was applied to the workload
in an applied document of this folder, that every item has a status and an
evidence link, and that the owner decided on each item that is not met.

It does not mean that the workload runs in production. The platform runs on a
local kind cluster and in tests, with synthetic data only; the Azure compute
environment is not built (S020, M2). An accepted workload is accepted for
that platform, and the applied document says which items would change in
Azure.

## Who decides

The maintainer decides, as there is one (C-01), and a pull request needs no
review (T-35). A session prepares the applied document and the evidence; it
does not decide. The checks and this checklist are the gate. Where an item
needs a legal judgement (a retention period, whether personal data may be
used for a purpose), it is the insurer's legal function's to answer, and this
project records it as open.

## The process

1. **Threat-model note first.** For the security-relevant parts of the
   workload (tools, approvals, tenant data, provider routing, the registry),
   run the `feature-threat-model` skill before building, and add rows to the
   [threat model](../architecture/security/threat-model.md).
2. **Scaffold.** `uv run meridian workload new NAME` writes the package, the
   two entry points, the agent and its entry for the Agent Runtime in
   `services.yaml`. It declares and never grants. SA-30 lists what stays by
   hand.
3. **Build with the gates running.** The registry check, the import
   contracts, the tests and the evaluation gate run in CI on every pull
   request.
4. **Apply the checklist.** Copy the table below into a document named
   `service-acceptance-<workload>.md` in this folder, give every item a
   status, an evidence link and a note, and say at the end which items are
   not met and which step would meet each.
5. **Decide.** The owner reads the items that are not met and decides, per
   item: accepted for now with a reason and what would reopen it, or a step
   that must come first. The workload is called accepted only when none is
   left undecided.
6. **Apply again** when something the evidence rests on changes materially:
   a new tool or tool scope, a new data class, a new route or provider, a
   change of the prompt or the golden set, or a deployment to a new
   environment. An item the change does not touch is not redone, and the
   document's date moves only for what was checked.

The four statuses are the ones of the
[provider onboarding process](provider-onboarding.md#what-onboarded-means):

- **Met**: the evidence exists in the repository for what runs today, in
  tests, in CI or on the kind cluster. It never means deployed in Azure.
- **Partly met**: part of the control is built or shown; the note says which.
- **Not met**: the evidence does not exist; the note says why and, where the
  plan has one, the step that would meet it.
- **Not applicable**: the item does not apply to this workload; the note says
  why.

When a threat-model row for the item says "implemented in part", the item is
at most partly met: the register owns that label and this document follows it.

## Withdrawing acceptance

Acceptance is withdrawn by a new version of the applied document that gives an
item a worse status, with the date and the reason. The document is kept as
history; a reader sees the earlier one in git. Nothing in the platform reads
an acceptance: the registry does not refuse a tenant for lack of it, so
withdrawing it is a statement and not a switch.

## The checklist

"Enforced by" names the check that already runs. Where it says a person, the
item rests on someone reading this document; no tool stops a pull request that
skips it.

| ID | Item | What must be shown | Enforced by |
|---|---|---|---|
| SA-01 | Agent registered | The workload's agent is in `agents.yaml` with its kind (`graph` or `job`) and its description | `meridian registry validate`; the runtime refuses to start when a graph agent has no published graph (T-40) |
| SA-02 | Tenant and data class | A tenant in `tenants.yaml` lists the agent, carries the data class of what it sends and has four limits; a route can serve its class | `meridian registry validate` refuses a tenant no route can serve and limits that do not fit together; the class itself is a person's choice |
| SA-03 | Tools declared | Every tool the agent may call is in `tools.yaml` with an effect, a scope, and closed and bounded input and output schemas (hard rule 6) | `meridian registry validate` |
| SA-04 | Allowlist enforced | The agent calls only the tools on its allowlist, and the runtime and each tool server both refuse any other (T-21) | Tests of the runtime's tool client and the tool servers; `meridian registry validate` for the allowlist's contents |
| SA-05 | Mutating tools and decisions | A tool that writes needs an idempotency key; no tool decides; where a person must decide, the workload pauses for them and the decision is recorded by the application, never by a tool (hard rule 6, T-23, T-31) | `meridian registry validate` refuses a write tool without a key, a decision tool in an allowlist and a tool whose name or scope carries a decision word; that the design leaves the decision to a person is a person's check |
| SA-06 | Tool calls audited | Every tool call leaves an audit record, written with the tool's own write (T-14, QA-05) | Tests of the tool servers against PostgreSQL |
| SA-07 | Model calls through the gateway | The workload reaches a model only through the Model Gateway (hard rule 4, ADR 3) | import-linter, in `make lint` in CI |
| SA-08 | No framework in platform packages | Nothing under `src/meridian/platform/` imports `langgraph` or `langchain*`, and the workload's graph code stays above the platform (hard rule 5, ADR 2) | import-linter, in `make lint` in CI |
| SA-09 | Callers | The workload's services have entries in `services.yaml`: who may call whom, and which tenants and agents each may name | `meridian registry validate`; the services refuse a caller the file does not map (tests) |
| SA-10 | What is sent to a model | The data classes and the residency labels of what the workload sends are known; every call goes only to a deployment its class may reach; special-category text makes no call (hard rule 3, T-11, T-13, QA-03) | Gateway tests (the filter, the refusal, the audit); the screens and the redaction are heuristics with their own tests (T-73) |
| SA-11 | Synthetic data only | Every policy, claim, document and evaluation case comes from the seeded generator (hard rule 2, C-03) | Reproducibility tests of the generator; the secret scan; nothing stops a person typing real data into a form (T-04) |
| SA-12 | Human oversight | No material decision is made without a person (C-02, QA-06): no approval over the threshold or with a fraud flag, no rejection on the merits; a human decision is an explicit act (T-30, T-33) | The evaluation gate's absolute graders in CI; the adjuster's page is checked by its tests |
| SA-13 | People are identified | A person who decides or reads is identified, and their role is checked (T-05, T-32, T-69) | A person; nothing |
| SA-14 | Audit trail | A decision can be reconstructed from the audit records: who, what, on which proposal, when (T-14, QA-05) | Tests against PostgreSQL; the reconciliation of QA-05 is not built |
| SA-15 | Evaluation gate | A golden set with a manifest that names the workload, a committed baseline and a CI comparison that fails on a regression, a broken absolute rule or a changed fingerprint (T-72) | `meridian eval compare` in CI |
| SA-16 | Injection suite | Injection cases are measured; no tool outside the allowlist and no route changed by injected text; detection against QA-09's target (T-26, T-27, T-83) | `meridian eval compare` in CI against the committed baseline; the suite has no target of its own |
| SA-17 | Threat-model note | The threats of the workload's security-relevant parts are rows in the register, written before they were built | `make docs` checks that a cited `T-NN` exists; the rest a person |
| SA-18 | SLOs | Objectives for what the workload promises, with an indicator that can be computed and a target that was measured | A person |
| SA-19 | Alerts and dashboards | Alert rules and dashboards for the workload exist as code, are tested, and are seen working on a cluster | `make alerts` in CI (the checker and the rules' unit tests); tests on the dashboards; seeing them on a cluster is a person's |
| SA-20 | Runbooks written | A runbook exists for each way the workload fails that an operator can act on | A test runs every query of the runbooks in a read-only transaction; the rest a person |
| SA-21 | Runbooks exercised | Each runbook has been run against a failure, and the record says what happened | A person |
| SA-22 | Chart | The workload's services run from a hardened chart: probes, limits, non-root, read-only root, a pinned image | `make helm-lint` and the chart's tests in CI |
| SA-23 | Network isolation | A default-deny network policy and one allow policy per workload; a path no rule allows is proved blocked | The chart's tests in CI; `make smoke` on a kind cluster, which CI does not run |
| SA-24 | Service identity and certificates | Each service that is called proves its caller by certificate; certificates renew, and an alert fires before one ends | Tests over real TLS in CI; `make smoke` on kind |
| SA-25 | Cost budget per tenant | Each tenant has a monthly cost quota and a daily token budget, enforced before the call (QA-12, C-04) | The ledger (tests against PostgreSQL); `meridian registry validate` for limits that fit |
| SA-26 | Rollback | A release or a registry change can be undone by a procedure that has been run | A person |
| SA-27 | Recovery of data | The workload's data can be restored from a backup kept outside the environment, within a stated time and loss (QA-10) | A person |
| SA-28 | Retention | The workload's stored data has a retention rule per store (data classification, T-49). A period is the insurer's to choose | A person |
| SA-29 | Capability labels | Every capability of the workload is labelled implemented, simulated or designed in the README (hard rule 7, C-07) | A person; `make docs` does not read the labels |
| SA-30 | A second workload | What a second workload would need is written, and the scaffold does it as far as it can | The scaffold's tests (`meridian workload new` in a copy of the tree); what stays by hand is a person's |

## Which source owns what

This folder does not copy facts. The registry owns agents, tools, tenants and
services; the [threat model](../architecture/security/threat-model.md) owns
the risks; the [quality attributes](../architecture/requirements/quality-attributes.md)
own the targets and say which are measured; the
[operations documents](../operations/README.md) own the objectives, the alerts
and the runbooks; the [evaluation README](../../data/evaluation/README.md)
owns what the gate grades. An applied document links to them and says what
they show.

The regulatory statements of this repository are in the
[constraints](../architecture/requirements/constraints.md) (C-02), the
[scope](../architecture/overview/02-scope.md) and the
[data classification](../architecture/security/data-classification.md). This
document adds none.
