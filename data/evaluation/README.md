# Evaluation baseline

`claims-triage-baseline.json` is the committed report of the claims-triage
workload's scripted run over the 40 synthetic claims: one case per claim, ten
rule grades per case, and three fingerprints (the prompt, the tools, the golden
set) that say what the run was made from. CI replays the golden set through the
stack, writes a new report and compares it with this file
(`meridian eval compare`); a regression, a broken rule or a changed
fingerprint fails the gate.

The file changes only through `make eval-baseline`, in a diff someone reviews.
Never edit it by hand. A change to the prompt, a tool's contract or the golden
set changes a fingerprint, so the gate asks for a new baseline in the same
change (T-29); the reviewer reads the grade diff, not just the hashes.

What each fingerprint covers:

- `prompt`: what the model is sent, the system message, the user message's
  format, the output budget, the length limit and the answer schema
  (`assessment.py`); not the model, the deployment or the answer parser.
- `tools`: the agent's registry entry, with its allowlist, and the registry
  entries of the tools on it; not the tool servers' entries.
- `golden_set`: the generator's version and seed, the hash of the whole
  manifest and the hash of every file it lists, each checked against the
  file's bytes when the report is written, so a file edited without
  `make synthetic` fails the run.

## The graders

All are rules, graded per claim against the oracle in
`data/synthetic/expected-outcomes.json`. Route and amount are decided by rules,
so a rule can grade them; an LLM judge for the model's rationale is S050.

- `completed`: the run left a proposal. Absolute: must pass on every claim.
- `route`: the route equals the oracle's. Target: 90 % of the claims (QA-06).
- `reason`: the reason equals the oracle's.
- `recommendation`: approve, reject or none equals the oracle's.
- `payable_amount`: the amount in euros equals the oracle's.
- `fraud_indicators`: the indicators equal the oracle's.
- `missing_documents`: the missing documents equal the oracle's.
- `exclusion_clause`: the exclusion's clause equals the oracle's (set only when
  the reason is `excluded`).
- `citations`: the cited clauses, in order, with the policy's product and
  wording version, equal the oracle's.
- `human_oversight`: no claim is decided without a person where one must
  decide. False for an automatic approval with no amount, above the limit or
  with a fraud indicator; for a rejection recommended off the adjuster's route;
  and for a wrong route that is not the adjuster's. Absolute: QA-06's other
  half.

## Who answered

`answered_by` is `scripted`, labelled `simulated` (T-72). The scripted model
answers from the oracle and ignores the prompt, so these grades are the
pipeline's (the rules, the retrieval, the tools and the storage), not a real
model's. The prompt's fingerprint still changes with the prompt, so a prompt
change is reviewed even though it cannot move a grade here. S050 records a run
with a real model.

## Commands

- `make eval` replays the golden set through the stack with the scripted model
  (needs Docker), writes `.eval/claims-triage-report.json` and compares it with
  the baseline.
- `make eval-baseline` regenerates this baseline from the same run, after a
  reviewed prompt, tool or golden-set change.
