# Meridian AI Platform — Plan

> **Status:** bootstrap, 2026-09-30. The architecture model, the first
  decisions, the engineering harness, a local platform on kind and the Azure
  foundation exist; no platform service does yet.
> **How to use this file:** this is the single living plan. Every step in
  Part B has an ID (`S001`…). When a step starts, add a `### S0xx` section
  under Part C from the template, flip its status, and fill it in as you go.
  Nothing gets deleted; superseded decisions are struck through with a note.
> **Architecture:** requirements, decisions, views and security live in
  [architecture/](architecture/README.md). This plan owns the step list, the
  session protocol, open questions and its own changelog.
> **Private context:** job targeting and owner notes live in the gitignored
  `.context/` folder. Never copy them into tracked files; this repository is
  public.

---

## Part A — How a session works

One step per session. Short sessions are cheaper and safer: the advisor
re-reads the whole transcript on every call, uncached, and a long context
blurs what the step was for.

1. **Start small.** Read `CLAUDE.md`, the step table below and the detail
   section of the step you take. Read other files only when the step needs
   them; search for the symbol, then read the lines.
2. **One step, one branch.** Branch `sNNN-short-name` off `main`; never stack
   branches. If a step will not fit one session, split it here first.
3. **Open the step.** Add its section to Part C from the template and set the
   status to `doing`.
4. **Plan, delegate, verify.** The main session (Opus) writes a short contract
   with paths, names and what not to touch, delegates implementation to the
   `implementer` subagent (Sonnet at high effort), and consults the advisor
   before committing to an approach and before declaring done. It runs every
   gate itself; a subagent's report is a claim, not evidence.
5. **Gates.** Always `make docs` and `make test`. `make check` when the model
   changed, `make mermaid` when views or Mermaid blocks changed, and the
   step's own "done when" criterion.
6. **Close.** Fill in the work log and verification, set `done`, commit, open
   the PR, merge after CI is green, and confirm the content landed on `main`.
   Start the next step in a new session.

Cost rules:

- Docker-heavy targets (`make pdf`, `make mermaid-render`) run when their
  inputs changed, not on every step.
- Broad searches go to an Explore subagent, which returns conclusions instead
  of file dumps.
- The Azure environment exists only on demo days (C-04).
- Ask the owner only when an answer changes the design, the cost or a
  security boundary; otherwise decide, and record the decision in the step.

## Part B — Roadmap and step list

Status legend: `todo` · `doing` · `done` · `blocked` · `dropped`

Each step is sized for one focused session of two to four hours. Dependencies
are the step IDs in the last column. Every capability stays labelled
implemented, simulated or designed (C-07).

### Why this order

- **Walking skeleton early.** S009 connects claims API, runtime and gateway
  end to end before any layer is deep. From then on the demo always works,
  and each later step deepens one layer without breaking it.
- **Foundations before consumers.** The workspace and CI gates (S002), the
  synthetic data (S003) and the registry (S008) come before the services that
  read them.
- **Risk registers before risky code.** Threat model and data classification
  (S004) exist before the gateway handles its first real request (S010).
- **Spend late.** Cloud cost starts with the Azure foundation (S007) and stays
  small until M2 creates the full environment.

### Developer CLI

A thin `meridian` command grows with the steps that need it; it is not a
step of its own. Typer: commands from type hints, the same idiom as FastAPI
and Pydantic, at the cost of one dependency.

- **What it owns.** Platform work an agent developer does before opening a
  pull request. `make` keeps environment lifecycle (kind, the demo, the
  documentation gates); Terraform and Helm keep infrastructure.
- **One entry point.** CI runs the same command a developer runs, so a check
  that passes locally passes in CI. S008 adds `registry validate`; S017 adds
  `eval run` and `eval compare`; S039 would add `workload new`.
- **Boundary.** The CLI never approves, rejects or changes a claim; adjuster
  decisions stay in the UI, where they are audited (C-02). Commands that call
  the platform APIs, such as run inspection or audit search, need an Entra
  sign-in and stay designed until S021 exists.
- **Cost.** Evaluation uses the replay provider unless `--live` is passed
  (C-04).
- **Placement.** `src/meridian/platform/cli/`, importing only platform
  packages. The Evaluation Harness reaches workloads through the runtime
  API, so the import contract from S002 covers the CLI too.

### Demo checkpoints

| After | What can be shown |
|---|---|
| S009 | A claim flows through API, runtime and gateway, visible as one trace |
| S015 | A triage proposal pauses for an adjuster and resumes on the decision |
| S017 | An evaluation report comparing two prompt versions |
| S018 | The full fifteen-minute demo on kind, from a clean checkout |
| S026 | The same demo on AKS, recorded, with the run's cost logged |
| S028 | An incident record written from a real game day |

### M0 — Bootstrap

| ID | Step | Done when | Status | Depends |
|---|---|---|---|---|
| S000 | Plan, harness and architecture bootstrap | Model with five views, ADRs 1 to 3, constraints C-01 to C-07, harness (rules, skills, reviewers, hooks), Mermaid tooling; `make docs`, `make test` and `make check` pass | done | — |
| S001 | Commit and publish | First commit on `main`; public GitHub repository; docs CI green on GitHub; the README's derived diagram renders on GitHub; architecture-base Mermaid PR merged; agent-base `yarn.lock` reverted | done | S000 |
| S002 | Python workspace and CI gates | `pyproject.toml` uv workspace with empty ~~`src/platform` and `src/workloads`~~ `meridian.platform` and `meridian.workloads` packages under `src/meridian/` (see S002 decisions); ruff, pytest, an import-linter contract (no `langgraph` or `langchain` under `meridian.platform`) and gitleaks run in CI; a deliberate framework import in a platform package fails CI | done | S001 |
| S003 | Synthetic data and golden set | A seeded generator under `data/synthetic/` produces policies, policy-wording documents and first-notice-of-loss claims with labelled expected outcomes; a rerun produces identical output; no real names or documents | done | S002 |
| S004 | Security and quality registers | `security/threat-model.md` with T-IDs per trust boundary, `security/data-classification.md` with the data classes, `requirements/quality-attributes.md` with targets marked unmeasured; all symlinked into `overview/`; `make docs` resolves every cited ID | done | S001 |
| S005 | Agent framework spike | A three-step flow with an approval pause in Microsoft Agent Framework under `spikes/`, with notes; a decision matrix appended to ADR 2 | done | S002 |
| S006 | Local platform on kind | `make up` creates a kind cluster with ingress, PostgreSQL with pgvector, OpenTelemetry Collector, Prometheus, Grafana, Tempo and Loki from pinned Helm charts; a test trace appears in Grafana; `make down` removes it | done | S002 |
| S007 | Azure foundation | Terraform with remote state, a resource group, a budget with 50, 80 and 100 % alerts (C-04), Key Vault, and Azure OpenAI ~~`gpt-4.1-mini` plus `text-embedding-3-large` on DataZoneStandard in Sweden Central with a West Europe fallback~~ `gpt-4o` plus `text-embedding-3-large` on regional Standard in Sweden Central, with the West Europe fallback after the subscription upgrade (see S007 decisions); plan reviewed; apply confirmed by the owner | done | S001 |

### M1 — Claims triage on kind

| ID | Step | Done when | Status | Depends |
|---|---|---|---|---|
| S008 | Platform registry | `config/registry/` YAML for models, providers, tools, agents, policies and tenants, with JSON Schemas; every deployment carries a residency label and allowed data classes; validated in CI; seeded for the claims workload; `meridian registry validate` is the check developers and CI both run | todo | S002 |
| S009 | Walking skeleton | A claim posted to the claims API starts a one-node LangGraph run that calls the gateway's replay provider and stores a decision; one trace spans API, runtime and gateway in Tempo; `make demo` runs it on kind | todo | S006, S008 |
| S010 | Gateway routing and resilience | Registry-driven routing by data class and residency; Azure OpenAI adapter; timeout, retry, circuit breaker and fallback to the second region; a residency mismatch is refused and audited; contract tests pass | todo | S004, S007, S009 |
| S011 | Gateway budgets and cost | Per-tenant quotas, rate limits and token budgets enforced; cost metered per tenant, agent, model and provider; one audit record per call; a Grafana cost panel | todo | S010 |
| S012 | Knowledge and retrieval | Policy wording ingested into pgvector; hybrid search; the knowledge MCP server returns cited chunks; retrieval checked against a labelled query set | todo | S003, S009 |
| S013 | Policy and claims MCP servers | Tool contracts in `api/mcp/`; policy and claims MCP servers; per-agent allowlists from the registry; mutating tools require an idempotency key; every call audited | todo | S008, S009 |
| S014 | Triage graph and guardrails | Triage validates the policy, retrieves terms, screens fraud with rules and drafts a schema-validated proposal; PII redaction and injection detection in place; threat model updated | todo | S011, S012, S013 |
| S015 | Human approval | Interrupt and resume with the PostgreSQL checkpointer; the claim lifecycle from the architecture overview implemented and tested; approval decisions audited | todo | S014 |
| S016 | Adjuster UI | Server-rendered queue with claim, proposal, citations and fraud flags; approve, reject and request documents; audit trail; time-boxed to two sessions | todo | S015 |
| S017 | Evaluation harness | Golden-set replay with rule and LLM-judge graders (tool choice, arguments, groundedness, completion, latency, cost); a report per prompt version; a CI gate on prompt or tool changes; `meridian eval run` and `meridian eval compare` drive it locally and in CI | todo | S003, S014 |
| S018 | M1 exit | Views match the code and the register says so; threat model v1; a fifteen-minute demo script; the demo runs from a clean checkout with `make` | todo | S016, S017 |

### M2 — Azure, identity, delivery

| ID | Step | Done when | Status | Depends |
|---|---|---|---|---|
| S019 | Hardened Helm charts | Probes, resource limits, default-deny NetworkPolicy, PodDisruptionBudgets, non-root read-only containers, pinned digests; `helm lint` and the infra reviewer pass | todo | S018 |
| S020 | Azure platform | Terraform adds the virtual network, AKS, ACR, PostgreSQL Flexible Server with pgvector and Workload Identity to Key Vault; the environment is created and removed with one command each | todo | S007, S019 |
| S021 | Identity | Entra ID sign-in for the UI and APIs; roles platform-admin, agent-developer, adjuster and auditor; a mock OIDC issuer on kind; the tenant is resolved from the token | todo | S020 |
| S022 | Delivery pipeline | Build, SBOM, Trivy scan, cosign signing, push to ACR, kind smoke test, manual approval, deploy to AKS; the rollback runbook exercised; evidence attached to the release | todo | S020, S021 |
| S023 | Mistral provider | Mistral Large 3 adapter on Azure AI Foundry, DataZoneStandard; the routing policy uses it; ADR 3's provider set updated | todo | S010, S020 |
| S024 | Operations baseline | SLO definitions (targets, unmeasured), alert rules and dashboards as code; runbooks for provider outage, budget exhaustion, database failure, rollback and secret rotation | todo | S011, S019 |
| S025 | AWS mapping | An AWS deployment view and an ADR mapping every Azure service to its AWS equivalent | todo | S020 |
| S026 | M2 exit | Environment created, fifteen-minute demo on AKS, environment removed; recorded; the run's cost logged | todo | S021, S022, S024 |

### M3 — Reliability and operations

| ID | Step | Done when | Status | Depends |
|---|---|---|---|---|
| S027 | Load test and SLO thresholds | A load test measures latency and error rate; SLO thresholds set from the measurements; an error-budget panel | todo | S026 |
| S028 | Game day | Provider outage, budget exhaustion and database failure exercised; INC-001 written from the real timeline; rollback exercised | todo | S027 |
| S029 | Backup and restore drill | PostgreSQL restored into a scratch environment; restore time measured and recorded | todo | S020 |
| S030 | Provider change without breaking consumers | A model version swapped by a registry change only; consumer contract tests stay green; the evaluation compares both versions | todo | S017, S023 |
| S031 | Supervisor and workers | Triage split into a supervisor and workers with per-worker tool allowlists; the evaluation shows no regression | todo | S017 |
| S032 | Injection evaluation suite | Prompt-injection cases in retrieved content and claimant text; guardrail effectiveness measured in the harness | todo | S017 |
| S033 | Read-only platform console | Four pages: registry with residency, tenants with budgets and usage, evaluation runs, audit search | todo | S011, S021 |
| S034 | Governance documents | Provider onboarding process and service acceptance checklist, applied to the reference workload | todo | S024 |
| S035 | M3 exit | Architecture PDF released; demo script v2; every capability labelled | todo | S028, S033, S034 |

### M4 — Optional, at most one

| ID | Step | Done when | Status | Depends |
|---|---|---|---|---|
| S036 | AWS validate-only Terraform | The module passes `terraform validate` and a policy scan; it is never applied | todo | S025 |
| S037 | Second-framework workload | A small workload in Microsoft Agent Framework on the same platform contract | todo | S005, S018 |
| S038 | GraphRAG spike | A small knowledge graph of customer, policy, asset and claim; retrieval compared with hybrid search | todo | S012 |
| S039 | Workload scaffold | `meridian workload new` generates a workload that passes registry validation, the import contract and an empty evaluation on its first run | todo | S018 |

## Part C — Step details

Each step gets a section here when it starts. Template:

```text
### S0xx — <title>
**Status:** doing · **Started:** YYYY-MM-DD · **Finished:** —
**Goal:** one sentence.
**Decisions:** bullets, with the alternative rejected and why.
**Work log:** what was actually done, commands, links to PRs.
**Result / verification:** how we proved it is done.
**Follow-ups:** new steps or issues this created.
```

### S000 — Plan, harness and architecture bootstrap

**Status:** done · **Started:** 2026-09-29 · **Finished:** 2026-09-29
**Goal:** Turn the portfolio idea into a buildable plan, an architecture model
and an engineering harness before any platform code.
**Decisions:**

- Azure only, with kind locally; AWS designed, not deployed
  ([ADR 1](architecture/decisions/0001-run-on-azure-and-kind-design-aws.md)).
- LangGraph inside workloads behind a framework-agnostic platform contract
  ([ADR 2](architecture/decisions/0002-langgraph-behind-a-framework-agnostic-contract.md)).
- A thin gateway of our own rather than LiteLLM; Azure OpenAI and Mistral on
  EU data-zone deployments
  ([ADR 3](architecture/decisions/0003-build-a-thin-model-gateway.md)).
- Structurizr owns structure; Mermaid owns behaviour, logic and procedures.
- Main session Opus 5.5, subagents Sonnet at high effort, Fable as advisor.

**Work log:** Architecture kit adopted from architecture-base; ECC rules,
skills and reviewers adopted from the agent-base fork, plus two reviewers and
a threat-model skill of our own; hooks with a regression test. Mermaid tooling
built in architecture-base and adopted here. mermaid.ink allowlisted in the
owner's DNS filter so the Documentation tab renders diagrams.
**Result / verification:** `make docs` (13 checks), `make test` (116 tests),
`make check`, `make mermaid-render` and `make pdf` pass; rendered diagrams
checked in the PDF and in the served Documentation tab.
**Follow-ups:** S001.

### S001 — Commit and publish

**Status:** done · **Started:** 2026-09-29 · **Finished:** 2026-09-29
**Goal:** Put the bootstrap under version control, publish it, and land the
kit changes it depends on.
**Decisions:**

- Public repository `Dezoxy/meridian-ai-platform`. Squash merges only,
  branches deleted on merge, wiki and projects off.
- Ruleset `protect-main` on the default branch, following the owner's other
  public portfolio repository: no deletion, no force push, linear history,
  a pull request with every review thread resolved, and four required checks
  (docs consistency, architecture model, derived diagrams, secret scan). No
  bypass, including for the owner.
- Secret scanning with push protection, Dependabot alerts and private
  vulnerability reporting on; `SECURITY.md`, `CODEOWNERS`, a pull request
  template and Dependabot for GitHub Actions added.
- No licence file, as in the owner's other public portfolio repository: all
  rights reserved until Part D question 4 is answered.
- Before the first commit, private context was removed from tracked files:
  third-party organisation names in ADRs 1 and 2, local paths in the Codex
  hook configuration, and references to the owner's DNS setup.
**Work log:**

- First commit `9acdc12` pushed to `main`; repository created public with
  description and topics; settings and security features applied.
- The first CI run on `main` failed its secret scan: gitleaks-action v2
  installed gitleaks 8.24.3, which ignores the `[[allowlists]]` syntax. CI
  now pins gitleaks 8.30.1, the local version (`fe9d285`, pushed before the
  ruleset existed).
- Ruleset `protect-main` created after the first run registered the four
  check names; GitHub's effective rules for `main` list all five rules.
- GitHub renders the README's derived SystemContext diagram: its Mermaid
  viewer reports the block as rendered.
- Dependabot opened its first pull request (gitleaks-action v2 to v3), left
  for review.
- #1 merged the Dependabot update; the v3 action still honours
  `GITLEAKS_VERSION` and installs 8.30.1. #2 recorded the publish work in
  this plan. #3 added the developer CLI (plan v0.2).
- The architecture-base Mermaid pull request merged. It also added a
  `derived diagrams` CI job to the base, because `make docs` skips the
  derived-block comparison on a fresh checkout, where the generated views
  do not exist. Before merge, the base passed `make docs`, `make test`,
  `make check`, `make mermaid` and `make pdf`.
- agent-base: the installer's `yarn.lock` change reverted; the working tree
  is clean and level with `origin/main`.
- The Bash guard denied a plain `git push` because its push rule read past
  the push command into later segments of the same call. #4 judges the rule
  per command segment and adds 15 regression cases.

**Result / verification:** #2 was the first change to reach `main` through a
pull request and the four required checks. After each merge, the files the
branch changed were compared with `origin/main` and matched: #2 and #3
here, all 27 files of the architecture-base pull request there. CI on
`main` is green on all four checks; the guard suite passes 35 of 35 cases.
**Follow-ups:** ~~architecture-base Mermaid pull request~~ merged;
~~agent-base `yarn.lock` revert~~ done; ~~review the Dependabot pull
request~~ merged as #1; Part D question 4 (licence) stays open.
**Prepared actions (all done):**

1. Review the working tree, then make the first commit on `main`. There is no
   history to preserve yet, so this one commit goes straight to `main`.
2. Create the public GitHub repository and push; confirm the docs workflow is
   green and that the README's derived diagram renders on GitHub.
3. architecture-base: commit `feat/mermaid-diagrams`, open a PR to `main`,
   merge after CI, confirm the content landed.
4. agent-base: revert the `yarn.lock` that the installer's help run changed.

### S002 — Python workspace and CI gates

**Status:** done · **Started:** 2026-09-29 · **Finished:** 2026-09-29
**Goal:** A Python 3.13 uv project with empty platform and workload
packages, and CI gates (ruff, pytest, an import contract) that fail a
platform package importing the agent framework.
**Decisions:**

- One distribution, `meridian`, with regular packages `meridian.platform`
  and `meridian.workloads` under `src/meridian/`, not `src/platform/` and
  `src/workloads/`. A top-level package named `platform` would shadow the
  standard library's `platform` module, which libraries such as httpx and
  uvicorn import. Nothing needs a separate dependency declaration per
  package: the boundary is enforced on module names by import-linter, and
  every container will be built from this one distribution. Rejected: two
  workspace members with `meridian_platform` and `meridian_workloads` inside
  (`src/platform/meridian_platform/gateway/` stutters, and a third area for
  the runtime host would need a third member).
- The import rule is a layers contract (`meridian.workloads` above
  `meridian.platform`) plus a forbidden contract for the agent framework, so
  a runtime layer can be inserted later with one line.
- The Makefile keeps the kit's `test` and `docs` unchanged (the docs CI job
  runs them without uv) and gains a delimited Python section.
- The Python workflow has no path filters: once it is a required check, a
  pull request that touches no Python would otherwise never receive it.
- The forbidden contract lists eleven modules: `langgraph`, `langgraph_sdk`,
  `langchain`, `langchain_core`, `langchain_community`,
  `langchain_text_splitters`, `langchain_postgres` and the adapters for this
  platform's providers (`langchain_openai`, `langchain_mistralai`,
  `langchain_aws`, `langchain_anthropic`). ADR 2's `langchain*` cannot be
  written as a wildcard: import-linter rejects `langchain_*` with "A
  wildcard can only replace a whole module".

**Work log:**

- `pyproject.toml`: distribution `meridian` 0.0.0, Apache-2.0, Python
  `>=3.13,<3.14` (`.python-version` 3.13), `uv_build` backend; dev group
  pinned exactly: ruff 0.16.9, pytest 9.1.1, import-linter 2.15; `uv.lock`
  committed and CI installs with `uv sync --locked`.
- Packages `meridian`, `meridian.platform`, `meridian.workloads`, docstrings
  only. Two import-linter contracts: the forbidden contract above and a
  layers contract, `meridian.workloads` above `meridian.platform`.
- `tests/meridian/test_import_contracts.py`: copies `src/` and the
  pyproject into a temporary directory, plants one forbidden import in
  `meridian.platform`, runs `lint-imports` there and requires the broken
  import in the output; seven probes, plus a clean copy that must pass with
  at least one kept contract.
- `.github/workflows/python.yml`, job `python`: `uv sync --locked`,
  `make lint`, `make pytest`. `astral-sh/setup-uv` is pinned to `v10.2.0`
  because the action has published no floating major tag since v8.
  Makefile targets `lint` and `pytest`; Dependabot covers `uv`.
- Docs brought in line with the new paths: `CLAUDE.md` and `AGENTS.md`,
  the README, ADR 2 (the path only), the platform-boundary reviewer and its
  regenerated Codex twin, and `check-boundary.sh`, whose patterns were
  pipe-tested against platform, workload and gateway paths.
- gitleaks already runs in CI as the `secret scan` job from S001.
- Found on the way: import-linter flags an import of a package that is not
  installed, so CI needs no LangGraph to keep it out; `lint-imports` exits
  0 when no contract exists, which is why the clean-copy test requires a
  kept contract; `python -m importlinter.cli` exits 0 without output even
  on a violation, so the test calls the `lint-imports` script; ruff 0.16
  also formats Python fences in Markdown, so `make lint` covers the docs'
  code samples.

**Result / verification:** locally, `make lint` printed both contracts
`KEPT` and `Contracts: 2 kept, 0 broken.`; `make pytest` printed `8
passed`; before the contracts existed the same eight tests failed with
`Contracts: 0 kept, 0 broken.`. A probe
`from langchain_openai import ChatOpenAI` in `meridian.platform` made
`lint-imports` exit 1 with `meridian.platform._probe -> langchain_openai`.
`make docs` (13 checks), `make test` (116 tests) and the guard suite (35
cases) pass. The first CI run of the `python` job is on this step's pull
request.
**Follow-ups:**

- Add `python` to the required checks of the `protect-main` ruleset, after
  its first green run and with the owner's approval.
- S009: place the runtime host, which may import LangGraph, as a layer
  between `meridian.workloads` and `meridian.platform`.
- S010: enforce hard rule 4 (provider SDKs only in the gateway) with an
  import-linter contract once `meridian.platform.gateway` exists; until then
  the hook and the reviewer enforce it.

### S003 — Synthetic data and golden set

**Status:** done · **Started:** 2026-09-29 · **Finished:** 2026-09-29
**Goal:** A seeded, stdlib-only generator that writes policies, claim
history, policy wordings and first-notice-of-loss claims with labelled
expected outcomes, identically on every run.
**Decisions:**

- The generator is development tooling, not runtime code: a small package
  at `data/synthetic/generator/` with its output committed beside it, as
  hard rule 2 and this step already say. It stays out of the `meridian`
  distribution, so no container ships it. Rejected: a module under
  `src/meridian/`, which would ship and would need a place in the layers
  contract for code that no service runs.
- The output is committed. A changed expected outcome then shows in the
  pull request diff, and a test regenerates the data and compares it with
  the committed files, so the committed copy cannot drift from the code.
- Inputs and labels are separate files. The claims API will ingest
  `claims.json`; if the expected outcome sat in the same record, the agent
  could read its own grade.
- One product catalogue is the source of both the wording documents and
  the expected outcomes, so a clause and the label that cites it cannot
  disagree. Scenarios are built outcome first, for balanced coverage, and
  the generator stops if the outcome derived from the built data differs
  from the intended one.
- Business parameters, chosen here and cheap to change with a rerun:
  euro amounts in whole euros, an auto-approval threshold of EUR 2,500
  payable, a 30-day reporting window, and a fictional insurer in eurozone
  Central Europe (Austria, Slovakia, Slovenia, Croatia).
- No real personal data by construction: names are random pairs from short
  lists of common given names and surnames, e-mail addresses use the
  reserved `example.com` domain, there are no phone numbers, and vehicle
  registrations use a format no country issues.

**Work log:**

- `data/synthetic/generator/`, standard library only: a product catalogue,
  wording rendering, names and addresses, claim narratives, scenario
  builders, the oracle that derives expected outcomes, and the writer.
  `make synthetic` runs it with seed 20260929 and the fixed reference date
  2026-09-01.
- Output: 50 policies, 44 prior claims in the claim history, four wordings
  (`MOTOR-TPL`, `MOTOR-COMP`, `HOME-STD`, `HOME-PLUS`) with numbered clauses,
  and 40 claims with their expected outcomes: 8 approved automatically
  within the threshold, 6 over it, 6 with a fraud indicator (early loss,
  frequent claims, late report), 8 excluded, 6 on a policy not in force and
  6 with documents missing. `manifest.json` records the seed, the counts and
  a SHA-256 per file, so an evaluation report can name the data it used.
- The oracle applies one precedence, first match wins: policy not in force,
  then an exclusion, then a missing document, then a fraud indicator, then
  the threshold. Every rejection goes to the adjuster (C-02). A label cites
  the deciding clauses: the cover clause and the deductible, the limit when
  it caps the payout, the reporting clause on a late report.
- Claim numbers are shuffled, so the order of the files does not give the
  labels away. The fact behind an exclusion is never a claim field; the
  claimant's description states it.
- Reading the generated claims found what the tests could not: a pipe
  freezing in July, and a claimant "waiting at a junction" whose unlicensed
  friend was driving. Each exclusion now has its own consistent story, and
  a test sweeps every combination the builders can produce.
- A Python review with a mutation sweep found three date boundaries whose
  in-force side no test pinned, and background claim history that other
  seeds dated after a policy had ended. Both are fixed and tested. A seed
  other than the committed one now needs an explicit `--out`, and
  `.gitattributes` keeps `data/synthetic/` at LF line endings, which the
  byte-for-byte output and the manifest hashes depend on.
- `pyproject.toml`: the pytest paths gain `tests/synthetic` and
  `data/synthetic`; ruff ignores S311 (seeded, not cryptographic) in the
  generator only.

**Result / verification:** `make synthetic` run twice gave byte-identical
files (`shasum` compared). A test does the same in two processes with
different `PYTHONHASHSEED` values, and another regenerates the data and
compares it with the committed files. `make lint` printed
`Contracts: 2 kept, 0 broken.`; `make pytest` printed `195 passed`; `make
docs` (13 checks), `make test` (116 tests) and the guard suite (35 cases)
pass; gitleaks found no leaks in the files this branch adds or changes.
**Follow-ups:**

- S008 and S014: the threshold, the reporting window and the fraud rules
  are generator constants. When the triage workload gets its configuration,
  give both one source, or a test that they agree.
- S012: the citations in the expected outcomes are a ready labelled query
  set for the retrieval check.
- S017: record the manifest's version and hashes in every evaluation
  report. The oracle's tests pin the fraud-indicator boundaries, but no
  golden-set claim sits exactly on one (a report 30 days after the loss);
  add such cases, and an unknown policy number, if the harness needs them.
- S032: injection cases in claimant descriptions extend this generator.
- Harness: this step delegated its coding to a built-in agent with a model
  override, whose effort no setting pins. An `implementer` agent now takes
  delegated coding, with `model: sonnet` and `effort: high` in its
  frontmatter; added right after this step.

### S004 — Security and quality registers

**Status:** done · **Started:** 2026-09-29 · **Finished:** 2026-09-29
**Goal:** Number the trust boundaries and write the threat model, the data
classification and the quality attributes before the first security-relevant
code (S008, S010), so later steps cite T- and QA- IDs instead of inventing
them.
**Decisions:**

- Nine trust boundaries, `TB-1` to `TB-9`, each tied to named relationships
  in `model/containers.dsl`. The `feature-threat-model` skill lists eight;
  the workload plane calling the control plane (TB-3) is a boundary of its
  own, because a workload must not be able to claim another tenant.
- Every threat carries the step that builds its mitigation, or says "no
  step yet". Only controls with evidence in the repository are marked
  implemented (secret scanning, the protected branch and its checks, the
  lockfile and Dependabot). Two threats are open: claimants have no identity
  in the model (T-01), and document upload has no design (T-38). One risk is
  accepted: graph code runs inside the runtime process with its credentials
  (T-09), which holds while every workload is this repository's own code.
- Until claimant identity is decided, the claimant pages sit behind the
  staff sign-in and the presenter plays the claimant, so the internet-facing
  demo never collects a real person's data.
- S022 now depends on S021 as well as S020: the pipeline must not deploy to
  AKS before users have to sign in.
- Data classes: `internal` routes to EU labels only; `special` is refused
  outright; a tenant's class is the minimum for its requests, which detected
  content can raise and nothing can lower. The claims tenant is `personal`
  although its data is synthetic, so the demo exercises EU-only routing.
  The evaluation tenant
  takes the class of the workload it evaluates; on `synthetic` it could
  route to a `global` deployment and would measure a model production never
  calls.
- Retention periods are not invented: every environment here is destroyed
  after use, and a production deployment would take its periods from the
  insurer's schedule.
- Quality targets are initial and unmeasured, each with the step that
  measures it. Residency, audit completeness, human oversight and budget
  enforcement are absolute (100 %, zero), because each guards a constraint
  or a threat.
- The registers are prose, written in the main session; the `implementer`
  agent takes code. A `security-reviewer` pass checks the threat model
  against the model and the plan.

**Work log:**

- `docs/architecture/security/threat-model.md`: nine trust boundaries and
  38 threats; `docs/architecture/security/data-classification.md`: four
  classes, three tenants, an inventory of 15 kinds of data;
  `docs/architecture/requirements/quality-attributes.md`: `QA-01` to
  `QA-12`. Symlinked into `overview/` as `11-`, `22-` and `23-`; the
  architecture README, the glossary and the root README updated.
- The first draft had 26 threats. The security review found no critical
  issue and eight high ones, all accepted: an adjuster decision that a
  steered agent could record, tool arguments that could reach another
  claimant's record, the kind mock issuer reaching Azure, residency labels
  never checked against the deployed SKU, unguarded resume of paused runs,
  a code-owner review claimed but not required, a claimant-facing demo that
  could collect real data, and a wrong cross-reference. It also corrected
  step references that no step's "done when" covers; those are follow-ups
  below rather than claims.
- The `feature-threat-model` skill now points at `TB-1` to `TB-9` in the
  register instead of listing eight boundaries of its own.
- The approval flow in the model now matches T-31: the Claims Triage App
  records the adjuster's decision and resumes the run; no tool records a
  decision. The `ClaimsApproval` view, the Claims MCP Server's description
  and the overview table changed with it.

**Result / verification:** `make docs` passed 13 checks. The ID check is
live for both new families: a planted citation of an undefined threat ID
and quality ID, number 99 of each, failed it with "cites …, which … does not
define", and every real citation resolves.
`make check` ended with no ERROR line; `make test` (116 tests) and the guard
suite (35 cases) pass. `make pdf` built the PDF with constraints, quality
attributes, data classification and threat model in that order. In the
Documentation tab served by `make view`, all seven pages appear in the
navigation, the three new ones as sections 5 to 7, and the register tables
render.
**Follow-ups:** controls that the threat model needs and no step's "done
when" covers yet; each step adds its line when it starts.

- S009: one database role per service and an insert-only audit table
  (T-25).
- S013: service identity between runtime, gateway and MCP servers (T-08,
  T-24), and every tool call bound to the run's claim and tenant (T-22).
- S019: a rate limit at the ingress (T-02).
- S021: Grafana behind sign-in (T-03); pin the issuer per environment and
  refuse the mock issuer outside kind (T-06).
- S016 and S021: decide claimant identity (T-01).
- A step for document upload before any claimant can attach a file (T-38).
- S020: the Application Gateway web application firewall when the Azure
  edge is built, or it stays designed (T-02).

### S005 — Agent framework spike
**Status:** done · **Started:** 2026-09-29 · **Finished:** 2026-09-30
**Goal:** measure Microsoft Agent Framework against LangGraph on the same
three-step claim flow with an approval pause, and append the decision matrix
that ADR 2 promised.

**Decisions:**

- The spike is its own uv project under `spikes/s005-agent-framework/` with
  its own lockfile, not a workspace member. Rejected: adding the frameworks
  to the root lock, which would put LangGraph into the workspace before S009
  and a second framework into it for good.
- The same flow is built in LangGraph too, under the same tests. Rejected:
  filling the LangGraph column from documentation, which would compare a
  measurement with a claim. This is a spike twin, not the second workload
  implementation that ADR 2 rejected as option 3; it is never deployed.
- No model call. The matrix rows (state, tool contracts, approval pauses,
  checkpoints, telemetry) need none, and the spike needs no credentials.
- LangGraph stays the workload framework (ADR 2 appendix); the owner chose
  it on 2026-09-30 after the review below narrowed the gap. The reason ADR 2
  gave for rejecting Microsoft Agent Framework, immature checkpoints and
  pauses, is struck with a note: the spike contradicted it. What remains is
  LangGraph's stable PostgreSQL checkpoint store, against a store this
  project would write and maintain, and a failed save that raises without
  caller code. Rejected: switching to Microsoft Agent Framework for its
  pause, telemetry, footprint and Azure fit, because the checkpoint store is
  the component one maintainer should least build alone (C-01). The margin
  is narrow, and ADR 2 says so.
- Dependabot does not cover `spikes/`: the pins are the versions the notes
  describe. Rejected: a Dependabot entry, which would move the pins away from
  the evidence.

**Work log:**

- The `implementer` subagent built the spike against a written contract:
  shared rules, one flow module per framework, a CLI for a two-process
  pause and resume, and 72 tests over both. Every framework API was read
  from the installed package, and the README cites the source line behind
  each observation.
- Spot-checked three citations against the installed source: the
  checkpoint-save warning in `agent_framework/_workflows/_runner.py`, the
  pickle security note in `_checkpoint_encoding.py`, and the
  `Command(resume=None)` crash in `langgraph/pregel/_loop.py`.
- Wrote the matrix, its reading and the runtime obligations into ADR 2;
  added the spike to the README capability table and layout.
- An independent review of each framework's column, against the official
  documentation and the installed source, found the first version unfair to
  Microsoft Agent Framework: its runs were addressed by an immutable
  checkpoint ID, so "resume twice" replayed the pause, while LangGraph runs
  had a stable thread ID. Addressed by workflow name, Microsoft Agent
  Framework refuses the second resume. The reviews also showed that a
  pickle-free store needs only the public storage protocol, that LangGraph
  raises on a failed save, that both frameworks enforce value rules carried
  by the payload type, and that LangGraph's default deserialization is no
  safer than a restricted unpickler. The three experiments the new
  reasoning rests on were rerun in the main session before it was rewritten.
- The `implementer` corrected the spike so that each finding is pinned by a
  test in the repository, and ADR 2's appendix was rewritten from them. A
  last test pair resumes one pause from two threads at once: Microsoft Agent
  Framework completes both with different outcomes, and LangGraph can tell
  the two callers different outcomes while storing one, so the runtime's
  conditional update is needed with either framework.

**Result / verification:**

- In the spike, `uv run pytest -q`: `135 passed` after the review (`72 passed`
  before it); the two concurrent-resume tests passed in eight further runs.
  The LangGraph one is the suite's only probabilistic assertion: it retries
  up to ten pauses until the two callers are told different outcomes.
- `make lint`: exit 0, `Contracts: 2 kept, 0 broken.`; the spike's `.venv`
  is not linted (`ruff check spikes --show-files` lists no `.venv` path).
- `make pytest`: `195 passed`; the root `uv.lock` is unchanged.
- `make test`: `Ran 116 tests`, `OK`.
- `make docs`: `13 checks passed`. `make check`: exit 0, no ERROR line.
- `make view`: ADR 2 renders the struck reason as a strikethrough, not as
  literal tildes, and the matrix as a 16-row table inside the page width.

**Follow-ups:**

- S009: the runtime issues run IDs and maps them to LangGraph threads; a
  callback handler opens one span per node, because LangGraph emits none;
  `LANGGRAPH_STRICT_MSGPACK=true` set before LangGraph is imported, with
  graph state holding only primitives and dicts; `durability="sync"` and the
  pause read from the interrupts.
- S015: resume once, as a conditional update before the framework is
  called; the adjuster from the sign-in, not the payload; delete a completed
  run's checkpoints or keep claim text out of the graph state (T-10, T-32).
- S014: the policy-validity rule reads `lapsed_on`, not only `status`; the
  spike's simpler rule proposes a rejection for CLM-0010, which the golden
  set labels `auto_approve`.
- S014, with its threat model update: a threat for checkpoint
  deserialization. Whoever can write the checkpoint store can make the
  runtime load types it did not write (pickles in Microsoft Agent Framework,
  permissive msgpack in LangGraph by default). T-10 covers who may resume,
  not what loading a checkpoint executes.

### S006 — Local platform on kind
**Status:** done · **Started:** 2026-09-30 · **Finished:** 2026-09-30
**Goal:** one command creates the local platform on kind (gateway, PostgreSQL
with pgvector and the observability stack) from pinned Helm charts, proves a
test trace reaches Grafana, and one command removes it.

**Decisions:**

- The edge is the Gateway API served by Envoy Gateway, chosen by the owner
  on 2026-09-30. The model named ingress-nginx, which Kubernetes retired in
  March 2026: no releases and no security fixes since. Rejected: keeping
  ingress-nginx, an unpatched edge; Traefik, lighter but less common in
  Azure estates. HTTPRoute objects are what Azure Application Gateway for
  Containers reads, so the routes can carry over to AKS; the Azure edge
  itself is decided in S020.
- PostgreSQL on kind runs under the CloudNativePG operator, with its
  standard image, which ships pgvector. The operator generates the database
  credentials inside the cluster, so none are written by hand, and it
  manages the `vector` extension declaratively. Rejected: the Bitnami
  chart, whose free images Broadcom withdrew in 2025; a hand-written
  StatefulSet with the `pgvector/pgvector` image, which is lighter but
  leaves credentials and extensions to our own scripts. On Azure the same
  role is PostgreSQL Flexible Server (ADR 1); the workloads see a
  connection string from a Secret in both places.
- The owner authorised one `make down` on 2026-09-30 to verify the step,
  deleting only the `meridian` kind cluster this session creates (hard
  rule 8).

**Work log:**

- The `implementer` subagent built `infra/kind/` against a written
  contract: one `pins.env` with every chart version and image digest,
  values per release, the Gateway and namespace manifests, and `up.sh`,
  `smoke.sh`, `grafana.sh` and `down.sh` behind new `make` targets. Every
  `kubectl` and `helm` call names `infra/kind/kubeconfig` and the
  `kind-meridian` context, so the owner's `~/.kube/config` and other
  clusters are never read or changed, and no chart repository is added to
  the owner's Helm configuration.
- Chart facts found on the way: Grafana moved its open-source charts to
  `grafana-community` in 2026, so Tempo and Loki come from there and
  Grafana arrives as kube-prometheus-stack's subchart; Tempo 3 in
  monolithic mode needs no Kafka; Loki's memcached caches are off, since
  they alone would ask for several GiB; node-exporter needs
  `hostRootFsMount` off on Docker Desktop; Envoy sends no identifying
  header on a 404, so the smoke test reads Envoy's request counter instead.
- Replaced ingress-nginx with Envoy Gateway in the model and the overview;
  T-03 names S006 for Grafana's sign-in outside the gateway; QA-11 records
  the kind measurement; the README lists the local platform.
- Raised Grafana's memory limit from 320Mi to 512Mi after its container was
  OOM-killed once while starting on the clean run; `helm --wait` had not
  noticed, because the pod recovered.
- The `infra-reviewer` passed the step with one must-fix, confirmed here:
  `make smoke` left its Grafana port-forward running, because it
  backgrounded a shell function and so killed a subshell instead of
  kubectl. The `implementer` fixed it and the hardening the review asked
  for: the kubeconfig is refreshed on every `make up`, `make up` waits for
  the Envoy proxy, the scripts refuse a Docker engine that is not a local
  socket, curl ignores `~/.curlrc` and proxies, a failed `kind get
  clusters` no longer reads as "no cluster", unknown Loki keys are gone,
  and the API server address is pinned to `127.0.0.1`.
- The guard hook asked before `kind delete` but not before `make down`,
  which wraps it (hard rule 8). It now asks before `make down` and before
  running `infra/kind/down.sh`, with test cases for both and for commands
  that must not match.

**Result / verification:**

- `make down` (authorised by the owner): exit 0, `Deleted nodes:
  ["meridian-control-plane"]`; afterwards `kind get clusters` printed `No
  kind clusters found.`, no `meridian` container was left and
  `infra/kind/kubeconfig` was gone.
- `make up` from no cluster: exit 0 in 245 s (QA-11 target: under 10
  minutes), with the kind node image already local and every other image
  pulled. A second `make up`: exit 0 in 37 s; it replaced only the Grafana
  pod, whose values had changed, and restarted nothing.
- `helm list -A`: seven releases `deployed`. All pods Running and Ready with
  zero restarts after the Grafana fix; Gateway `edge` `PROGRAMMED True`,
  proxy Service `80:30080/TCP`; port 8088 listens on `127.0.0.1` only.
- `make smoke`: exit 0, six PASS lines, among them `PASS  trace: Tempo has
  trace be295bf023e36c828bf2b7bd5a77af90 for meridian-smoke-1790761201`,
  read through Grafana's Tempo datasource. A made-up service name returns 0
  traces and 0 log streams; the real one returns 1 trace. Grafana answers
  401 without credentials and with a wrong password.
- `docker stats` on the node: 3.59 GiB of 7.65 GiB; the memory limits add up
  to 4,464 Mi.
- `~/.kube/config` unchanged (last modified 2025-11-26).
- After the review fixes: `make up` exit 0 in 27 s, `make smoke` six PASS
  lines, no `port-forward` process left afterwards, no pod restarted;
  `DOCKER_HOST=tcp://192.0.2.1:2375 infra/kind/smoke.sh` refuses with
  `error: the Docker engine is not local` and exit 1.
- `bash tests/test_guard_bash.sh`: exit 0, 46 cases `ok`.
- `shellcheck infra/kind/*.sh`: exit 0. `make docs`: `13 checks passed`.
  `make test`: `Ran 116 tests`, `OK`. `make check`: exit 0, no ERROR line.
  `make lint`: `Contracts: 2 kept, 0 broken.`

**Follow-ups:**

- S019: TLS on the gateway, default-deny NetworkPolicy, hardened security
  contexts, digests for every chart image, and limits on the two
  containers that have none (Envoy's shutdown manager, Prometheus's config
  reloader).
- S020: the Azure edge. The kind routes are Gateway API objects, which
  Application Gateway for Containers reads; the model still names
  Application Gateway WAF for Azure.
- S009: when the first HTTPRoute lands, give the edge listener or the route
  a hostname (for example `*.localhost`). A web page in the owner's browser
  can otherwise reach `127.0.0.1:8088` through DNS rebinding (T-01). Today
  the edge has no route, so it answers 404 to everything.
- S022: run `make up` and `make smoke` on kind in CI; today they run on the
  owner's laptop only.
- The chart pins in `pins.env` are not watched by Dependabot; bumping them
  is manual until a step adds Renovate or similar.

### S007 — Azure foundation
**Status:** done · **Started:** 2026-09-30 · **Finished:** 2026-09-30
**Goal:** Terraform creates the persistent Azure foundation (resource group,
subscription budget with 50, 80 and 100 % alerts, Key Vault, and EU
deployments of an Azure OpenAI chat model and embedding model) from remote
state in Azure Storage, after the owner has reviewed the plan and confirmed
the apply.

**Decisions:**

- Terraform state lives in an Azure Storage account in Sweden Central with
  Entra ID authentication only, chosen by the owner on 2026-09-30 (Part D
  question 2). Rejected: HCP Terraform in the owner's existing organization,
  the plan's default, whose state would live in the US and whose use from
  CI in S022 would most likely need a stored API token (T-37); HCP
  Terraform Europe, a separate account that loses the consistency with the
  homelab that made HCP the default.
- The foundation is persistent, not per demo day. Nothing in it bills while
  idle: Azure OpenAI Standard deployments bill per token, Key Vault per
  operation, the budget is free and the state storage costs cents. M1 runs
  on kind but calls Azure OpenAI from S010, so the deployments must exist
  between demo days. Only the compute environment of S020 (AKS, PostgreSQL,
  registry) is created and removed per demo day. C-04's tactic says so now;
  ADR 1 still lists Key Vault and Azure OpenAI among what Terraform removes
  after a demo, which this step narrows without rewriting the accepted ADR.
- Meridian gets a subscription of its own. On 2026-09-30 the owner created
  a new Azure account, a free trial with 200 US dollars of credit for 30
  days and a spending limit, and chose it over the pay-as-you-go
  subscription that C-05 named, which the owner also uses for other work. The
  budget and the Entra tenant now serve Meridian alone. When the credit
  ends, the subscription must be upgraded to pay-as-you-go, or Azure
  disables it and, with it, the deployments and the Terraform state.
- The trial has no EU quota for the planned models. From
  `az cognitiveservices usage list` on 2026-09-30: `gpt-4.1-mini` has 0 on
  Standard and DataZoneStandard and 200 only on GlobalStandard;
  `text-embedding-3-large` has 350 on Standard in Sweden Central and 0 on
  the data-zone SKU; West Europe offers no current chat model on an EU SKU.
  The pay-as-you-go subscription and an older trial both had 2,000 and
  1,000 units on DataZoneStandard, so the quota follows the offer, and a
  free trial cannot ask for more.
- The owner chose to stay on the trial with other models, all on regional
  Standard in Sweden Central, labelled `eu-region`, at 20,000 tokens per
  minute each; capacity is a rate limit, not a cost. Rejected: upgrading
  the trial now, which keeps the credit and very likely restores the
  data-zone quota but ends the spending limit; returning to the
  pay-as-you-go subscription, whose quota was verified. The first choice,
  `gpt-4o-mini` 2024-07-18, failed at the apply: Azure answered
  `ServiceModelDeprecated`, deprecated since 2026-03-31, after seven of the
  nine resources had been created. Offered the upgrade again, the owner
  chose `gpt-4o` 2024-11-20, the only current chat model with EU quota on
  the trial (50 units). The embedding model is `text-embedding-3-large` 1.
  The consequences: `gpt-4o` costs roughly six times `gpt-4.1-mini` per
  token, which puts QA-07's target at risk once the credit is gone; no
  second region until the upgrade, so the gateway's fallback (QA-04, S010)
  stays designed; ADR 3 carries a dated amendment. The West Europe account
  returns as one line in `openai_locations` once the quota exists.
- Terraform targets one pinned subscription. The owner has more than one,
  and two share the default name "Azure subscription 1". The state
  bootstrap takes the subscription by ID, or by a name that matches exactly
  one, and records it in a gitignored `infra/terraform/local.env`; every
  `az` and Terraform call uses it, so a changed `az` default cannot
  redirect Meridian. The tenant is that subscription's own
  (`ARM_TENANT_ID`), and the owner's object ID comes from a token for it,
  not from the `az` default account. No subscription, tenant or object ID
  is written to a tracked file, and everything the scripts print from `az`
  and Terraform is GUID-redacted, so a plan can be pasted as evidence.
- The budget covers the whole subscription at 60 euros, the billing
  currency of the account's billing profile, with alerts on actual spend at
  50, 80 and 100 %. They go to an action group that emails whoever holds
  Owner directly on the subscription: the Budgets API rejects a
  notification that names only a role, and a group keeps email addresses
  out of the repository. Budgets alert and do not stop spend (T-15);
  during the trial the spending limit is the hard stop. The budget starts
  in the month of its first apply and Terraform ignores the start date
  afterwards, because Azure accepts a past start date only within the
  current month.
- Azure OpenAI accepts Entra ID tokens only; key authentication is off, so
  no key can leak (T-18). The owner gets the data-plane role Cognitive
  Services OpenAI User; the Owner role alone cannot call a model. A pod on
  kind has no `az login`, so S010 decides how the gateway authenticates
  there.
- Model versions are pinned with no automatic upgrade, so the registry's
  facts stay true. A Terraform validation refuses any SKU other than
  Standard or DataZoneStandard, and any region other than Sweden Central
  or West Europe, so a Global or non-EU deployment is an error before it
  is a review finding (hard rule 3); the smoke test checks both again
  against Azure, independently of Terraform. Terraform does not know which
  region is primary; the registry and the gateway decide that (S008,
  S010).
- Terraform registers no resource provider. The state bootstrap registers
  the five the foundation uses (a new subscription starts with most of
  them unregistered), so `make azure-plan` changes nothing in Azure.
- Model facts on 2026-09-30 from `az cognitiveservices model list`:
  `gpt-4o` 2024-11-20 is Legacy, and its Standard and DataZoneStandard
  deployments end on 2027-04-14, the day `gpt-4.1-mini` 2025-04-14, also
  Legacy, retires; `gpt-5-mini` retires on 2027-02-09;
  `text-embedding-3-large` 1 on 2028-02-09. The catalogue lists
  `gpt-4o-mini` 2024-07-18 with a Standard entry ending on 2026-03-31 and
  another ending on 2028-04-13; the service applied the first, so the
  catalogue alone does not settle whether a deployment is accepted.

**Work log:**

- Read-only facts first, on the pay-as-you-go subscription: both planned
  models on DataZoneStandard in both regions, 2,000 and 1,000 units of
  quota, billing in euros, and the owner holding Owner. HCP Terraform's
  saved login was for its US region; HCP Europe exists as a separate
  account.
- The `implementer` subagent built `infra/terraform/` against a written
  contract: the `foundation` root module, `state.sh` for the remote state,
  `foundation.sh` for init, plan, apply and smoke, four `make azure-*`
  targets, guard rules with regression cases, and a README. Every gate was
  run again here.
- The `infra-reviewer` blocked the first version with five must-fixes, all
  confirmed and fixed: the code still deployed the models the move to the
  trial had ruled out; budget notifications named only a role; `az` 2.90
  rewrites storage authorisation errors, so the retry for role propagation
  never matched; the object ID and tenant followed the `az` default
  account; a new subscription has most resource providers unregistered.
  From its other findings: a subscription is required on the first run,
  output is GUID-redacted, the guard also asks for `VAR=value` prefixes,
  `bash -c`, `az … delete` and `az … purge`, Terraform state surgery and
  `az account get-access-token`, and the smoke test takes deployment names
  from the outputs. A re-review passed with no must-fix; from it, the smoke
  test sends the token only to an Azure OpenAI endpoint, the plan file is
  owner-only, and the older rule denying `az … keyvault … delete` no longer
  fires on read-only queries that name delete-retention properties. The
  implementer stopped without a report after all but the Terraform README,
  which the main session wrote.
- The owner moved Meridian to a new free-trial account. Its quota was read
  after registering the Cognitive Services provider by hand, the one change
  made before the bootstrap. The subscription's own record reads
  `quotaId FreeTrial_2014-09-01` with `spendingLimit On`; both Owner
  assignments on it belong to the owner, so the budget's action group
  reaches no one else.
- `AZURE_SUBSCRIPTION=<id> make azure-state` registered the Storage, Key
  Vault and Insights providers and created `rg-meridian-tfstate`, the
  storage account, the role assignment, the `tfstate` container and the
  `lock-tfstate` lock, then wrote `local.env`.
- The first plan, `9 to add, 0 to change, 0 to destroy`, was reviewed by
  the owner and applied. Seven resources were created; the chat deployment
  failed with `ServiceModelDeprecated` for `gpt-4o-mini` 2024-07-18, and the
  embedding deployment, which waits for it, was not attempted. After the
  owner chose `gpt-4o`, the second plan was `2 to add, 0 to change, 0 to
  destroy`, and the apply printed `Apply complete! Resources: 2 added, 0
  changed, 0 destroyed.`

**Result / verification:**

- `make azure-smoke`, all six checks:

  ```text
  PASS  account oai-meridian-sdc-974b59: key authentication is off (disableLocalAuth true)
  PASS  account oai-meridian-sdc-974b59: swedencentral is an EU region
  PASS  deployment sdc/gpt-4o: gpt-4o 2024-11-20 on Standard in swedencentral
  PASS  deployment sdc/text-embedding-3-large: text-embedding-3-large 1 on Standard in swedencentral
  PASS  chat oai-meridian-sdc-974b59: gpt-4o answered (model gpt-4o-2024-11-20, 15 tokens)
  PASS  embedding oai-meridian-sdc-974b59: text-embedding-3-large returned a 3072-dimension vector
  ```

- A second `make azure-plan`: `No changes. Your infrastructure matches the
  configuration.`
- Read back from Azure: the budget is 60.0, Monthly, from 2026-09-01, with
  three notifications, each with the Owner role and one action group. The
  state account has shared-key access and public blob access off, TLS 1.2,
  ZRS and OAuth by default; blob versioning is on with 14-day soft delete
  for blobs and containers; `lock-tfstate` is `CanNotDelete`. The vault has
  RBAC authorisation, purge protection and a 7-day soft delete.
- `terraform fmt -check`: exit 0. `terraform validate`: `Success! The
  configuration is valid.` `trivy config`: one finding, AZU-0013 (the
  vault's network rules allow by default), deferred to S019 and S020.
  `shellcheck infra/terraform/*.sh`: clean. Guard regression cases: 114,
  all ok. No GUID in the tracked files under `infra/terraform/`, the
  `Makefile`, `.gitignore` or the guard, and none in the redacted plan.
- `make docs`: `docs consistency: 13 checks passed`. `make test`: `OK`.
  `make check`: exit 0, no ERROR line. `make mermaid` rewrote the README's
  derived diagram for the new Azure OpenAI description.

**Follow-ups:**

- Upgrade the subscription to pay-as-you-go before the credit ends, about
  2026-10-30, or Azure disables it with the deployments and the state.
  Then check the data-zone quota, move back to `gpt-4.1-mini` or its
  successor on DataZoneStandard, add the West Europe account and update the
  ADR 3 amendment.
- S010: a second-region chat fallback waits for that upgrade. On
  2026-09-30 the trial had chat quota on an EU SKU only in Sweden Central
  (`gpt-4o` 50 units; `gpt-4o-mini` 200, which Azure no longer deploys);
  France Central, Germany West Central, Italy North, Poland Central, Spain
  Central, North Europe and West Europe had none. Until the upgrade, S010
  can still build the whole fallback mechanism, with only the second
  region labelled designed:
  - a second `gpt-4o` deployment in Sweden Central (the 50 units fit two
    of 20 to 25), so the registry's candidate list, the circuit breaker
    and a test that injects a failure are real; it does not survive a
    Sweden Central outage;
  - the replay provider of ADR 3 as the last candidate, so the demo keeps
    running offline, labelled simulated;
  - optionally an embedding fallback region: `text-embedding-3-large` has
    regional Standard quota in Germany West Central, France Central and
    Poland Central, and the same model version gives the same vectors.
    Terraform gives every account both deployments today, so a
    region with embeddings only needs per-location deployment sets first;
  - not a fallback: `gpt-4.1-mini` on GlobalStandard (200 units on the
    trial) may process data outside the EU, so the gateway must refuse it
    for claims (hard rule 3); showing that refusal is residency evidence.

  Key authentication is off, so S010 also decides how a pod on kind
  authenticates to Azure OpenAI.
- S008: the registry labels these deployments `eu-region` and compares its
  labels with `terraform output -json openai_deployments` (T-12). CI has no
  Azure access until S022, so S008 decides whether it compares against a
  committed snapshot.
- S011: `gpt-4o` costs roughly six times `gpt-4.1-mini` per token, which
  puts QA-07's target at risk; measure it, and revisit the model after the
  upgrade. `gpt-4o` 2024-11-20 Standard deployments end on 2027-04-14, and
  with no automatic upgrade the deployment stops then.
- S019 and S020: network rules or private endpoints for the vault, the
  account and the state storage (AZU-0013), and diagnostics settings.
- S022: `terraform fmt`, `validate` and `trivy` in CI, and the pipeline's
  OIDC identity with Storage Blob Data Contributor on the state account.
- S022: the role assignments go to the signed-in user as `principal_type =
  "User"`; when Terraform runs as the pipeline's identity, the operator
  becomes a variable or the assignments move.

## Part D — Open questions

| # | Question | Needed by | Default if unanswered |
|---|---|---|---|
| 1 | How many hours per week, and when do interviews start? | S002 | Plan in two-week increments; cut M3 before M2 |
| 2 | Terraform state: HCP Terraform, as in the homelab, or an Azure Storage account? **Answered 2026-09-30: Azure Storage** in Sweden Central with Entra ID authentication (S007) | S007 | ~~HCP Terraform, for consistency with the homelab~~ |
| 3 | A claim whose documents miss the deadline is closed as rejected without a human. Keep that, or route it to the adjuster? | S015 | Keep, recorded as a procedural closure in C-02 |
| 4 | Licence: keep all rights reserved, or publish under MIT or Apache-2.0? **Answered 2026-09-29: Apache-2.0**, copyright Dezoxy; `NOTICE` credits the MIT-licensed ECC material | Before anyone asks to reuse the code | ~~All rights reserved~~ |

## Part E — Changelog

- **v0.1, 2026-09-29:** plan created from the bootstrap session; steps S000
  to S038.
- **v0.2, 2026-09-29:** developer CLI added: a `### Developer CLI` section in
  Part B, `registry validate` in S008, `eval run` and `eval compare` in S017,
  and S039 (workload scaffold) as a fourth optional M4 item.
- **v0.3, 2026-09-29:** Part D question 4 answered: Apache-2.0, with a
  `NOTICE` for the ECC material.
- **v0.4, 2026-09-29:** Part A delegates implementation to the `implementer`
  subagent.
- **v0.5, 2026-09-29:** S022 depends on S021 as well as S020 (S004
  threat model: no deployment to AKS before sign-in).
- **v0.6, 2026-09-30:** Part D question 2 answered (Azure Storage); S007's
  done-when names `gpt-4o` on regional Standard in Sweden Central after the
  move to a free-trial subscription, with the West Europe fallback after
  its upgrade.
