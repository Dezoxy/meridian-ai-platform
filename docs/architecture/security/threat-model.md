## Threat model

Status on 2026-09-30: mostly designed. The platform's first services exist
as a walking skeleton that runs in tests and is not deployed yet (S009), so
most mitigations below are designed, with the step that builds it; the
controls that already exist (secret scanning, the protected branch, the
locked dependency set, and the first service-level ones from S009) are
marked implemented with their evidence. A threat
with no mitigation decided yet is marked open, and one risk is accepted
rather than mitigated. Labels follow C-07. "No step yet" means that no step's
"done when" in the plan covers the control; the plan records it as a
follow-up.

This register owns the `T-NN` IDs. Data classes are defined in the
[data classification](data-classification.md); measurable targets are in the
[quality attributes](../requirements/quality-attributes.md). Before building
a security-relevant feature, run the `feature-threat-model` skill and add or
refresh rows here.

### Trust boundaries

Each boundary is a place where the caller and the callee trust different
things. The relationships named are the main ones of the architecture model
(`model/containers.dsl`), so a changed relationship shows which boundary to
revisit. The rest are covered by single threats: database writes by T-25,
telemetry exports by T-03, registry reads by T-35.

| Boundary | Between | Relationships in the model |
|---|---|---|
| TB-1 | People and the platform | Claimant and adjuster to Ingress over HTTPS; operator to the Observability Stack |
| TB-2 | Edge and the workload APIs | Ingress to the Claims Triage App; the app validates user tokens against Entra ID |
| TB-3 | Workload plane and control plane | Claims Triage App to Agent Runtime with a service identity; Evaluation Harness to Agent Runtime; the workload's graph package inside the runtime process |
| TB-4 | Callers and the Model Gateway | Agent Runtime to gateway with tenant and agent headers; Knowledge MCP Server to gateway for embeddings |
| TB-5 | Model Gateway and the outside | Gateway to Azure OpenAI and Mistral; gateway to Key Vault for provider credentials |
| TB-6 | Agent Runtime and tools | Runtime to the Policy, Knowledge and Claims MCP servers; the servers to the Platform Database |
| TB-7 | Untrusted content and the model context | Claimant text, retrieved wording, tool results and model output entering a prompt or a decision |
| TB-8 | Agent proposal and human decision | A paused run, the adjuster's decision through the Claims Triage App, the resumed run and its audit record |
| TB-9 | Git and the running platform | Registry changes by pull request; dependencies, images, credentials and configuration reaching the cluster |

TB-3 is partly not a network boundary: the runtime hosts the workload's graph
package in its own process (ADR 2), so graph code runs with the runtime's
database and gateway credentials. TB-7 is not a network boundary at all: the
content crosses no socket a firewall could guard, which is why its
mitigations sit in the runtime and the workload.

### Assets

- Personal data of claimants: claims, contact details, free-text
  descriptions, and every prompt, completion and checkpoint that carries
  them.
- Provider credentials, signing secrets, the delivery pipeline's cloud
  identity and the Terraform state.
- Approval decisions and their audit trail: who decided what, on which
  proposal, when.
- Budgets and quotas, which are money.
- The registry: which deployment may see which data class, which agent may
  call which tool.
- The golden set and the evaluation gate, which decide whether a prompt or a
  model change may ship.

### Threat register

STRIDE-lite: S spoofing, T tampering, R repudiation, I information
disclosure, D denial of service, E elevation of privilege. T-33 is a human
oversight risk that STRIDE has no letter for.

| ID | Boundary | Threat | Mitigation | Built in | Status |
|---|---|---|---|---|---|
| T-01 | TB-1 | S, I: anyone can submit a claim, or read a claim's status, as any claimant. The model gives the claimant no sign-in; only staff authenticate. | Claimant identity is not decided. Until it is, the claimant pages sit behind the staff sign-in and the presenter plays the claimant; on kind they are reachable only from the laptop. | S016, S021 | Open |
| T-02 | TB-1 | D: a flood of claim submissions starts triage runs and spends the model budget, which then refuses the adjusters' work too. | T-01's interim control keeps anonymous traffic out; per-tenant budgets refuse work beyond budget (QA-12); a rate limit at the ingress; a web application firewall in the Azure design. | S011; no step yet for the ingress limit and the firewall | Designed |
| T-03 | TB-1 | I, T: traces or logs capture prompt text or claimant details that anyone reaching Grafana can read, or claimant text forges log lines. | Spans carry metadata (tenant, agent, model, tokens, cost, claim ID), never prompt or completion text; structured logs with claimant text as an escaped field; the log formatter redacts personal fields; Grafana behind sign-in, and outside the ingress on kind. | S006 (Grafana sign-in, outside the gateway on kind), S009, S014 | Implemented in part: span attributes pass an allowlist, and no span records an exception's message or stack trace (S009, tested with canaries); log redaction arrives in S014, Grafana sign-in in S021 |
| T-04 | TB-1 | I: a real person types real personal data into a demo that is meant to hold synthetic data only (C-03). | The claimant pages are not public (T-01); the form says the data must be fictional; demos submit claims from the golden set. | S016, S021 | Designed |
| T-05 | TB-2 | S, E: a forged, expired or replayed token, or a missing role check, lets a caller act as an adjuster. | Validate the signature, expiry, issuer and audience, with issuer and audience pinned per environment; check the role on every endpoint; resolve the tenant from the token, never from the request body. | S021 | Designed |
| T-06 | TB-2 | S: the Azure environment accepts tokens from the mock OIDC issuer that stands in on kind, or its signing key. | The mock issuer is deployed only by the kind configuration; the Azure configuration pins the Entra issuer, and the application refuses to start with the mock issuer outside kind; the mock's key is generated at `make up` and never committed. | S021 | Designed |
| T-07 | TB-2 | T: claim fields or model output carry SQL or markup that reaches the database or runs in the adjuster's browser. | Pydantic validation at the API; parameterised queries only; templates escape by default and render claimant text and model reasons as text. | S009, S016 | Implemented in part: bounded Pydantic models refuse extra fields and NUL bytes, and every query is parameterised (S009); template escaping arrives in S016 |
| T-08 | TB-3 | S: a workload calls the runtime or the gateway claiming another tenant or agent. | Service identity per workload container; the tenant and agent come from the caller's identity mapped in the registry, and a header that disagrees is refused; default-deny network policy. | S019; no step yet for service-to-service identity | Designed |
| T-09 | TB-3 | E: graph code runs inside the runtime process with the runtime's credentials, so a faulty or hostile graph package can act as the runtime. | Accepted: graph packages are code from this repository, reviewed and tested like the platform; isolating workloads from each other is out of scope for one maintainer (C-01). Reconsider before a second team's workload runs here. | — | Accepted |
| T-10 | TB-3 | S, E: a caller resumes, or reads, another claim's paused run, or replays a resume. | Run IDs are random; a resume must match the paused run's claim and tenant; a decision resumes a run once, idempotently; checkpoints are keyed by tenant. | S015 | Designed |
| T-11 | TB-4 | I: a prompt with personal data reaches a deployment whose residency label is `global` (C-02). | The gateway takes the request's data class, chooses only among deployments whose label allows it, and refuses and audits a mismatch (ADR 3); a contract test proves the refusal (QA-03). | S010 | Designed |
| T-12 | TB-4 | I: a registry label says `eu-zone` for a deployment that is actually `GlobalStandard` or in another region, so every residency check passes on a false fact. | Registry validation compares each deployment's label with the SKU and region in the Terraform outputs; the audit record keeps both the label and the deployment's SKU and region. | S007, S008, S010 | Implemented in part: Terraform outputs each deployment's SKU and region and refuses a Global SKU (S007); registry validation checks every label against its SKU and region, and CI compares the registry with a committed snapshot of the outputs (S008). Residual: a pull request can change the snapshot with the registry, so the live comparison stays a manual `make registry-snapshot` until the pipeline has an Azure identity (S022); the audit record arrives in S010 |
| T-13 | TB-4 | I: special-category data in free text (a claimant mentions a hospital stay) reaches a model under the `personal` class. | The runtime classifies claimant text before a model call; the request's class can only be raised above the tenant's; a `special` request makes no model call, and the claim goes to the adjuster. Residual: detection is imperfect; the demo data is synthetic. | S014 | Designed |
| T-14 | TB-4 | R: a model call or a tool call leaves no audit record, so a decision cannot be reconstructed. | One audit record per model call and per tool call, written before the result returns; if the audit write fails, the call fails (QA-05). | S009, S010, S011, S013 | Implemented in part: the gateway and the runtime write an audit row per model call and per run step, and a failed audit write fails the call (S009); tool calls in S013 |
| T-15 | TB-4 | D: a looping agent exhausts the tenant's budget and the monthly Azure spend (C-04). | Per-tenant token budgets with the cost reserved before the call (QA-12); a step limit per graph run; Azure budget alerts at 50, 80 and 100 %, which detect and do not stop spend. | S007, S011, S014 | Implemented in part: the subscription budget and its three alerts exist (S007); token budgets and step limits arrive in S011 and S014 |
| T-16 | TB-4 | I: the Knowledge MCP Server sends claimant-derived query text to the gateway for embeddings without the run's tenant and class. | Embedding calls carry the calling run's tenant and data class and pass through the same routing and redaction as completions. | S012 | Designed |
| T-17 | TB-4 | D: the gateway or the database is down, and every triage run stops. | A fallback provider region (QA-04); probes and disruption budgets; a tested restore (QA-10). One instance each is accepted for this project (C-01). | S010, S019, S029 | Designed |
| T-18 | TB-5 | I: provider credentials leak through logs, traces, error messages or the repository. | Credentials live in Key Vault, read through workload identity (Kubernetes Secrets on kind); never logged; provider error bodies are summarised, not echoed; secret scanning (T-34). | S007, S010, S020 | Implemented in part: Azure OpenAI accepts Entra ID tokens only, with key authentication disabled (S007); the gateway's handling arrives in S010 |
| T-19 | TB-5 | I: a container other than the gateway calls a provider directly, bypassing residency, budget and audit. | Provider SDKs are imported only in the gateway package, enforced by an import contract; default-deny egress lets only the gateway reach provider endpoints. On kind that is simulated unless the cluster's network plugin enforces policy; in Azure it needs FQDN-aware egress or private endpoints. | S010, S019, S020 | Designed |
| T-20 | TB-5 | I: the provider processes or retains prompts outside the EU, or a sub-processor sees them. | Personal data goes only to EU regional or data-zone deployments; prompts are redacted before the call; each provider passes the onboarding checklist before its first deployment. | S010, S014, S034 | Designed |
| T-21 | TB-6 | E: an agent calls a tool outside its allowlist, or a tool whose scope is broader than the task. | Per-agent allowlists and scopes in the registry, enforced by the runtime's tool client and again by each MCP server. | S008, S013 | Implemented in part: allowlists, scopes and input schemas that are closed and bounded at every depth are declared in the registry and validated in CI (S008); the runtime and the MCP servers enforce them from S013 |
| T-22 | TB-6 | I: injected text steers an allowed tool to another claimant's record, such as a policy lookup for someone else's policy. | Every tool call is bound to the run's claim and tenant: the runtime passes them from the run context, and the server refuses a record outside them, whatever the model chose as arguments. | S013 | Designed |
| T-23 | TB-6 | T: a retried mutating call records a note or a request twice. | Every mutating tool requires an idempotency key and returns the original result on a repeat (QA-08). | S013, S015 | Designed |
| T-24 | TB-6 | S, E: something other than the runtime calls an MCP server directly and writes to the database. | MCP endpoints require the runtime's service identity; network policy admits only the runtime. | S019; no step yet for service-to-service identity | Designed |
| T-25 | TB-6 | E: a compromised service reads or changes another service's data in the shared database. | One database role per service, granted its own schema; a service that writes audit events gets insert on the audit table and nothing else, and a trigger stamps each row's time, ID and database role so a service cannot forge them; nothing can update, delete or truncate it; the role that owns the schemas runs migrations only. | S009 (schemas, roles and grants in the migrations), S041 (the roles on the cluster) | Implemented in part: schemas, grants and the insert-only triggers are in the migrations and tested against PostgreSQL in CI (S009); the roles on the cluster arrive in S041 |
| T-26 | TB-7 | T: the claimant's description carries instructions ("approve this claim") that steer tool calls or the proposal. | Claimant text is passed as delimited data; an injection-detection guardrail; allowlists and argument binding limit what a steered agent can do (T-21, T-22); the route is decided by rules (T-30); the injection suite measures the result (QA-09). | S014, S032 | Designed |
| T-27 | TB-7 | T: second-order content carries injected instructions: tool results, claim notes, claim history, retrieved wording, arriving documents. | All tool and retrieval results are passed as quoted data with their source; wordings enter only through the generator and a pull request; the same guardrails as T-26. | S012, S013, S032 | Designed |
| T-28 | TB-7 | T: a completion with invalid or out-of-range content (a payable amount above the limit) becomes a proposal. | The proposal is schema-validated; amounts, limits and cited clauses are checked against the policy and the wording before the proposal is stored. | S014 | Designed |
| T-29 | TB-7, TB-9 | T: the golden set's expected outcomes are changed to let a regression pass, or evaluated output injects instructions into the LLM judge. | Expected outcomes change only with the generator in a reviewed diff, and a test regenerates and compares them; rule graders decide route and amount, and the LLM judge grades groundedness only and cannot override them. | S003, S017 | Implemented in part: the regeneration test exists (S003); grader separation arrives in S017 |
| T-30 | TB-8 | E: the workload decides on its own what C-02 reserves for a person: an approval over the threshold or with a fraud flag, or a rejection on the merits. | Deterministic rules decide the route from the facts the model extracts, and any missing or uncertain fact routes to the adjuster; no approval over the threshold or with a fraud flag and no rejection on the merits completes without an adjuster (QA-06). The one rejection without a person is the procedural closure after a missed document deadline (C-02). | S014, S015, S017 | Designed |
| T-31 | TB-8 | E: a steered agent records an approval decision itself. | No agent allowlist contains a decision tool, and registry validation rejects one; the Claims Triage App records the decision from the adjuster's request, and the resumed run only reads it. | S008, S013, S015 | Implemented in part: registry validation rejects any allowlisted tool labelled `decision`, or whose name or scope carries a decision word (decide, approve, reject, decline or deny, in any inflection), and the registry declares none (S008). Residual: a tool's effect is declared by its author, so a mislabelled tool under a neutral name passes; the decision path arrives in S015 |
| T-32 | TB-8 | R, T: an adjuster's decision cannot be attributed, or is changed afterwards. | The Claims Triage App records the decision with the adjuster's identity from the token, the time and the proposal version; the audit table is insert-only for every service (T-25). | S015, S021 | Designed |
| T-33 | TB-8 | Oversight: automation bias; the adjuster confirms proposals without reading them, and human oversight becomes a formality. | The queue shows the cited clauses, the fraud indicators and the reasons next to the proposal; a decision is an explicit action, never a default. Residual: a process risk that software only reduces. | S016 | Designed |
| T-34 | TB-9 | I: a secret is committed to the repository. | gitleaks runs on every pull request and on `main` as the required `secret scan` check, and GitHub push protection blocks known secret formats. | S001 | Implemented: `protect-main` requires the check |
| T-35 | TB-9 | T: an unwanted registry or code change reaches `main`, such as a `global` deployment allowed for personal data or a widened allowlist. | Ruleset `protect-main`: a pull request, five required checks, no bypass; registry schema validation in CI, with personal data allowed only on EU labels. No review is required: there is one maintainer, so the checks are the gate, and a pull request can change a workflow that defines one. | S001, S008 | Implemented: the ruleset and its required checks (S001); registry validation runs in the required `python` job, with each data class's residency ceiling fixed in the validator's code, so personal data on a non-EU label fails unless the check itself changes (S008) |
| T-36 | TB-9 | T: a malicious or vulnerable dependency, action or image reaches the cluster. | Locked dependencies installed with `uv sync --locked`; Dependabot for uv and Actions; SBOM, image scan, signing and signature verification in the delivery pipeline. Actions are pinned to version tags, not commit SHAs, so a moved tag is trusted. | S002, S022 | Implemented in part: the lockfile and Dependabot exist; the image controls arrive in S022 |
| T-37 | TB-9 | I: the delivery pipeline's cloud identity or the Terraform state, which holds resource secrets, leaks. | GitHub reaches Azure through OIDC federation with no stored credential; the state lives in an access-controlled remote backend, never in the repository. | S007, S022 | Implemented in part: the state lives in Azure Storage with Entra ID authentication only, shared keys off, versioning, soft delete and a delete lock (S007); the pipeline's OIDC identity arrives in S022 |
| T-38 | TB-1, TB-2 | T, D: an uploaded document is malicious or oversized, or each upload starts a new, paid triage run. | Not designed: documents are metadata only until a step designs uploads. That step sets type and size limits, scanning, and a limit on triage runs per claim. | No step yet | Open |
| T-39 | TB-4, TB-9 | T, R: the gateway runs in replay mode where real claims arrive, so proposals come from hand-written text while every caller believes a model drafted them. | Replay is an explicit gateway mode, never a route candidate; the gateway starts in replay mode only when its environment is test, CI or kind; every response, audit record and span names the replay deployment and provider, and the stored proposal records the deployment that drafted it. | S009 | Implemented (S009): replay refused outside test, CI and kind, and named on every response, audit row, span and stored proposal |
| T-40 | TB-3, TB-9 | E: an installed package registers a graph under a registered agent's name, and the runtime runs its code with the runtime's credentials. | The runtime loads a graph only for an agent in the registry, only from an entry point the `meridian` distribution publishes, and refuses a name published twice, a target outside `meridian.workloads` and a module loaded from outside the installed package; dependencies are locked (T-36). It adds little against an attacker who can already write to the Python path, which T-36 covers. | S009 | Implemented (S009), tested with a planted distribution |
| T-41 | TB-5 | I: the agent framework's own tracing (LangSmith, pulled in by LangGraph) ships a run's state, the claim included, to a service outside the EU, past the gateway, the audit and the residency labels. | The runtime forces every LangSmith and legacy LangChain tracing switch off before LangGraph loads, and refuses to start when one was set, so a request for it fails loudly; telemetry leaves only through OTLP to the platform's own collector. | S009 | Implemented (S009), tested with a local stand-in for the LangSmith API |

### Hard rules and the register

The hard rules in `AGENTS.md` and the threats that test them:

| Hard rule | Threats |
|---|---|
| No secrets in the repository | T-06, T-18, T-34, T-37 |
| Synthetic data only | T-01, T-04 |
| EU residency is a label | T-11, T-12, T-16, T-20, T-35 |
| Every model call through the gateway | T-16, T-19, T-41 |
| Platform packages never import the agent framework | No threat; it is a maintainability rule, enforced by the import contract |
| Every tool declared, allowlisted and audited | T-14, T-21, T-22, T-23, T-24, T-31 |

No tension found: no designed mitigation needs a rule broken.

### Residual risk

- T-01 and T-38 are open. Claimant identity is a product decision (how a
  claimant proves who they are without a staff account) and waits for the
  claimant pages and the identity step. Document upload has no design yet.
- T-09 is accepted: workload code shares the runtime's process and
  credentials, which holds only while every workload is this repository's
  own reviewed code.
- T-13, T-26 and T-27 depend on detection that is never complete. The
  mitigations cap the damage (allowlists, argument binding, deterministic
  routing, a person on every material decision) rather than promising
  detection.
- T-33 is a process risk. The UI can make a careless approval harder; it
  cannot make it impossible.
- T-35: with one maintainer, nobody reviews a change but its author. The
  required checks carry the whole weight.
- Until service identity exists (S019), the runtime and the gateway trust
  the tenant and agent they are sent (T-08). In S009 the three services
  meet only inside a test process; from S041 they are reachable only inside
  the cluster. Until then, anyone who can reach a service can act as any
  tenant, read a run's status by its ID, and pick which of two duplicate
  headers wins.
- Provider-side abuse monitoring may retain prompts for a limited time under
  the provider's terms. It is accepted for synthetic data and recorded for
  the provider onboarding checklist in S034.
