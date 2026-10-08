# Service acceptance applied to the claim brief

Status on 2026-10-06 (S037): **designed**, applied for the second time, on
paper. This is the [service acceptance checklist](service-acceptance.md) filled
in for the second workload, the claim brief (`claim-brief`, on Microsoft Agent
Framework), from the files of this repository on that date. No tool or gate
enforces the checklist; the rows say which checks already run. The workload
runs in tests with synthetic data only, and was **seen on kind once on
2026-10-06 under replay** (no model was called): the migrate Job applied its two
migrations (0023 and 0024), and a brief was started, read, approved and filed
for one claim and rejected for another. It has not run in Azure, and nothing is
deployed to AKS (M2 is todo). What was not seen on a cluster is listed once,
below.

Result: 30 items, **6 met**, **15 partly met**, **9 not met**, 0 not
applicable. No decision has been taken on the items that are not met, so the
claim brief is **not called accepted**. It is a small workload that exists to
test [ADR 2](../architecture/decisions/0002-langgraph-behind-a-framework-agnostic-contract.md)'s
claim for a second framework ([ADR 9](../architecture/decisions/0009-run-a-second-agent-framework-behind-the-same-host-protocol.md));
this document is the honest list of what stands between it and acceptance.

## What the evidence is, and is not

- Every row rests on tests, the registry check or the import contracts, and a
  row says where the one run on kind added to it. A "met" row means that, as the
  checklist defines it, and never means deployed in Azure.
- **Seen on kind, once, on 2026-10-06, under replay** (no model was called, so
  the brief is the replay's fixed sentence): after `make deploy` (the migrate
  Job applied 0023 and 0024, every pod ready with no restart) and `make smoke`
  (45 PASS, no FAIL or SKIP), a brief was started for a claim that waits for an
  adjuster, read, a decision that named another run was refused (409), the
  decision approved and the brief filed, the same decision posted again
  refused (409), and the adjuster's page showed one `brief.decided`. A second
  claim's brief was rejected, and a second brief for that claim then started. The
  runtime's run counter was split by agent (`meridian_agent="claim-brief"`:
  `paused` and `completed`). The logs Loki holds had no line with the replay's
  sentence, "simulated; no model was called" or the request fingerprint, with
  controls that the namespace and the runtime's own lines reach Loki.
- **Not seen on a cluster:** a brief whose run fails; a resume refused for a
  changed workflow (`workflow-changed`, `checkpoint-refused`); the sweep closing
  a brief left unfiled; a live model writing the brief (replay only); a second
  runtime replica; the brief's trace read in Tempo (the triage's was). No log
  line names the agent `claim-brief` (there were none), so "Loki holds no brief
  text" rests on the three needles and the route's lines alone.
- The workload was run in the test suite from its start through the pause to a
  filed note, beside a triage of the same claim, through the real Agent
  Runtime, the three tool servers and PostgreSQL, with a stub model
  (`tests/meridian/test_claim_brief_stack.py`). Through the real gateway in
  replay mode the brief is the replay's fixed sentence, which says it is
  simulated (`test_through_the_real_replay_gateway_the_brief_says_it_is_simulated_and_is_audited`
  in that file). No real model has written a brief.
- The routes were also tested against a stubbed runtime
  (`tests/meridian/workloads/claims_triage/test_claim_brief_start.py` and its
  neighbours).
- There is one maintainer and no review is required on a pull request (T-35).
  The step was read by the repository's reviewer agents (security, in three
  passes, platform boundary, Python and database), which are models and not
  people. What they left open is in the notes below, not hidden.
- The runbooks were not exercised and the objectives are proposals nobody has
  measured ([operations](../operations/README.md)).

## Who decides at its pause, and through which route

An adjuster decides, through `POST /claims/{id}/brief/decision` on the Claims
API, with a body of `decision` (`approve` or `reject`) and `run`, the brief's
run ID: it must be the claim's latest brief's, and a stale one is a 409. The
Claims API records the decision in `claims.decisions`, keyed by the brief's own
run, with its audit event `brief.decided`, in one transaction, and only then
resumes the run. The run reads the recorded decision through `approval_outcome`
and never takes the resume's value (T-31). On `approve` the run writes one claim
note of fixed text; on `reject` it writes nothing; it moves no claim's state
either way.

- **No sign-in.** Whoever reaches the Claims API can start a brief or decide
  one, and who decided is not recorded: T-69 and S021, as for the triage's JSON
  decision route.
- **No page.** No page reads `claims.briefs`. The adjuster's trail shows
  `brief.decided` as a row of plain text with no link to the brief, and no queue
  lists a brief. The brief itself is read with `GET /claims/{id}/brief`.
- **A claim that closed meanwhile.** A claim that is `approved`, `rejected` or
  `withdrawn` has no brief started, and a decision on a brief of a claim that
  closed since is recorded as `reject` whatever the body says (the audit reason
  is `claim-closed`): the brief closes as `rejected` and no note is written.
- **Decided once.** The same decision posted again goes on to resume again and
  writes its decision row and its audit event once; a different decision is a
  409.

## What its evaluation's gate does and does not prove

Its evaluation is published and empty. `meridian.evaluations` has an entry
`claim-brief` (`src/meridian/workloads/claim_brief/evaluation.py`), and
`data/evaluation/claim-brief/golden/` holds a manifest and a `cases.json` with
no case, as the scaffold writes them. The workload has no grader: `report()`
raises `NO_GRADERS`, and `submissions()` raises it as soon as a case exists, so
nothing can be posted and not graded.

What the gate proves:

- the entry point and the golden set load, the manifest names the workload and
  the empty set evaluates nothing
  (`tests/meridian/workloads/claim_brief/test_brief_registration.py`, which also
  shows in a fresh interpreter that loading the evaluation loads no agent
  framework);
- adding the agent did not move the triage's evaluation: the `tools`
  fingerprint of `claims-triage` is pinned and unchanged
  (`tests/meridian/registry/test_hosts.py`), and no baseline, recording or
  golden file of the triage changed in the branch.

What it does not prove:

- nothing about the brief is graded: not that it is accurate, short, free of
  a made-up identifier or grounded in the facts it was sent;
- no baseline, recording or fingerprint of the brief's prompt is compared in
  CI. The prompt has a version (a hash of its messages, the output cap, the
  length bound and the data class, `claim_brief/prompt.py`) that a test holds
  stable, and nothing but that test fails when it changes;
- `make eval` and `meridian eval compare` compare the triage's baselines and
  never loop over the published evaluations, so the brief cannot fail them;
- `meridian eval run --allow-empty` says that a set with no case evaluated
  nothing, and without the flag it fails (T-82).

## The checklist applied

| ID | Item | Status | Evidence | Note |
|---|---|---|---|---|
| SA-01 | Agent registered | Met | [`agents.yaml`](../../config/registry/agents.yaml), [registration test](../../tests/meridian/registry/test_claim_brief_registration.py), [host wiring](../../src/meridian/runtime/host_wiring.py) | `claim-brief` is a graph agent (the default kind) with `host: agent-framework`, `structured_outputs: false`, five tools and no worker. The runtime refuses to start when an agent's entry point is not what its host runs, in both directions ([`test_runtime_hosts.py`](../../tests/meridian/runtime/test_runtime_hosts.py)), and the registry refuses workers on this host |
| SA-02 | Tenant and data class | Met | [`tenants.yaml`](../../config/registry/tenants.yaml), [registration test](../../tests/meridian/registry/test_claim_brief_registration.py) | Only the tenant `claims-triage` lists the agent: class `personal`, and the four limits the triage has (10 requests per 10 s, 10,000 tokens per minute, 300,000 per day, EUR 10 per month), shared. `evaluation` and `development` do not list it. The workload's data class is held equal to the class of every tenant that lists it, and its 400-token output cap inside that tenant's tokens per minute |
| SA-03 | Tools declared | Met | [`tools.yaml`](../../config/registry/tools.yaml), [registration test](../../tests/meridian/registry/test_claim_brief_registration.py) | Five of the six tools, none new: `policy_lookup`, `claim_history` and `approval_outcome` read; `request_approval` and `add_claim_note` write. No `wording_search`: one fewer service to authorise. The tools' contracts are the triage's and did not change |
| SA-04 | Allowlist enforced | Met | [Refusal test](../../tests/meridian/test_claim_brief_refusal.py), [tool client tests](../../tests/meridian/runtime/test_tool_client.py), [tool server tests](../../tests/meridian/toolserver/test_tool_server.py) | The runtime's client refuses a tool off the agent's list before sending, and each server checks the list again for the run's own agent. A step of the brief that calls `wording_search`, through the real runtime, is refused, audited as `tool-not-allowed` and asks no server. The refusal is shown in tests only; on kind the brief's own calls were all allowed ones, so no refusal was seen there |
| SA-05 | Mutating tools and decisions | Partly met | [Workflow tests](../../tests/meridian/workloads/claim_brief/test_brief_workflow.py), [`tools.yaml`](../../config/registry/tools.yaml), [threat model](../architecture/security/threat-model.md) (T-23, T-31) | Both write tools take an idempotency key, made from the run, the tool and a fixed step word, because a failed resume re-opens the step that failed and `file` may run twice (one note, not two: `test_the_file_step_run_twice_writes_one_note_because_its_key_does_not_change`). The decision is recorded by the Claims API and the run only reads it (`test_the_resume_s_value_decides_nothing_only_the_recorded_row_does`). Not shown: the tool server does not check that a note follows a recorded approval, so a direct call to `add_claim_note` for a run with no decision files one; the gate is the workflow's code and the Claims API's record, as for the triage since S015. `approval_required` cannot stand in for it: the server refuses every call of such a tool. T-31's residual stands: a tool's effect is its author's label |
| SA-06 | Tool calls audited | Partly met | [Stack test](../../tests/meridian/test_claim_brief_stack.py), [tool server tests](../../tests/meridian/toolserver/test_tool_server.py), [threat model](../architecture/security/threat-model.md) (T-14) | The servers write the audit row in the transaction of the tool's own write, and a brief's rows are under its own agent and run (`test_a_brief_is_audited_under_its_own_agent_and_run`). T-14 is implemented in part, for the triage and the brief alike |
| SA-07 | Model calls through the gateway | Met | [`pyproject.toml`](../../pyproject.toml), [import contract tests](../../tests/meridian/test_import_contracts.py), [claim brief contract test](../../tests/meridian/test_import_contracts_claim_brief.py), [import allowlist test](../../tests/meridian/workloads/claim_brief/test_brief_import_allowlist.py) | The workflow reaches a model only through the runtime's model client, awaited, and the gateway; through the real gateway in replay mode the call is audited under `claims-triage`, `claim-brief`, `personal` and `replay`. The framework's 29 adapter modules are named in the platform's contract, the eleven that call a model also in the two provider-SDK contracts, a test over `uv.lock` fails when an adapter is locked, and the host refuses a step the framework defines (it would call a model with a chat client of its own). The brief's package may import four names of the framework and no HTTP client. Not caught: a raw socket or a call by composition in a future workload, since the host's rule is about inheritance; for the brief these tests and review hold it |
| SA-08 | No framework in platform packages | Met | [`pyproject.toml`](../../pyproject.toml), [ADR 2](../architecture/decisions/0002-langgraph-behind-a-framework-agnostic-contract.md), [ADR 9](../architecture/decisions/0009-run-a-second-agent-framework-behind-the-same-host-protocol.md) | `meridian.platform` may import none of `langgraph`, `langchain*` or `agent_framework` and its adapters, and a layers contract keeps workloads above the runtime above the platform. Enforced in CI; tests plant violations of each. `uv run lint-imports` ended with `Contracts: 6 kept, 0 broken.` on this date. The registry's `host` field names a framework as data and imports nothing |
| SA-09 | Callers | Partly met | [`services.yaml`](../../config/registry/services.yaml), [registration test](../../tests/meridian/registry/test_claim_brief_registration.py), [threat model](../architecture/security/threat-model.md) (T-08) | `claims-api` (which starts and resumes it) and `agent-runtime` (which runs it and names it to the gateway) list the agent, and no other service does. On kind and in tests only, for the triage and for the brief (on kind both services accepted the brief's calls under their entries). T-08 is implemented in part: the identity is a service's, not a person's, until S021 |
| SA-10 | What is sent to a model | Partly met | [Model call tests](../../tests/meridian/workloads/claim_brief/test_brief_model_call.py), [secrecy tests](../../tests/meridian/workloads/claim_brief/test_brief_secrecy.py), [stack test](../../tests/meridian/test_claim_brief_stack.py), [data classification](../architecture/security/data-classification.md), [threat model](../architecture/security/threat-model.md) (T-73) | The model is sent numbers, dates, booleans and words of closed sets, and no free text: not the description, the document names, the city, the claimant, the claim's ID or the policy number; a test plants a canary in every free-text field and finds none at the model. The call carries the class `personal`; the gateway routes personal data to EU labels only (the triage's tests). The two screens are not applied, because no claimant text is sent. The brief's text is redacted (e-mail addresses, IBANs, card and phone numbers) before it is stored or shown; names and addresses are not masked (T-73). The model is simulated wherever the brief has run: no real model has written one |
| SA-11 | Synthetic data only | Partly met | [Stack test](../../tests/meridian/test_claim_brief_stack.py), [data classification](../architecture/security/data-classification.md), [threat model](../architecture/security/threat-model.md) (T-04) | The claims, policies and histories the tests use come from the seeded generator. Nothing stops a person posting a real claim to the Claims API and starting a brief for it (T-04, as for the triage) |
| SA-12 | Human oversight | Partly met | [Workflow tests](../../tests/meridian/workloads/claim_brief/test_brief_workflow.py), [stack test](../../tests/meridian/test_claim_brief_stack.py), [quality attributes](../architecture/requirements/quality-attributes.md) (QA-06) | The brief decides nothing about the claim and never moves its state (the test fixture fails any test whose calls changed a claim's row); its only write after the pause waits for a person's recorded decision, and a reject writes nothing. Not shown: no absolute grader covers it (it has no grader at all), and with no sign-in anyone who reaches the Claims API can record the decision (T-69) |
| SA-13 | People are identified | Not met | [Threat model](../architecture/security/threat-model.md) (T-05, T-32, T-69) | By default the three brief routes have no sign-in, and the decision names no person. Since 2026-10-08 (S021) the routes demand a staff session or token with the adjuster role when the Claims API's switch `MERIDIAN_SIGNIN` is `staff`; it is off by default, was seen on kind against a mock issuer, and no deployment turns it on. Entra ID sign-in, the tenant from the token and who decided are S021's, not built |
| SA-14 | Audit trail | Partly met | [Trail test](../../tests/meridian/workloads/claims_triage/test_claim_brief_trail.py), [`0024`](../../src/meridian/platform/migrations/0024_claim_briefs.sql), [quality attributes](../architecture/requirements/quality-attributes.md) (QA-05), [threat model](../architecture/security/threat-model.md) (T-14) | The decision and its audit event `brief.decided` are written in one transaction, name the claim, and are in the claim's trail on purpose (on kind the adjuster's page showed one `brief.decided` for each decided brief). The run's own events are under the agent `claim-brief` and are not in the adjuster's trail, and the sweep's event for an abandoned brief run names no claim. A brief's own state changes (start, close) leave no event of their own. Who decided is not known (S021); the QA-05 reconciliation is not built |
| SA-15 | Evaluation gate | Not met | [Evaluation README](../../data/evaluation/README.md), [`evaluation.py`](../../src/meridian/workloads/claim_brief/evaluation.py), [golden set](../../data/evaluation/claim-brief/golden/manifest.json), [threat model](../architecture/security/threat-model.md) (T-72) | The golden set holds no case, the workload has no grader, and there is no baseline, recording or fingerprint of the brief that CI compares. What the gate proves and does not is the section above |
| SA-16 | Injection suite | Not met | [Model call tests](../../tests/meridian/workloads/claim_brief/test_brief_model_call.py), [stack test](../../tests/meridian/test_claim_brief_stack.py), [threat model](../architecture/security/threat-model.md) (T-26, T-83) | No injection case concerns the brief and none is measured. By construction no text a third party wrote reaches the model and the model chooses no tool and no route, which tests show with canaries; that is not a measurement, so the item is marked not met and not not-applicable |
| SA-17 | Threat-model note | Partly met | [Threat model](../architecture/security/threat-model.md) (T-93 to T-99) | Seven rows were written at the step's close, from the code and the reviews: the second host's checkpoint store (T-93), the three brief routes (T-94), the brief's stored text (T-95), what a step can reach (T-96), the dependency (T-97), a release that strands waiting runs (T-98) and the trail (T-99), and T-25, T-63, T-69 and T-77 were changed. The step's design note came before the code and is outside the repository, so the repository does not show that each row was written before the code it covers |
| SA-18 | SLOs | Not met | [SLOs](../operations/slo.md) | No objective names the brief. The runtime's run and model-call counters carry the agent as a label (S064), so an indicator could be computed per agent; none is defined and no target was measured |
| SA-19 | Alerts and dashboards | Partly met | [Alert rules](../../infra/kind/alerts/meridian.yaml), [SLOs](../operations/slo.md), [operations](../operations/README.md#alerts) | No rule or panel names the brief. The runtime's counters carry its agent, and the presence rules read the run counter whatever the agent. On kind `meridian_runtime_runs_total` was seen split by agent for the brief (`paused` and `completed`), after the next export and scrape; no rule or panel read it |
| SA-20 | Runbooks written | Not met | [Runbooks](../operations/README.md#runbooks) | No runbook names a failure of the brief that an operator can act on: a brief that waits for ever (its run and checkpoint rows have no age bound, and it blocks the claim's one open brief), a release that strands paused briefs, or a missing migration, which fails one of the sweep's statements. The database runbook covers the database only |
| SA-21 | Runbooks exercised | Not met | [Operations](../operations/README.md) | None has been exercised for the brief |
| SA-22 | Chart | Partly met | [Chart](../../infra/helm/meridian/), [chart tests](../../tests/meridian/test_helm_chart.py) | The brief adds no service, image or chart entry: it runs in the Agent Runtime and the Claims API, which the hardened chart already runs. The chart did not change. On kind `make deploy` applied the two migrations the brief adds (the migrate Job), every pod was ready with no restart, and `make smoke` printed 45 PASS; the chart's other limits (one replica, nothing in Azure) are the triage's |
| SA-23 | Network isolation | Partly met | [Network policies](../../infra/helm/meridian/templates/networkpolicy.yaml), [threat model](../architecture/security/threat-model.md) (T-84) | The brief opens no network path: the Claims API calls the runtime, the runtime the gateway and the tool servers, as for the triage. The policies were not changed; the brief ran once on kind with them in place, and no probe of a blocked path was made for it |
| SA-24 | Service identity and certificates | Partly met | [`services.yaml`](../../config/registry/services.yaml), [ADR 4](../architecture/decisions/0004-prove-service-identity-with-mutual-tls.md) | No service or certificate was added. The two services that name the agent are held by the registry as in SA-09; the brief ran once on kind, where these services serve mutual TLS (S055), and nothing in that run checked its certificates |
| SA-25 | Cost budget per tenant | Partly met | [Limits tests](../../tests/meridian/workloads/claims_triage/test_claim_brief_limits.py), [`tenants.yaml`](../../config/registry/tenants.yaml), [quality attributes](../architecture/requirements/quality-attributes.md) (QA-12) | The brief's call is a gateway call under the tenant `claims-triage` and counts against its windows and budget, which it shares with the triage: it has none of its own. A brief's call is capped at 400 output tokens, a claim may have five briefs and a closed claim none. Not shown: anyone who reaches the Claims API, with no sign-in, can still spend the shared tenant's budget on briefs and starve the triage |
| SA-26 | Rollback | Not met | [Rollback runbook](../operations/runbooks/rollback.md), [workload README](../../src/meridian/workloads/claim_brief/README.md) | No rollback was run. A release that changes the workflow's steps or edges, or a state type's fields, ends every brief that is paused when it is deployed (`workflow-changed`, `checkpoint-refused`); two tests pin them ([`test_brief_stored_shapes.py`](../../tests/meridian/workloads/claim_brief/test_brief_stored_shapes.py)), and a rollback across such a change is no safer. No release stamp on the rows exists |
| SA-27 | Recovery of data | Not met | [Quality attributes](../architecture/requirements/quality-attributes.md) (QA-10), [database failure runbook](../operations/runbooks/database-failure.md) | `claims.briefs` and `runtime.workflow_checkpoints` are in the one database with no backup on kind and no restore run (S029) |
| SA-28 | Retention | Not met | [Data classification](../architecture/security/data-classification.md#retention), [`0024`](../../src/meridian/platform/migrations/0024_claim_briefs.sql), [threat model](../architecture/security/threat-model.md) (T-49) | `claims.briefs.brief` has no retention rule, and a brief that nobody decides keeps its run and its checkpoint rows, which hold the unredacted brief, with no age bound (on kind a second brief for a claim was left waiting and still waits): the periods are the owner's to choose. S068 did not build the brief's expiry (it waits for the owner's answer on an undecided brief) and built the mechanism for the audit table and the ledger only (S070) |
| SA-29 | Capability labels | Partly met | [README](../../README.md#what-it-does-and-what-is-real-today), [workload README](../../src/meridian/workloads/claim_brief/README.md), [registry README](../../config/registry/README.md), [constraints](../architecture/requirements/constraints.md) (C-07) | The README's rows, the workload's README and the registry's say implemented and tested, not run on a cluster, with an empty evaluation. Nothing reads the labels (`make docs` does not); the sweep of every capability is S035 |
| SA-30 | A second workload | Partly met | [Scaffold](../../src/meridian/platform/cli/workload.py), [ADR 9](../architecture/decisions/0009-run-a-second-agent-framework-behind-the-same-host-protocol.md), [registration test](../../tests/meridian/workloads/claim_brief/test_brief_registration.py) | The brief is the second workload, and was not made by the scaffold: the scaffold writes a LangGraph graph with the default host, and the brief's host line, state types, workflow, store and routes were written by hand, on the scaffold's evaluation template. What a second framework needed is in ADR 9. The runtime's, registry's and scaffold's tests no longer assume one graph agent: with a scaffolded workload in the tree only the pin `NAMES` fails, on purpose |

## Items not met, and what would meet each

| ID | What would meet it | Step |
|---|---|---|
| SA-13 | Sign-in against Entra ID, the roles, the tenant from the token, the decision recorded with who made it, for the three brief routes too | S021 (needs S020) |
| SA-15 | Cases from a seeded generator, graders for a brief, a baseline and a CI comparison that includes the brief | None |
| SA-16 | Injection cases for the brief, measured, or a recorded decision that the item does not apply to a workload that sends no text a third party wrote | None |
| SA-18 | An objective for the brief with an indicator and a measured target | S027 (thresholds), none for the objective |
| SA-20 | Runbooks for a brief that waits for ever, for a release that strands paused briefs and for a missing migration | None |
| SA-21 | Each runbook run against a failure | S022, S028 |
| SA-26 | A rollback run, and a release stamp on the checkpoint rows so that a pod of another release does not end a paused run | S022; none for the stamp |
| SA-27 | A backup kept outside the environment and a restore, timed | S029 |
| SA-28 | A retention rule for `claims.briefs` and a bound on an undecided brief | S070 |

## Items partly met, and what is left

| ID | What is left | Step |
|---|---|---|
| SA-05 | The tool server does not check that a note follows a recorded approval; a tool's effect rests on its author's label | None |
| SA-06 | A request the SDK rejects leaves no row (T-14) | None |
| SA-09, SA-24 | Identity of a person; a check of the brief's certificates on the cluster | S021 |
| SA-10 | Names and addresses are not redacted (T-73); no real model has written a brief | S067 (in part); none for the rest |
| SA-11 | Nothing stops real data typed into the Claims API | None; the routes stay off the public internet (T-01) |
| SA-12 | An absolute grader for the brief; a person's identity on the decision | S021; none for the grader |
| SA-14 | Who decided; the QA-05 reconciliation; an event for a brief's own state changes | S021; none |
| SA-17 | Writing the note before the code is a habit, not a check | None |
| SA-19 | A rule or a panel for the brief | S062, S064, S028 |
| SA-22, SA-23 | The cases not seen on a cluster (above); one replica; Azure | S020 |
| SA-25 | A budget of the brief's own, or a sign-in, so that the shared tenant cannot be spent on briefs by a caller nobody knows | S021 for the sign-in; none for a budget of its own |
| SA-29 | A sweep of every capability's label | S035 |
| SA-30 | A scaffold for a workload on the second host; a third workload | None |
