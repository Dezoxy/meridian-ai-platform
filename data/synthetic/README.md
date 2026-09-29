# Synthetic data and golden set

A seeded generator writes the data of a fictional insurer, Meridian Insurance,
which operates in eurozone Central Europe: policies, claim history, four
policy-wording documents and 40 first-notice-of-loss (FNOL) claims, each with a
labelled expected outcome. The set of expected outcomes is the golden set.
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
| `policies.json` | Policies, sorted by `policy_number` | 50 |
| `claim-history.json` | Prior, closed claims, sorted by `history_id` | see manifest |
| `claims.json` | FNOL inputs only, never labels, sorted by `claim_id` | 40 |
| `expected-outcomes.json` | A list with one label record per `claim_id`, sorted by `claim_id` | 40 |
| `wordings/<CODE>.md` | The policy wording of each product | 4 |
| `manifest.json` | Seed, counts and a SHA-256 of every other file | 1 |
| `generator/` | The generator; run it with `make synthetic` | code |

Money is whole euros as JSON integers. There are no floats anywhere, and the
currency is `EUR` (stated once, in the manifest). Dates are ISO 8601 strings.
Fields keep the order shown in the tables. Lists are sorted by their ID.

### policies.json

| Field | Meaning |
|---|---|
| `policy_number` | `POL-0001` to `POL-0050`; not the number of the claim on it |
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
entry, or entries older than that window.

### claims.json

| Field | Meaning |
|---|---|
| `claim_id` | `CLM-0001` to `CLM-0040`, shuffled so the number reveals nothing |
| `policy_number` | The policy claimed on |
| `reported_on`, `loss_date` | Losses fall between 2026-05-01 and 2026-08-25; nothing is reported after 2026-09-01 |
| `peril` | A peril code of the product's line |
| `claimed_amount` | Euros claimed |
| `loss_location` | `city`, `country` |
| `claimant` | `name`, `email`; always the policy holder |
| `description` | First person, two to four sentences; it states any circumstance behind an exclusion, tells a story that fits it, and gives any reason for a late report |
| `documents` | Document codes provided, in catalogue order |

### expected-outcomes.json

| Field | Meaning |
|---|---|
| `claim_id` | The claim in `claims.json` that this record labels; one record per claim |
| `route` | `auto_approve`, `adjuster` or `request_documents` |
| `reason` | `within_threshold`, `over_threshold`, `fraud_indicator`, `excluded`, `policy_inactive` or `missing_documents` |
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
numbers are random permutations, and 10 of the 50 policies have no claim.

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

- One `random.Random(seed)` is threaded through every builder. No module-level
  `random`, no clock: the dataset's date is the fixed reference date
  2026-09-01, and nothing is reported after it.
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
| The mix of scenarios | `generator/plan.py` |
| How a scenario is built | `generator/builders.py`, `generator/records.py` |
| The rules of the outcome | `generator/oracle.py` |
| The claim descriptions | `generator/narratives.py` |
| Names, cities, vehicles | `generator/people.py` |

One random stream feeds everything, so adding or removing any template variant
or scenario reshuffles the generated claims. Downstream steps must treat a
regenerated golden set as a new version; the hashes in `manifest.json` identify
it. A different `--seed` produces a different but equally valid set. Only the
default seed is committed.
