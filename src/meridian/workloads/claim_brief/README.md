# claim-brief

The second workload of the platform, and the second agent framework: a short
brief of a claim for an adjuster, written on Microsoft Agent Framework, on the
same platform contract as the triage workload (ADR 2). It decides nothing about
the claim and never moves its state.

**Status.** Implemented and tested through the Agent Runtime's second host
(`meridian.runtime.agent_framework_host`), against PostgreSQL, the real tool
servers and a stub gateway. It is **not registered** (no entry in
`config/registry/` or in `pyproject.toml`), **not reachable through the run
API** and **has not run on a cluster**. The registration comes with the change
that wires the host into the runtime.

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
the Claims Triage App stores in its own table, and it is in the run's checkpoint
rows until the run ends.

## Tools

Five of the registry's six, with no worker: `policy_lookup`, `claim_history`,
`request_approval`, `approval_outcome` and `add_claim_note`.

## Tests

`tests/meridian/workloads/claim_brief/`.
