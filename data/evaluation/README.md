# Evaluation files

What the claims-triage workload's evaluation is made from and what it
produced. Everything here is synthetic claims and a model's answers to them.
Never edit a file by hand: each has a command that writes it.

| File | What it is | Written by |
|---|---|---|
| `recordings/claims-triage.json` | The answers `gpt-4o` gave to the golden set's triage questions and to the judge's, recorded once from Azure OpenAI | `make eval-record` |
| `claims-triage-baseline.json` | The report of the golden set answered from that recording: the gate's baseline | `make eval-baseline` |
| `claims-triage-live.json` | The report of the live run that made the recording, with its latency | `make eval-record` |
| `claims-triage-live-variant.json` | The report of a live run with a variant prompt, not recorded | `make eval-record` |
| `prompt-comparison.md` | The two live reports side by side (`meridian eval diff`) | `make eval-record` |

## The gate

CI answers the 40 golden claims through the real services with the Model
Gateway in `recorded` mode, writes a report and compares it with the
baseline (`meridian eval compare`). It fails on a grade that passed in the
baseline and fails now, on a broken absolute grader, on a missed target and
on a changed fingerprint. The baseline is the recorded real model's run
(the owner's decision, 2026-10-04): 38 of 40 on `recommendation`, 40 of 40
on every other grader.

A report carries five fingerprints, and a change to any of them asks for a
new baseline in the same reviewed change (T-29, T-72):

- `prompt`: what the model is sent, the system message, the user message's
  format, the answer schema, the output budget and the length limit
  (`assessment.py`); not the model, the deployment or the answer parser.
- `judge`: the same for the judge's prompt (`evaluation/judge.py`).
- `recording`: the recording file's bytes.
- `tools`: the agent's registry entry, with its allowlist, and the registry
  entries of the tools on it; not the tool servers' entries.
- `golden_set`: the generator's version and seed, the hash of the whole
  manifest and of every file it lists, each checked against the file's
  bytes; a file in the golden set's directory that the manifest does not
  list is refused.

## The recording

An entry is found by the SHA-256 of the request the provider is given: the
messages after the gateway's redaction, the output budget and the answer
schema. It holds the answer's text, the finish reason, the provider's model
string, the token counts and the latency of the live call, and nothing
else: no request, no header, no identifier (T-76; a test reads every file
here for an Azure host, an account name, an email address and a GUID).

A changed prompt or schema finds no entry. The gateway then answers 502,
the run fails, and the evaluation says how many requests have no recording
and for which prompt the file was made. Record again with
`make eval-record`: about 60 chat calls on the live model, with an Azure
login, measured at EUR 0.12 on 2026-10-03. Then `make eval-baseline`, and
read the grade diff before committing it.

## What is real and what is not

- The chat answers are a real model's: `gpt-4o` (2024-11-20) on the regional
  deployment in Sweden Central, live in `claims-triage-live*.json` and
  replayed from the recording in the baseline (`answered_by`: `live` or
  `recorded`, both labelled `real`).
- The embeddings and so the wording search are simulated in every run here,
  the recording run included: the stack's first gateway stays in replay
  mode, so the clauses found, and each chat request, are the same when
  recording and when replaying. What a real embedding does to retrieval is
  not measured.
- A recorded run exercises the gateway's path (routing, limits, ledger,
  audit) and no provider. Its latency is unset: the ledger would time a file
  read. Its cost is real in this sense only: `recorded-chat` carries the
  price of the deployment that answered, so the ledger charges the recorded
  tokens what the live run was charged.
- A report's `input_tokens` sums the run's chat tokens and its simulated
  embedding tokens; the cost is the chat's alone, since a replay embedding
  costs nothing.
- `recorded` and `real` are labels the harness writes. A hand-written
  recording with a regenerated baseline would pass CI; only the review of
  the diff under this directory stops it (T-72).

## The graders

Ten rule graders compare the proposal with the oracle in
`data/synthetic/expected-outcomes.json`, without the judge:

- `completed`: the run left a proposal. Absolute: must pass on every claim.
- `route`: the route equals the oracle's. Target: 90 % of the claims (QA-06).
- `reason`, `recommendation`, `payable_amount`, `fraud_indicators`,
  `missing_documents`, `exclusion_clause`, `citations`: each equals the
  oracle's.
- `human_oversight`: no claim is decided without a person where one must
  decide. Absolute: QA-06's other half.

Three more:

- `groundedness`: the LLM judge's one verdict, whether the facts the model's
  rationale relies on are in the claim's description and the candidate
  clauses. True for a proposal with no rationale. The judge runs as the
  registry agent `evaluation-judge`, which has no tool, under the tenant
  `evaluation`; anything but a strict `{"grounded": true, ...}` is false. It
  is in neither the absolute graders nor a target, and it never changes a
  rule grade (T-29, T-77).
- `cost`: the claim's model cost in the gateway's ledger is at most
  EUR 0.02 (QA-07).
- `latency`, in a live report only: the claim's model calls took at most
  30 s in the ledger (QA-01's bound with `gpt-4o`).

Each case also keeps, for a reader and never compared: `observed` (the
route, the reason, the amount, the model's rationale and the judge's
reason, both redacted), `tools` (the name and the arguments of every tool
call of the run, taken from the calls themselves) and `measured` (model
calls, tokens, cost).

## Commands

- `make eval`: the golden set through the stack with the recorded answers
  (needs Docker), then the comparison with the baseline.
- `make eval-compare`: the comparison alone; it refuses a report older than
  a tracked file it is made from.
- `make eval-baseline`: regenerate the baseline after a reviewed prompt,
  tool, recording or golden-set change.
- `make eval-record`: record again from the live model. It spends money and
  needs an Azure login.
- `uv run meridian eval diff A B`: two reports side by side, as
  `prompt-comparison.md`.
- `uv run meridian eval run --base-url URL --report FILE`: post the golden
  set's claims to a deployed stack, read each proposal and grade the ten
  rules. No judge, no ledger and no tool capture over HTTP. A claim the
  stack has already triaged is skipped, so the report holds only what ran
  and is not comparable with the baseline.

## What the first live run found (2026-10-03)

- Route, reason, amount, citations, indicators, documents and the exclusion
  clause equal the oracle on all 40 claims; both absolute graders pass.
- `recommendation` is 38 of 40. CLM-0012 is withheld from the model by the
  special-category screen (S047). On CLM-0034 the model answered `unsure`
  (the description does not say whether wear and tear played a part), which
  the rules read as no assessment: the claim keeps its route to an adjuster
  and carries no recommendation.
- The three automatic approvals a simulated model could not confirm
  (CLM-0011, CLM-0015, CLM-0023) are confirmed.
- The model was asked on 14 claims: 0.8 to 2.1 s and at most EUR 0.0023 per
  claim, EUR 0.029 for the set.
- The variant prompt, which asks the rationale to quote the claimant, is
  worse: it gives CLM-0034 a recommendation the judge calls ungrounded, and
  it loses the exclusion on CLM-0038. `prompt-comparison.md` has the rows.
