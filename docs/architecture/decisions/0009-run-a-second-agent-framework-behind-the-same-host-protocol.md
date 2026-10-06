# 9. Run a second agent framework behind the same host protocol

Date: 2026-10-06

## Status

Accepted

Amends [2. Run LangGraph behind a framework-agnostic platform contract](0002-langgraph-behind-a-framework-agnostic-contract.md)

The owner started the step on 2026-10-06 ("We can do s037 if we can now") and
answered three questions: the workload calls tools ("yeah it should call
tools"), it pauses for a person ("yeah"), and who may start it is "your
suggestion", which the session had named in its reply as the Claims API. The
other decisions below are the session's, and the owner may overturn any.

Implemented and tested off a cluster, not run on one: the second host, its
checkpoint store, the workload `claim-brief` and the Claims API's three brief
routes run in the test suite, through the real Agent Runtime, the three tool
servers, PostgreSQL and a stub model. Nothing here has run on kind or in Azure.

## Context

ADR 2 claimed that the platform's packages never import an agent framework and
that "a later switch would rewrite the workload and the runtime host, not the
platform". Until S037 that was a claim: one workload existed, on LangGraph, and
the Agent Runtime called LangGraph directly to compile, run, pause, resume,
read the output, keep checkpoints and make spans. ADR 2 also listed "a second
workload in that framework" as an optional milestone M4 item, and rejected a
second copy of the claims triage as two weeks for thin evidence. It rejected
Microsoft Agent Framework as the primary runtime on one ground that survived the
S005 spike: the framework ships no PostgreSQL checkpoint store, so this project
would write and maintain the store that holds paused runs and their personal
data.

S037 tests the claim for a second framework, Microsoft Agent Framework
(`agent-framework-core`). The work started from three facts. The runtime's
neutral code (the run API, run rows, resume-once, leases, audit, limits, the two
clients, metrics) needs three things of a framework: start a leg with an input,
resume a leg, forget a run's checkpoints. All six tools are bound to a claim.
And the pause needs a person, which the Claims API already records for the
triage.

## Decision drivers

- ADR 2: the platform's packages import no agent framework; the runtime's
  contract with a workload does not change for one framework.
- Hard rule 4: every model call goes through the Model Gateway, whatever the
  framework's own agent classes could do.
- Hard rule 6: every tool declared, allowlisted and audited; a mutating tool
  needs an idempotency key; a person decides at a pause.
- T-10, T-31, T-63: resume once, the decision is the Claims API's record and
  never the resume's value, and a checkpoint store never loads a type nobody
  registered.
- C-01: one maintainer. A second runtime service, a second database or a second
  copy of the triage is a thing to keep alive.
- The evaluation's baselines fingerprint the triage's registry entry (T-72):
  adding a field must not move them.

## Considered options

What the second framework runs:

1. A second triage in Microsoft Agent Framework (ADR 2's option 3).
2. A small claims workload of another kind that decides nothing about the claim:
   a brief of a claim, drafted by one model call, filed or not by an adjuster.

Where it runs:

3. In the Agent Runtime, one process hosting both frameworks behind one
   protocol.
4. In a second runtime service for the second framework.

How the runtime picks a host:

5. The registry names it, per agent, in a field.
6. The runtime guesses it from what the entry point returns, or reads a second
   entry-point group.

Where its checkpoints live:

7. A store of our own in PostgreSQL, JSON only.
8. The framework's file store, or its Cosmos DB store.
9. No pause at all.

## Decision

Options 2, 3, 5 and 7, with the probes of S005 and P0 first. Microsoft Agent
Framework runs behind the same `Host` protocol as LangGraph, on a second
workload, `claim-brief`, and not on a second triage. The registry's `host`
field picks each agent's host, and the runtime's own PostgreSQL table keeps the
second host's checkpoints.

How it is built:

- **A `Host` protocol in `meridian.runtime.hosts`, three operations.** `start`
  a leg with an input, `resume` a leg, `forget` a run's checkpoints. A leg ends
  `Completed` or `AwaitingApproval` with the output so far, or raises. The
  protocol names no framework's type, and a fresh-interpreter test holds that
  importing it loads neither framework. Everything else stays where it was, in
  the neutral code: the run API, the run rows, resume-once, leases, audit,
  limits and metrics. `LangGraphHost` (`langgraph_host.py`) is the old code
  moved behind the protocol, with the assertions of the existing runtime tests
  unchanged.
  `AgentFrameworkHost` (`agent_framework_host.py`) is new.
- **The registry names the host.** `Agent.host` is `langgraph` (the default) or
  `agent-framework`. The default is not written into an agent's dump, so the
  evaluation's `tools` fingerprint of `claims-triage` did not move
  (`tests/meridian/registry/test_hosts.py` pins the dump and the fingerprint).
  The registry refuses a `job` that names a host, and workers on the second host
  (the worker view of the tool client is built and tested for LangGraph only;
  the check goes when the second host carries workers). The service refuses to
  start when an agent's entry point is not what its host runs, in both
  directions, with a fixed sentence that copies nothing from a factory
  (`host_wiring.py`).
- **A checkpoint store of our own.** `runtime.workflow_checkpoints`
  (migration 0023, `workflow_checkpoints.py`): JSON only through a codec that
  refuses at save any type nobody registered, any key beginning `__`, a lone
  surrogate, a NUL and a float the database would hand back as an integer; and
  on load any tag or type name outside the registry, so no pickle and no type
  the workload did not list is ever revived. Rows are keyed by the run's thread
  and a checkpoint ID, "latest" is the table's own sequence and never a clock,
  and the runtime's role holds select, insert and delete only. The sweep's
  role finds and deletes a thread's rows and cannot read a body. This is the
  store ADR 2 said the project would have to write.
- **How the second host runs a leg.** One event loop and a new workflow object
  per leg, built from the workload's factory. The workload's steps get the
  runtime's `ModelClient` and `ToolClient` as thin asynchronous faces
  (`asyncio.to_thread` of the same calls), so every limit, audit row and refusal
  is the one the clients already had. A resume reads the latest checkpoint of the
  run's thread and takes one of three ways: one pending request, answer it; no
  pending request and a response in flight (a resume that failed after the
  answer), restore it and only the failed step runs again; neither, the failure
  word `no-pending-pause`. It never names a checkpoint a caller gave, because
  naming the pause's own checkpoint answers the pause a second time (P0). The
  response it passes is a marker type of its own and the runtime's resume value
  is ignored. The step bound is ten steps a leg, which on this framework means
  the restored count plus ten because the framework counts across legs.
- **The workload.** `claim-brief` (`src/meridian/workloads/claim_brief/`) has
  four steps in a line. `gather` calls `policy_lookup` and `claim_history`.
  `draft` makes one model call for a plain-text brief, from closed-set facts
  only and no free text. `ask` yields the brief, calls `request_approval` and
  pauses. `file` reads the recorded decision through `approval_outcome`, never
  the resume, and on approve writes one claim note of fixed text through
  `add_claim_note`. It decides nothing about the claim and moves no claim state.
  Its two writes use fixed `step` words, because the host re-opens a failed
  resume at the step that failed and a step may therefore run twice.
- **The Claims API starts it, an adjuster decides it.** `POST
  /claims/{id}/brief`, `POST /claims/{id}/brief/decision` and `GET
  /claims/{id}/brief` (migration 0024, `claims.briefs`). The decision is
  recorded in `claims.decisions`, keyed by the brief's own run, before the run
  is resumed, and the brief's text is redacted before it is stored or shown.
  The routes are API only: no page reads `claims.briefs`.

What the protocol had to gain or change to take a second host, from the reports
of the contracts that did it:

- **The `Host` protocol itself did not change** when LangGraph was fitted to it:
  `start`, `resume` and `forget`, with the signatures R4a wrote before R1 moved
  LangGraph behind them. Four things around it did.
- **`HostScope`.** The service keeps a scope per agent, not one host. LangGraph's
  saver is per request (a connection of its own, S015) and existing tests pin
  the order in which it opens, so its scope opens the saver and yields a host
  bound to it. The second host is one object for the life of the service, since
  a definition is used once per host instance.
- **`run_leg` builds the `ModelClient`** and hands both clients to the host. It
  used to be built inside `runs.execute`, which is gone. `forget` replaces the
  runtime's `_delete_checkpoints`, and the runtime records the run as ended
  before it forgets, which every host may rely on: the second host's delete is
  guarded by the run's status.
- **Start-time checks and widened types.** The service now calls each agent's
  factory at start with probe clients (`check_graph_factory`, `check_factory`).
  That is new behaviour for LangGraph too: a factory that raises when called
  stops the start where it used to fail the first request. The loader's return
  type widened to `AgentFactory`.
- **Failure words.** The second host adds `step-limit`, `checkpoint-not-saved`,
  `checkpoint-not-read`, `checkpoint-refused` and `workflow-changed`.
  `no-pending-pause` is the same word on both hosts, because the neutral code
  ends a run as failed for it. A resume that can never succeed ends the run
  (`runs.RESUME_CANNOT_SUCCEED`: `no-pending-pause`, `several-pending-pauses`,
  `checkpoint-refused`, `workflow-changed`), and one that may succeed pauses it
  again. LangGraph's step limit stays `unexpected`, as it always was, and the
  two words differ on purpose: sharing one would change an old run's audit row
  and a metric label.
- **Moves forced by the 800-line ceiling.** `settling.py` (the code that writes
  a leg's end to its run row) left `app.py`, and `runtime_calls.py` left
  `triaging.py`. `resume_command` moved into `langgraph_host.py`, so `runs.py`
  imports no framework.

Leaks the design accepts:

- **A run's input is the framework's first input.** The protocol passes it
  through as it is, so it sits in the first checkpoint row of either host's
  store until the run ends: the leak ADR 2 named, now on both. For the brief the
  input is six scalars of the claim (its ID, the policy number, two dates, the
  peril and the amount) and a count of documents, and no description, city,
  claimant or document name: the Claims API sends that whitelist, after a
  review found the claim's free text in the checkpoint rows.
- **The brief's own text is in the checkpoint rows while the run lives.** It is
  also in the runtime's answer to the Claims API (internal, mutual TLS). The
  stored copy and every answer of the three routes hold the redacted form.
- **A resume with nothing to resume is one failure word on both hosts**,
  `no-pending-pause`, because the neutral code ends a run as failed for that
  word and for no other that LangGraph can raise.
- **A failed save stops the leg at the next step boundary, not at once.** The
  framework only logs it, so the host reads the store's failures after the step.
  The step that was running has already acted, and a write inside it is
  protected by its idempotency key alone.
- **The second host's spans are made from the framework's events, after the
  fact,** and are siblings of the tool and model spans of the same step, not
  their parents (P0 measured it). The framework's event payloads hold claim
  content and never reach a span.

## Consequences

Positive:

- ADR 2's claim is a fact for a second framework: `claim-brief` runs on a
  framework the platform's packages import nowhere. The platform's own diff is a
  `host` field in the registry with two checks, one logger name in the log
  format's hold list and two migrations; the gateway, the tool servers, the
  evaluation harness and the knowledge store did not change. So ADR 2's "not the
  platform" holds with that one amendment: a framework is named in a platform
  package's data, never imported.
- The boundary is enforced, not hoped for. `meridian.platform` may not import
  `agent_framework`, and the contract names the framework's 29 adapter modules
  too; the eleven that make a model call are also named in the two
  provider-SDK contracts, which keep a provider SDK out of everything but the
  gateway's Azure adapter. A test fails when a release adds an adapter the
  contracts do not name, and another when `uv.lock` holds any `agent-framework-*`
  package but core. A sixth contract keeps an HTTP client out of the brief's
  package. `uv run lint-imports` ended with `Contracts: 6 kept,
  0 broken.` when this record was written.
- Model calls and tool calls still go through the gateway and the tool client.
  The host refuses at start, and at each leg, a workload step that is, or
  inherits from, a class the framework defines, because such a step would call a
  model with a chat client of its own.
- A second paused workload shows the runtime's resume-once, lease and sweep
  machinery is not LangGraph's: both hosts pass through the same run rows.

Negative / accepted trade-offs:

- **The project keeps the store ADR 2 said it would.** The checkpoint store and
  its codec are about 600 lines, the host about 670, with a migration and the
  tests that pin their refusals: the "most safety-critical storage in the
  platform, kept by one maintainer" of ADR 2's matrix. The framework ships no
  PostgreSQL store and every first-party store pickles.
- **A checkpoint is bound to the workflow that wrote it.** A resume compares the
  checkpoint's graph signature with the workflow's, and a stored state type that
  no longer fits the code is refused. Either ends the run as failed on its first
  resume (`workflow-changed`, `checkpoint-refused`), the brief closes as
  `failed`, and the rows are forgotten, because a run that can never resume
  would otherwise wait for ever. A release that renames or moves a step class,
  changes a step or an edge, adds a field to `Gathered` or `Drafted`, or bumps
  the framework so that it builds the signature differently, therefore fails
  every brief that is paused when it is deployed. The signature is
  deterministic across interpreters. During a rolling update an old pod that
  reads rows a new pod wrote ends the run for good, and each failed brief counts
  towards the five a claim may have. Two tests pin the signature and the state
  types' fields so that such a change touches them on purpose
  (`tests/meridian/workloads/claim_brief/test_brief_stored_shapes.py`). Not
  built: a release stamp on the rows, so that a pod of another release would
  pause the run again instead of ending it.
- **Framework-defined steps are refused, and the rule reaches inheritance
  only.** A step of the workload's own class that holds the framework's agent or
  a chat client and awaits it passes `check_definition`. That is held for
  `claim-brief` by its import allowlist (four names of the framework and no HTTP
  client, `test_brief_import_allowlist.py`), the sixth contract and review. The
  next workload on this host is held by review.
- **Two frameworks share one process** and the runtime's credentials: a fault
  in either dependency tree is the runtime's (T-09 accepts graph code in the
  runtime's process).
- **Forged step counts or names still pause for ever.** A checkpoint whose step
  count or step name is forged, with the graph's hash intact, gives
  `checkpoint-not-read`, which pauses the run again; the table's writers are the
  runtime's role and the schema's owner, so it takes one of them. A paused brief
  has no age bound, as a triage waiting for an adjuster has none (retention is
  S068's, and the periods are the owner's to choose).
- **Two failure words for one limit.** The second host's step limit is
  `step-limit` and LangGraph's is `unexpected`.
- **`forget` is guarded by the run's status and the sweep is the backstop**:
  under a run still `Running` or `AwaitingApproval` it deletes nothing. The
  sweep's role may delete a thread's rows table-wide (as on the three LangGraph
  tables), so a compromised sweep credential could end a paused brief's resume.
- **The resume value differs:** LangGraph passes the runtime's value to the
  graph and the second host drops it. Both workloads read the decision from the
  Claims API's record, so neither acts on it, and the protocol says neither.

The dependency, read from `pyproject.toml`, `uv.lock` and the installed
metadata:

- **`agent-framework-core` is pinned exactly at 1.19.0** and is not a
  pre-release: its version has no pre-release suffix and its classifier says
  `Development Status :: 5 - Production/Stable`. Its licence is MIT, by
  classifier (it carries no licence expression). It is the version the P0 spike
  measured. The design noted 1.20.0 as released on 2026-10-02, after it.
  Beside it the framework's persistent stores and most integrations are
  pre-releases (ADR 2's matrix, read on 2026-09-29), and none is installed.
- **It brings `msgspec` 0.21.1 (BSD-3-Clause, classified Beta) and
  `python-dotenv` 1.2.4 (BSD-3-Clause)**, and nothing else new to the lock: no
  provider SDK. `python-dotenv` was uploaded five days before it was locked,
  under the repository's seven-day rule for releases, and stays. The framework
  reads a `.env` file only where a caller names one and nothing here does; a
  fresh-interpreter test holds that, and that no socket is opened, no global
  tracer or meter provider is set and no provider SDK loads.
- **The framework's telemetry is off.** It is on by default, so the runtime sets
  its two variables before it is imported and the host sets the framework's
  settings object off as well (the variables are read too late when something
  imports the framework first). The platform sets no global tracer provider.
  Its loggers are held at WARNING (`HELD_AT_WARNING`, a list of names that
  imports nothing): the framework writes about a dozen INFO lines a leg.
- **Renovate has no rule for it.** The `pep621` reader puts it in the monthly
  `python` group, with the seven-day release age every PyPI package has. A bump
  that changes the graph's signature would strand paused briefs, so the pin test
  above is the guard until a rule that flags the bump exists (a backlog item,
  and `.github/renovate.json` is another step's).
- **`NOTICE` is unchanged**: it lists material copied into the repository, and
  these three packages are installed from PyPI, not copied.

Rejected options:

- Option 1, a second triage: ADR 2's reason stands (two weeks for thin
  evidence, and a second copy that rots), and a workload that moves a claim's
  state would have to share the triage's state machine. The brief decides
  nothing about the claim and never moves its state.
- Option 4, a second runtime service: a chart entry, a certificate, policies and
  an identity for a second copy of the neutral code.
- Option 6: guessing the host from what an entry point returns is implicit; a
  second entry-point group says one thing in two places.
- Option 8: the framework's file store keeps state on a pod's disk and, like
  every first-party store, pickles; its Cosmos DB store is a second database and
  Azure only.
- Option 9, no pause: the owner asked for one.
- A page for the brief now: the adjuster's page followed the decision API in
  S016 and this follows the same order. A second table for the brief's decision:
  `claims.decisions` already keys on a run and allows `approve` and `reject`.
- A base class every step must inherit so that the host opens its span inside
  the step: a workload SDK for one small workload. The spans stay siblings.
- Naming a checkpoint by an ID on resume, which answers a pause a second time.
- `approval_required: true` on `add_claim_note`: the tool server answers
  `approval-required` to every call of such a tool (`toolserver/pipeline.py`),
  so none could be filed. That the note follows a recorded approval is the
  workflow's code and the Claims API's record, as for the triage since S015; a
  tool-server check for it is not built.
- `wording_search` for the brief: one fewer service to authorise. The brief has
  five tools.

## Risks

- A release that changes the workflow strands every paused brief (see above).
  Mitigation today: the two pin tests and the README of the workload; designed,
  not built: a release stamp on the rows.
- The routes have no sign-in (T-69): whoever reaches the Claims API can start a
  brief or decide one, and a brief costs a model call from the shared tenant's
  budget. Bounded today by five briefs per claim and no brief for a closed claim.
- Composition in a future workload (above).

## Related

- Requirements: C-01
- Architecture views: Containers
- Other ADRs: [2. Run LangGraph behind a framework-agnostic platform
  contract](0002-langgraph-behind-a-framework-agnostic-contract.md),
  [3. Build a thin model gateway](0003-build-a-thin-model-gateway.md),
  [5. Split an agent into workers routed by
  code](0005-split-an-agent-into-workers-routed-by-code.md)
- Evidence: `spikes/s037-agent-framework-host/` (the probes P0, 79 tests against
  1.19.0), `src/meridian/runtime/hosts.py`,
  `src/meridian/runtime/agent_framework_host.py`,
  `src/meridian/runtime/workflow_checkpoints.py`,
  `src/meridian/workloads/claim_brief/README.md`, and the service acceptance of
  the brief, [service-acceptance-claim-brief.md](../../governance/service-acceptance-claim-brief.md)
