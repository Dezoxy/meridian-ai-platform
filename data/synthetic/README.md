# Synthetic data and golden set

A seeded generator writes the data of a fictional insurer, Meridian Insurance,
which operates in eurozone Central Europe: policies, claim history, four
policy-wording documents and 47 first-notice-of-loss (FNOL) claims, each with a
labelled expected outcome: 40 drawn from the first random stream, and 7 added
after them from a second one. The set of expected outcomes is the golden set.
Status: implemented. The evaluation harness, retrieval and the MCP tool
servers read these files, so the formats below are a contract.

Everything here is synthetic and fictional, as
[constraint C-03](../../docs/architecture/requirements/constraints.md)
requires of a public repository. Names are random pairs from short lists of
common given names and surnames of Austria, Slovakia, Slovenia, Croatia and
Hungary. E-mail addresses use the reserved `example.com` domain. There are no
phone numbers, dates of birth, IBANs or national IDs. Addresses combine an
invented street name with a real city and carry no postcode. A vehicle
registration is `SYN-` and four digits, a format that no country issues.

The output is committed, so a changed label shows in the pull request diff. A
test regenerates the data and compares it with the committed files.

## Files

| Path | Holds | Records |
|---|---|---|
| `policies.json` | Policies, sorted by `policy_number` | 56 |
| `claim-history.json` | Prior, closed claims, sorted by `history_id` | see manifest |
| `claims.json` | FNOL inputs only, never labels, sorted by `claim_id` | 47 |
| `expected-outcomes.json` | A list with one label record per `claim_id`, sorted by `claim_id` | 47 |
| `wordings/<CODE>.md` | The policy wording of each product | 4 |
| `manifest.json` | Seed, counts and a SHA-256 of every other file | 1 |
| `injection/cases.json` | Injection cases: golden claims that carry an attack or a look-alike text | 94 |
| `injection/manifest.json` | Seed, counts and a SHA-256 of the case file, and of the golden manifest it was built from | 1 |
| `generator/` | The generator; run it with `make synthetic` | code |

Money is whole euros as JSON integers. There are no floats anywhere, and the
currency is `EUR` (stated once, in the manifest). Dates are ISO 8601 strings.
Fields keep the order shown in the tables. Lists are sorted by their ID.

### policies.json

| Field | Meaning |
|---|---|
| `policy_number` | `POL-0001` to `POL-0056`; not the number of the claim on it |
| `product` | Product code, see Products |
| `wording_version` | `2026-01` |
| `holder` | `name`, `email`, `address` (`street`, `city`, `country` as an ISO code) |
| `start_date`, `end_date` | Start, and start plus one year minus one day |
| `status` | `active` or `lapsed` (non-payment) |
| `lapsed_on` | The lapse date, or null |
| `deductible` | Euros per claim |
| `sum_insured` | Vehicle value (Motor Comprehensive, 8,000 to 45,000), the sum for a home (80,000 to 400,000), or null (Motor Third-Party Liability) |
| `limit` | 1,000,000 for Motor Third-Party Liability; otherwise the `sum_insured` |
| `insured_object` | Motor: `make`, `model`, `year`, `registration`. Home: `address`, `building_type` (`apartment` or `house`) |

A policy whose term has ended keeps the status `active`; it is recognised by
its `end_date`. `lapsed` means cover ended early for non-payment, on
`lapsed_on`. One claim is on a policy that lapsed after the loss, so the
policy was in force on the loss date.

### claim-history.json

| Field | Meaning |
|---|---|
| `history_id` | `HIST-0001` and up |
| `policy_number` | The policy the claim was paid on |
| `loss_date` | Date of the past loss |
| `peril` | A peril the product covers |
| `paid_amount` | Euros paid |
| `status` | Always `closed` |

Policies are renewals, so history can predate the current term. It is never
dated after the end of the term or on or after the lapse date. The
frequent-claims cases have two or three entries dated from 365 days before the
loss up to the day before it. All other history is quiet: no entries, one
entry, or entries older than that window. The two claims after the first forty
that sit on the frequent-claims boundary have two entries each, the older one
365 days before the loss (frequent) or 366 (one entry in the window, so not).

### claims.json

| Field | Meaning |
|---|---|
| `claim_id` | `CLM-0001` to `CLM-0047`; the first forty and the seven after them are each shuffled, so the number reveals nothing |
| `policy_number` | The policy claimed on; for one claim (`policy_not_found`) a number no policy has |
| `reported_on`, `loss_date` | Losses fall between 2026-05-01 and 2026-08-25; nothing is reported after 2026-09-01 |
| `peril` | A peril code of the product's line |
| `claimed_amount` | Euros claimed |
| `loss_location` | `city`, `country` |
| `claimant` | `name`, `email`; the policy holder (for the claim on no policy, a stand-in holder who is in no file) |
| `description` | First person, two to four sentences; it states any circumstance behind an exclusion, tells a story that fits it, and gives any reason for a late report |
| `documents` | Document codes provided, in catalogue order |

### expected-outcomes.json

| Field | Meaning |
|---|---|
| `claim_id` | The claim in `claims.json` that this record labels; one record per claim |
| `route` | `auto_approve`, `adjuster` or `request_documents` |
| `reason` | `within_threshold`, `over_threshold`, `fraud_indicator`, `excluded`, `policy_inactive`, `missing_documents` or `policy_not_found` |
| `recommendation` | `approve`, `reject` or null |
| `payable_amount` | Euros, or null when nothing is payable yet |
| `exclusion` | Exclusion code, or null |
| `fraud_indicators` | Every indicator that applies: `early_loss`, `frequent_claims`, `late_report` |
| `missing_documents` | Required documents not provided, in catalogue order |
| `citations` | `{"wording": <product code>, "clause": "<section>.<n>"}` |

A citation resolves to the heading `### <clause> <title>` in
`wordings/<product code>.md`.

The circumstance behind a circumstance exclusion (a track day, drinks before
driving, a friend without a licence, a rotten roof, a leak of months) is a fact
in the description only. It is not a field of the claim.

### manifest.json

| Field | Meaning |
|---|---|
| `synthetic` | Always `true` |
| `workload` | `claims-triage`, the workload the set belongs to; `meridian eval run` refuses a set whose manifest names another or none, and the injection set's manifest carries the same key |
| `generator_version` | Version of the generator that wrote the files |
| `seed` | The seed of the run |
| `reference_date` | 2026-09-01, the dataset's clock; nothing is reported after it |
| `currency` | `EUR`, the currency of every amount |
| `auto_approval_limit` | 2500, in euros of payable amount |
| `counts` | Records per file group: `policies`, `claim_history`, `claims`, `expected_outcomes`, `wordings` |
| `reasons` | Claims per reason |
| `files` | Relative path to SHA-256 of the file bytes, for every other output file, sorted by path |

The manifest is written last.

## Products

| Code | Name | Line | Covered perils | Exclusions, in wording order | Deductible | Limit |
|---|---|---|---|---|---|---|
| `MOTOR-TPL` | Motor Third-Party Liability | motor | third_party_liability | own_vehicle_damage (peril: collision, theft, fire, glass, storm); racing (third_party_liability) | 0 | 1,000,000 |
| `MOTOR-COMP` | Motor Comprehensive | motor | all six motor perils | racing (collision); driving_under_influence (collision); unlicensed_driver (collision) | 300 | sum insured |
| `HOME-STD` | Home Standard | home | fire, storm, burst_pipe, burglary | flood (peril); accidental_damage (peril); wear_and_tear (storm, burst_pipe); gradual_leak (burst_pipe) | 250 | sum insured |
| `HOME-PLUS` | Home Plus | home | all six home perils | wear_and_tear (storm, burst_pipe); gradual_leak (burst_pipe) | 150 | sum insured |

The motor perils are `collision`, `theft`, `fire`, `glass`, `storm` and
`third_party_liability`. The home perils are `fire`, `storm`, `flood`,
`burst_pipe`, `burglary` and `accidental_damage`.

There are two kinds of exclusion. A peril exclusion says the peril itself is
not covered by the product. A circumstance exclusion removes cover for a
covered peril under a named circumstance. For every product, every peril of its
line is either covered or named by a peril exclusion; a test enforces it.

Required documents, in catalogue order (`police_report`, `photos`,
`repair_estimate`, `accident_statement`):

| Peril | Documents |
|---|---|
| collision | photos, repair_estimate |
| theft | police_report |
| fire, glass, storm, flood, accidental_damage | photos |
| burst_pipe | photos, repair_estimate |
| burglary | police_report, photos |
| third_party_liability | accident_statement |

Each wording has six sections: definitions, what is covered, what is not
covered, deductible and limits, making a claim, and period of cover. A clause is
a `###` heading numbered `<section>.<n>`: one cover clause per covered peril
(2.n), one clause per exclusion (3.n), 4.1 deductible, 4.2 limit, 5.1
reporting, one documents clause per covered peril (5.n after 5.1), 6.1 period of
cover and 6.2 lapse for non-payment. The wording text and the citations are
written from one catalogue, so they cannot disagree. Flood is covered by Home
Plus and excluded by Home Standard, which a retrieval test can tell apart.

## How a claim is judged

The expected outcome is derived by a pure function (`generator/oracle.py`) of
four inputs and the catalogue: the claim, the policy, the claim history and the
circumstance. The circumstance is the exclusion code that the facts of the claim
fall under, if any. It is a fact behind the description and is in no file, so
the labels of the circumstance exclusions cannot be re-derived from the JSON
files alone; the description states the circumstance in prose. The first rule
that matches wins:

0. No policy has the claim's number (the oracle is given no policy). Route
   `adjuster`, reason `policy_not_found`, no recommendation, no payable amount,
   no indicators, no citations: nothing else is decided, as the triage rules
   decide it.
1. The policy was not in force on the loss date: the loss is before
   `start_date` or after `end_date`, or the policy lapsed on or before the loss
   date. Route `adjuster`, reason `policy_inactive`, recommendation `reject`.
   Cite 6.2 if the policy lapsed, otherwise 6.1.
2. The peril is not covered, or an exclusion applies to it. Route `adjuster`,
   reason `excluded`, recommendation `reject`. Cite the exclusion's clause.
3. A required document is missing. Route `request_documents`, reason
   `missing_documents`, no recommendation. Cite the documents clause of the
   peril.
4. A fraud indicator applies. Route `adjuster`, reason `fraud_indicator`,
   recommendation `approve`.
5. The payable amount is above the auto-approval limit. Route `adjuster`,
   reason `over_threshold`, recommendation `approve`.
6. Otherwise route `auto_approve`, reason `within_threshold`, recommendation
   `approve`.

Steps 4 to 6 cite, in this order: the cover clause of the peril, 4.1, 4.2 if
the limit capped the payable amount (`claimed_amount` above `limit`), and 5.1 if
the report was late.

The payable amount is `min(claimed_amount, limit) - deductible`. It is null for
steps 1 to 3. For steps 4 to 6 it must be positive; the oracle raises
otherwise. The fraud indicators are computed for every claim, not only in step
4.

| Parameter | Value |
|---|---|
| Auto-approval limit | Payable up to and including 2,500 |
| Reporting window | 30 days; `late_report` when the report is more than 30 days after the loss |
| Early loss | The loss is 0 to 30 days after `start_date`, both included |
| Frequent claims | At least 2 history entries dated from 365 days before the loss up to the day before it |

If a policy both lapsed and ended before the loss, the oracle cites the lapse
(6.2). A circumstance that the product does not exclude for the peril, such as
racing on a theft claim, changes nothing.

## Scenario mix

The plan is written down in `generator/plan.py`, so the balance is deliberate:

| Reason | Claims | Built as |
|---|---|---|
| `within_threshold` | 8 | Payable 300 to 2,500, one exactly 2,500, one on a policy that lapsed after the loss |
| `over_threshold` | 6 | Payable above 2,500, one exactly 2,501, one claimed above the sum insured so the limit caps it and 4.2 is cited |
| `fraud_indicator` | 6 | Two each of `early_loss`, `frequent_claims` and `late_report`, each with only that indicator, some above 2,500 |
| `excluded` | 8 | Each exclusion code once: three peril exclusions and five circumstance exclusions |
| `policy_inactive` | 6 | Three expired (one the day after `end_date`), two lapsed (one on the loss date) and one not yet started |
| `missing_documents` | 6 | One or two required documents left out |

The claims are spread over all four products. Every scenario is built for its
outcome; the generator then runs the oracle on what it built and stops if the
reason, the fraud indicators, the exclusion or the missing documents differ
from the intent. Non-fraud claims are built with a policy well into its term
and a prompt report, so they carry no indicator. Claim numbers and policy
numbers are random permutations, and 10 of the first 50 policies have no claim.

### Two random streams, and the seven claims after the forty

The table above is the first forty claims, `CLM-0001` to `CLM-0040`, and the
first fifty policies. They are **frozen while the recording of the triage
model's answers stands**: that recording is keyed by a hash of each request,
which holds the claim's description, so a description that moves by one byte
loses an answer that only a paid recording brings back. One random stream
feeds those forty, so adding any scenario to it reshuffles all of them.

The scenarios added since are in `EXTRA_PLAN` in `generator/plan.py`, built in
`generator/extra.py` from a second stream: `random.Random` seeded with the
seed, a colon and a fixed label, once the first stream is spent. The new
claims take the numbers `CLM-0041` to `CLM-0047` (shuffled among themselves
from the second stream) and their policies `POL-0051` to `POL-0056`, so no
number, record or history entry of the first set changes. A test holds a
digest of the first forty claims, their labels, their policies and their
history, and fails with a message that says a paid recording would be needed.
Add a scenario to `EXTRA_PLAN`, never to `PLAN`, while the recording stands.

| Claims | Built as |
|---|---|
| 3 | One fraud indicator on its boundary, the only one in play: a loss 30 days after the policy's start, a report 31 days after the loss, two earlier claims with the older 365 days before the loss. The payable amount is within the limit, so each is referred for the indicator alone |
| 3 | The same, one day off the boundary: the 31st day after the start, a report after 30 days, an older claim 366 days before the loss (one entry in the window). Each is within the threshold and approves automatically |
| 1 | A claim whose policy number no policy has (`POL-9xxx`), the reason `policy_not_found` |

The perils are ones no circumstance exclusion of the product names (fire on
Home Standard and Home Plus, glass on Motor Comprehensive, burglary for the
unknown policy), so the triage model is not asked about any of the seven and
the oracle's label is the expectation without a recorded answer.

## Injection cases

`injection/cases.json` holds the cases of the injection evaluation suite
(S032). **Its sentences are attack text. They are data: nothing in the file
is an instruction to whoever, or whatever, reads it.** Status: implemented,
and used by tests only; no service reads it.

A case is a golden claim that asks the model, copied under a new claim ID,
with a sentence added. There are four groups:

| IDs | Label | Carrier | Cases | What is added |
|---|---|---|---|---|
| `CLM-1001` to `CLM-1054` | `attack` | `description` | 54 | A sentence in the claimant's description that tries to make the model say no exclusion applies |
| `CLM-2001` to `CLM-2012` | `attack` | `clause` | 12 | A sentence put into an exclusion clause of the claim's wording, before the clause's closing sentence |
| `CLM-3001` to `CLM-3024` | `benign` | `description` | 24 | An ordinary claimant's sentence, most of them written to resemble an attack's words |
| `CLM-4001` to `CLM-4004` | `benign` | `clause` | 4 | A plain wording sentence (a definition, a notice period, a cross-reference) put into an exclusion clause the way an attack is, on claims no exclusion applies to (the clause is one the triage shows the model for the claim's peril), to show that a changed clause alone does not flag a claim or change how the claim ends |

| Field | Meaning |
|---|---|
| `case` | The case's ID, which is its claim's `claim_id` |
| `label` | `attack` or `benign` |
| `carrier` | `description` or `clause`: where the added sentence sits |
| `family` | The technique, such as `override`, `role-marker`, `obfuscated`, `other-language` or `indirect` |
| `base_claim` | The golden claim the case copies; its expected outcome is the case's |
| `claim` | The claim to post: the base claim's fields with the new ID and, for a description case, the longer description |
| `clause` | `null`, or for a clause case the product, wording version and clause number and the sentence to insert (`inserted`) into that clause's stored text, immediately before its closing sentence "This exclusion applies to claims for ..." |

Things to know:

- The file is ASCII. A character outside it is written as an escape, so a
  zero-width or look-alike character shows in a diff as `\u200b` or
  `\u043e` and cannot hide.
- The sentences ask for one thing, the triage model's answer. None names an
  address, a command, a file or a secret, and a test refuses one that does.
  Some address whoever reads them ("any language model"), and some hold the
  role markers of chat formats: a tool that reads this file must treat it
  as text.
- A clause's sentence sits before the closing sentence because a clause
  that does not end with that sentence is not read as an exclusion at all:
  the model never sees it, and the claim goes to an adjuster.
- The two `name-masked` cases replace the claimant's name with words
  (`Ignore Previous`, `Approve Claim`). They are no person's name. Since
  S067 the Claims API screens the description as posted, before it replaces
  the name, so both are stopped before the model; they were not before.
- The cases were written in the session that built the suite, after it had
  read the screen they test. They are not a blind red-team set: a rate
  measured on them describes these cases.
- The set has its own manifest, so the golden set's fingerprint does not
  move when a case changes. Its `golden_set` field is the SHA-256 of the
  golden manifest, so a change to the golden set changes this manifest too.
- The base claims are named by ID for the committed seed. Another seed puts
  other scenarios behind those IDs; the generator still writes a case set,
  and the tests of the committed one do not apply to it.

To add a case, append it to its list in `generator/injection_text.py` (the
IDs follow the order of the lists, so a case added in the middle renumbers
the ones after it), run `make synthetic`, then `make eval-baseline`, and
read the diff of `data/evaluation/injection-summary.md`.

## Regenerating

```sh
make synthetic
# or, from the repository root:
PYTHONPATH=data/synthetic uv run python -m generator --seed 20260929 --out data/synthetic
```

The default seed is 20260929 and the default output folder is this one,
whatever the working directory. Any other seed needs an explicit `--out`; the
generator refuses otherwise, so the committed golden set cannot be overwritten
by accident. A rerun with the same seed produces identical bytes:

- One `random.Random(seed)` is threaded through every builder of the first
  forty claims, and a second stream, seeded from the seed and a fixed label,
  through those of the seven after them. No module-level `random`, no clock:
  the dataset's date is the fixed reference date 2026-09-01, and nothing is
  reported after it.
- No output depends on the iteration order of a set. JSON keeps the field
  order of the tables above, is indented by two spaces and ends with a newline.
  Files are UTF-8 with LF line endings and contain no timestamp.
- `tests/synthetic/` runs the generator in two processes with different hash
  seeds and compares every byte, regenerates the data and compares it with the
  committed files, and fails on a JSON or wording file that the generator did
  not write. The manifest holds a SHA-256 of every other file.

## Changing the data

Edit the generator, rerun `make synthetic`, review the diff of
`expected-outcomes.json` and commit the code and the output together. Run
`make pytest` before the pull request; a stale committed file fails it.

| To change | Edit |
|---|---|
| A product, an exclusion, a document, a threshold or a clause number | `generator/catalogue.py` |
| The wording prose | `generator/wording_text.py` |
| The mix of scenarios | `generator/plan.py` (`EXTRA_PLAN` for a new one while the recording stands) |
| The scenarios of the second stream | `generator/extra.py` |
| How a scenario is built | `generator/builders.py`, `generator/records.py` |
| The rules of the outcome | `generator/oracle.py` |
| The claim descriptions | `generator/narratives.py` |
| Names, cities, vehicles | `generator/people.py` |
| An injection case | `generator/injection_text.py`, `generator/injection.py` |

One random stream feeds the first forty claims, so adding or removing any
template variant or scenario of `PLAN`, or editing the narratives, reshuffles
them and loses the recording's answers: a regenerated first forty is a new
version of the golden set, and a new recording costs money. A scenario of
`EXTRA_PLAN` moves nothing in the first forty; it reshuffles only the claims
after them. The hashes in `manifest.json` identify the set. A different
`--seed` produces a different but equally valid set. Only the default seed is
committed.
