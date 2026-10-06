# Service acceptance applied to claims triage

Status on 2026-10-06 (S034): **designed**, applied once on paper. This is the
[service acceptance checklist](service-acceptance.md) filled in for the
reference workload, claims triage, from the files of this repository on that
date. No tool or gate enforces the checklist; the rows say which checks
already run. The workload runs on a local kind cluster and in tests, with
synthetic data only. Nothing is deployed to AKS (M2 is todo), and the Azure
foundation holds Terraform state, a Key Vault and the Azure OpenAI
deployments, with no Meridian service on it.

Result: 30 items, **9 met**, **15 partly met**, **6 not met**, 0 not
applicable. No decision has been taken on the items that are not met, so
claims triage is **not called accepted**. It is the reference workload of a
portfolio platform; this document is the first honest list of what stands
between it and acceptance.

## What the evidence is, and is not

- Runbooks were written from the code and not exercised, and the service
  level objectives are proposals nobody has measured
  ([operations](../operations/README.md)).
- There is one maintainer and no review is required on a pull request (T-35),
  so every control that says "in a reviewed diff" means the author's reading.
- The injection suite's detection rate is below QA-09's target and is measured
  with a scripted model that obeys every injection.
- The evaluation baseline is a recording of a real model (`gpt-4o`) replayed in
  CI; the embeddings, and so the wording search, are simulated in every
  evaluation run.
- On kind the gateway answers in replay mode, which is simulated.

A "met" row means the evidence exists in tests, in CI or on the kind cluster.
It never means deployed in Azure.

## The checklist applied

| ID | Item | Status | Evidence | Note |
|---|---|---|---|---|
| SA-01 | Agent registered | Met | [`agents.yaml`](../../config/registry/agents.yaml) | `claims-triage` is a graph agent with `structured_outputs: true`; `knowledge-ingestion` and `evaluation-judge` are `job` agents with no tool. The runtime refuses to start without a published graph (T-40, implemented) |
| SA-02 | Tenant and data class | Met | [`tenants.yaml`](../../config/registry/tenants.yaml) | The tenant `claims-triage` lists `claims-triage` and `knowledge-ingestion`, carries the class `personal` (its data is synthetic) and has four limits: 10 requests per 10 s, 10,000 tokens per minute, 300,000 tokens per day, EUR 10 per month. `evaluation` has the same class; `development` carries `synthetic` |
| SA-03 | Tools declared | Met | [`tools.yaml`](../../config/registry/tools.yaml), [`api/mcp/`](../../api/mcp/README.md), [threat model](../architecture/security/threat-model.md) (T-21) | Six tools on three servers: `policy_lookup`, `claim_history`, `wording_search` and `approval_outcome` read; `add_claim_note` and `request_approval` write. Each has a scope and closed, bounded schemas; the servers publish exactly the registry's contracts and `make registry` fails when they differ |
| SA-04 | Allowlist enforced | Met | [Tool client tests](../../tests/meridian/runtime/test_tool_client.py), [tool server tests](../../tests/meridian/toolserver/test_tool_server.py), [threat model](../architecture/security/threat-model.md) (T-21) | The runtime's client refuses a tool off the agent's list before sending, and each server checks the list again for the run's own agent. Tested in process against PostgreSQL, and the servers run on kind |
| SA-05 | Mutating tools and decisions | Partly met | [`tools.yaml`](../../config/registry/tools.yaml), [`checks.py`](../../src/meridian/platform/registry/checks.py), [threat model](../architecture/security/threat-model.md) (T-23, T-31) | Both write tools require an idempotency key, which the runtime derives; the registry refuses a write tool without one and any decision tool or decision word in an allowlist. No tool carries `approval_required`: the pause is a step of the graph and the adjuster's decision is recorded by the Claims API. T-31 is implemented in part: a tool's effect is declared by its author, so a mislabelled tool under a neutral name passes |
| SA-06 | Tool calls audited | Partly met | [Tool server tests](../../tests/meridian/toolserver/test_tool_server.py), [threat model](../architecture/security/threat-model.md) (T-14) | A tool server writes the audit row in the transaction of the tool's own write, so neither exists without the other. T-14 is implemented in part: a request the MCP SDK rejects before the handler leaves no row |
| SA-07 | Model calls through the gateway | Met | [`pyproject.toml`](../../pyproject.toml), [import contract tests](../../tests/meridian/test_import_contracts.py), [`python.yml`](../../.github/workflows/python.yml) | The graph reaches a model through the runtime's model client and the gateway; the evaluation judge does too, under its own agent. Provider SDKs are forbidden outside the gateway by import-linter, which `make lint` runs in CI |
| SA-08 | No framework in platform packages | Met | [`pyproject.toml`](../../pyproject.toml), [ADR 2](../architecture/decisions/0002-langgraph-behind-a-framework-agnostic-contract.md) | `meridian.platform` may not import `langgraph` or `langchain*`, and a layers contract keeps workloads above the runtime above the platform. Enforced in CI; a test plants violations |
| SA-09 | Callers | Partly met | [`services.yaml`](../../config/registry/services.yaml), [service identity tests](../../tests/meridian/test_service_identity.py), [threat model](../architecture/security/threat-model.md) (T-08) | Seven services have entries with their callees, tenants and agents, each naming `claims-triage` at most; a service the file does not map is refused. On kind and in tests only; T-08 is implemented in part: the identity is a service's, not a person's, until S021 |
| SA-10 | What is sent to a model | Partly met | [`policies.yaml`](../../config/registry/policies.yaml), [gateway routing tests](../../tests/meridian/gateway/test_routing.py), [data classification](../architecture/security/data-classification.md), [threat model](../architecture/security/threat-model.md) (T-11, T-13, T-73) | Personal data routes only to `eu-region` and `eu-zone` deployments, a `global` one first in a route is never reached, and special-category text makes no call; QA-03's contract tests pass. The audit query over a live run is not built, and the redaction and the screens are heuristics that miss national phone forms, Hungarian identifiers and other names (T-73, S067 in part) |
| SA-11 | Synthetic data only | Partly met | [Generator tests](../../tests/synthetic/test_reproducibility.py), [data classification](../architecture/security/data-classification.md), [threat model](../architecture/security/threat-model.md) (T-04) | The golden set, policies and wordings come from a seeded generator whose reruns are identical, and the pages carry a banner. The banner asks; nothing stops a person typing real data, so the pages stay off the public internet |
| SA-12 | Human oversight | Partly met | [Evaluation README](../../data/evaluation/README.md), [quality attributes](../architecture/requirements/quality-attributes.md) (QA-06), [threat model](../architecture/security/threat-model.md) (T-30, T-33) | The absolute graders fail CI on any approval over the threshold or with a fraud flag and any rejection without a person; on the recorded run route and oversight are 40 of 40. T-30 is implemented in part: an automatic approval within the threshold rests on the model's one answer "none applies" (T-26), and nothing measures a real model's resistance |
| SA-13 | People are identified | Not met | [Threat model](../architecture/security/threat-model.md) (T-05, T-32, T-69) | No sign-in exists: anyone who reaches the Claims API acts as an adjuster, and who decided is not recorded. Entra ID sign-in, roles and the tenant from the token are S021 |
| SA-14 | Audit trail | Partly met | [Quality attributes](../architecture/requirements/quality-attributes.md) (QA-05), [threat model](../architecture/security/threat-model.md) (T-14, T-32) | Every state change, an adjuster's decision among them, is an audit event in the transaction of the change, and the adjuster's page reads one filtered view of it. The reconciliation of QA-05 is not built, the decision names no person (S021), and the audit search is S033 |
| SA-15 | Evaluation gate | Met | [Evaluation README](../../data/evaluation/README.md), [baseline](../../data/evaluation/claims-triage-baseline.json), [`python.yml`](../../.github/workflows/python.yml), [threat model](../architecture/security/threat-model.md) (T-72) | 40 synthetic golden claims, a manifest that names the workload, six fingerprints and a baseline that CI compares (`meridian eval compare`). Limits: one model version, simulated embeddings, and a hand-written recording with a regenerated baseline would pass CI; only reading the diff stops it, and no review is required (T-35) |
| SA-16 | Injection suite | Not met | [Injection summary](../../data/evaluation/injection-summary.md), [quality attributes](../architecture/requirements/quality-attributes.md) (QA-09), [threat model](../architecture/security/threat-model.md) (T-26, T-83) | 24 of 66 attacks were stopped before the model, 36 % against QA-09's 90 %; 16 of 22 look-alike sentences were flagged. No tool outside the allowlist was called in 94 cases, which meets that half. The model is a script that obeys every injection (simulated); 30 of the 42 attacks that reached it turned a referral into an automatic approval within the threshold. What a real model does is unmeasured |
| SA-17 | Threat-model note | Partly met | [Threat model](../architecture/security/threat-model.md) | Rows exist for the tools, the approvals, the tenant data, the routing and the registry, and each was compared with the code for version 1 (S018). The repository does not show that each was written before the code it covers |
| SA-18 | SLOs | Partly met | [SLOs](../operations/slo.md) | Eight objectives: five with an indicator the kind cluster's Prometheus can compute, three (`triage-latency`, `gateway-overhead`, `triage-completion`) designed with no metric. Every target is a proposal nobody has measured; thresholds come from a load test (S027) |
| SA-19 | Alerts and dashboards | Partly met | [Alert rules](../../infra/kind/alerts/meridian.yaml), [rule tests](../../tests/meridian/test_alert_rules.py), [dashboards](../../infra/kind/dashboards/), [operations](../operations/README.md#alerts) | Twelve rules checked offline by `make alerts` in CI; the cost dashboard is on kind and the certificate rules were seen loaded and inactive there (2026-10-05). The health dashboard and the rest were not seen on a cluster, and nothing is notified: kind runs no Alertmanager (S028). T-86 is implemented in part |
| SA-20 | Runbooks written | Met | [Runbooks](../operations/README.md#runbooks), [runbook query test](../../tests/meridian/test_runbook_queries.py) | Six: provider outage, budget exhaustion, database failure, rollback, secret rotation, certificate expiry. A test runs each query in a read-only transaction. "Written" is all that is claimed here |
| SA-21 | Runbooks exercised | Not met | [Operations](../operations/README.md) | None has been exercised: each says it was written from the code. S022 exercises the rollback, and the game day (S028) the outage, the budget and the database |
| SA-22 | Chart | Partly met | [Chart](../../infra/helm/meridian/), [chart tests](../../tests/meridian/test_helm_chart.py), [threat model](../architecture/security/threat-model.md) (T-85) | One hardened chart runs the six services, the Jobs and the sweep on kind: probes, limits, non-root user, read-only root, a pinned image. Each service has one replica, so the disruption budgets protect nothing (C-01); nothing is in Azure, and image scanning and signing are S022 |
| SA-23 | Network isolation | Partly met | [Network policies](../../infra/helm/meridian/templates/networkpolicy.yaml), [smoke network test](../../tests/meridian/test_smoke_network_policy.py), [threat model](../architecture/security/threat-model.md) (T-84) | A default-deny policy and one allow policy per workload; on the cluster each pod reached exactly its policy's destinations and `make smoke` proves a blocked path on every run, which CI does not run. Implemented on kind only: the other namespaces have no policy (S063) and the gateway's rule towards a provider is S020 |
| SA-24 | Service identity and certificates | Partly met | [Certificates](../../infra/helm/meridian/templates/certificates.yaml), [ADR 4](../architecture/decisions/0004-prove-service-identity-with-mutual-tls.md), [certificate runbook](../operations/runbooks/certificate-expiry.md) | Mutual TLS between the five called services on kind, an issuer that signs only for `meridian`, and four alerts on expiry; tested over real TLS. The edge and the Claims API are plain HTTP and nobody signs in (S020, S021), and a restart on renewal was not seen on the cluster |
| SA-25 | Cost budget per tenant | Met | [`tenants.yaml`](../../config/registry/tenants.yaml), [budget tests](../../tests/meridian/gateway/test_gateway_budgets.py), [quality attributes](../architecture/requirements/quality-attributes.md) (QA-12, QA-07) | Each tenant has a daily token budget and a monthly cost quota, reserved before the call and tested against PostgreSQL with racing calls; the three quotas add up to EUR 17 against C-04's 60. Unmeasured on a running platform; a demo day's cost is S026 |
| SA-26 | Rollback | Not met | [Rollback runbook](../operations/runbooks/rollback.md) | The runbook is written from the deploy script and the chart; no script performs a rollback and none has been run. S022 exercises it |
| SA-27 | Recovery of data | Not met | [Quality attributes](../architecture/requirements/quality-attributes.md) (QA-10), [database failure runbook](../operations/runbooks/database-failure.md) | The runbook says the kind database is one instance with no backup, and no restore has been run: QA-10 is unmeasured and `make down` removes the data. The drill is S029 |
| SA-28 | Retention | Not met | [Data classification](../architecture/security/data-classification.md#retention), [threat model](../architecture/security/threat-model.md) (T-49) | `audit.events` and `gateway.usage` have no retention rule; the periods are the owner's to choose when S065 opens, and a production period would come from the insurer's retention schedule, which is the insurer's and is not answered here |
| SA-29 | Capability labels | Partly met | [README](../../README.md#what-it-does-and-what-is-real-today), [constraints](../architecture/requirements/constraints.md) (C-07) | The capability table labels each row, with caveats such as "laptop only" and "simulated". Nothing reads the labels (`make docs` does not), and the sweep of every capability is part of S035 |
| SA-30 | A second workload | Partly met | [Scaffold](../../src/meridian/platform/cli/workload.py), [first-run test](../../tests/meridian/cli/test_workload_first_run.py), [threat model](../architecture/security/threat-model.md) (T-09, T-81) | `meridian workload new NAME` writes a graph of one node, an evaluation with no grader, an empty golden set, two entry points, an agent and the runtime's entry. Still by hand: the agent in a tenant's list, its tools, a prompt, synthetic cases and graders, and for an API of its own an entry in `services.yaml` and a chart entry with a certificate. No generated workload has run in a cluster, and T-09 holds only while a workload is this repository's own reviewed code |

## Items not met, and what would meet each

| ID | What would meet it | Step |
|---|---|---|
| SA-13 | Sign-in against Entra ID, the four roles, the tenant resolved from the token, the decision recorded with who made it | S021 (needs S020) |
| SA-16 | A measured detection rate that meets QA-09, or a recorded decision to change the target, and a real model's answers to the cases | None for the rate; S067 changes what the screen reads. No step measures a real model |
| SA-21 | The rollback run, then the outage, budget and database runbooks run on a game day, each with a record | S022, S028 |
| SA-26 | A rollback run against a release and a registry change | S022 |
| SA-27 | A backup kept outside the environment and a restore into a scratch environment, timed | S029 |
| SA-28 | A retention rule for `audit.events` and `gateway.usage` with periods the owner chooses; the insurer's schedule is the insurer's | S065 |

## Items partly met, and what is left

| ID | What is left | Step |
|---|---|---|
| SA-05 | A tool's effect rests on its author's label | None |
| SA-06 | A request the SDK rejects leaves no row | None |
| SA-09, SA-24 | Identity of a person; TLS at the edge; Azure | S021, S020 |
| SA-10 | Redaction and screens are heuristics; the audit query over a live run | S067 (in part); none for the rest |
| SA-11 | Nothing stops real data typed into a form | None; the pages stay off the public internet (T-01) |
| SA-12 | A real model's resistance to "none applies" | None |
| SA-14 | Who decided; the QA-05 reconciliation; an audit search | S021, S033; none for the reconciliation |
| SA-17 | Writing the note before the code is a habit, not a check | None |
| SA-18 | Measuring the objectives and the three designed ones | S027, S064 |
| SA-19 | Seeing the rules and dashboards on a cluster; a rule on missing data; notification | S064, S028 |
| SA-22, SA-23 | Azure; the namespaces outside `meridian`; image controls | S020, S063, S022 |
| SA-29 | A sweep of every capability's label | S035 |
| SA-30 | A second workload actually built and run | S037 is the plan's second-framework workload; no step for the by-hand list |
