# claim-brief

The second workload of the platform, and the second agent framework: a short
brief of a claim for an adjuster, written on Microsoft Agent Framework, on the
same platform contract as the triage workload (ADR 2). It decides nothing about
the claim and never moves its state.

**Status.** Implemented and tested through the Agent Runtime's second host
(`meridian.runtime.agent_framework_host`), against PostgreSQL, the real tool
servers and a stub gateway (the workload's own tests use `hostsupport.Gateway`,
an `httpx.MockTransport`; the stack test uses a stub model, and one of its tests
goes through the real gateway in replay mode). It is **registered** (S037): the agent
`claim-brief` is in `config/registry/agents.yaml` with `host: agent-framework`,
the tenant `claims-triage` lists it, and its entry point is in the
`meridian.graphs` group of `pyproject.toml`. It is **reachable through the run
API** (the Claims API's three brief routes start it, record the decision and
read the brief), and it **has run in tests** through the real runtime, the three
tool servers and a stub model (`tests/meridian/test_claim_brief_stack.py`). It
was **seen on kind once on 2026-10-06 under replay** (no model was called): a
brief started, read, approved and filed for one claim and rejected for another;
not seen: a failed run, a changed-workflow refusal, the sweep closing a brief, a
live model. Its evaluation is published and empty: the golden
set (`data/evaluation/claim-brief/golden/`) holds no case, because the workload
has no graders yet (designed, not built).

## The four steps

| Step | What it does |
|---|---|
| `gather` | `policy_lookup`, then `claim_history`; keeps only the facts below. |
| `draft` | One model call through the gateway for a brief in plain text. |
| `ask` | Yields `{"brief": text}`, calls `request_approval`, then pauses. |
| `file` | Reads the decision with `approval_outcome`, then files or not. |

The output of the paused leg is `{"brief": text}`; after the resume it is
`{"brief": text, "filed": true}` (an `approve`) or `"filed": false` (a
`reject`). A decision that is not recorded, or any other word, fails the run
with `decision-not-recorded` and the run can be resumed once it is. The step
after the pause can run twice (a failed resume re-opens it), so both writes go
through a fixed `step` word: the second run writes no second row.

## What it sends to the model

Numbers, dates, booleans and words of closed sets, and nothing a person wrote as
free text: not the description, the document names, the city, the claimant, a
policy's product or a history entry's text. The list, with each field's source,
is in the docstring of `facts.py` and held by a test that plants a canary in
every free-text field of the input and of both tools' results. The call carries
the data class `personal` and a cap of 400 output tokens (see `prompt.py` for
why). The prompt is a constant with a version beside it, a hash of the messages,
the cap and the class.

## What it writes

Two rows, both keyed by its own run and both of fixed text: an approval request
(`claims.approval_requests`) and, on an approve, one claim note
(`claims.notes`). Never the model's text. The brief is the run's output, which
the Claims Triage App redacts (the function that redacts the triage's rationale:
e-mail addresses, IBANs, card and phone numbers) and stores in its own table,
and it is in the run's checkpoint rows until the run ends.

## Tools

Five of the registry's six, with no worker: `policy_lookup`, `claim_history`,
`request_approval`, `approval_outcome` and `add_claim_note`.

## Changing it while a brief waits

A paused brief keeps its graph and its two state types (`Gathered`, `Drafted`)
in its checkpoint rows, and a run whose stored state no longer fits the code, or
whose graph changed, ends as failed on its first resume. So a renamed or moved
step class, a changed edge, a field added to a state type, or a framework
upgrade that changes how the graph's signature is built fails every brief that
waits when it is deployed: release it when none waits, or make it compatible.
Two tests (`test_brief_stored_shapes.py`) pin the graph's signature and the
state types' fields, so such a change has to touch them on purpose.

A bump of `agent-framework-core` is such a change: Renovate gives it a pull
request of its own, which needs those two tests green and the release notes
read, and which is deployed only when no brief waits, that is, when
`claims.briefs` has no row in state `awaiting_decision` (or `drafting`, a run
still under way).

## What it may import

Four names of the framework (`Executor`, `WorkflowContext`, `handler`,
`response_handler`) and no HTTP client, so a step cannot hold the framework's own
agent or chat client, or post to a model itself, outside the gateway: the host
refuses a step the framework defines, and this is held where imports are held
(`test_brief_import_allowlist.py`, and an import contract in `pyproject.toml`).

## Tests

`tests/meridian/workloads/claim_brief/` (the workload on the second host, its
registration and its empty evaluation) and
`tests/meridian/test_claim_brief_stack.py` (a brief from its start through the
pause to a filed note, beside a triage of the same claim, through the real
runtime and tool servers).
