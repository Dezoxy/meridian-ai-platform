# S038 spike: a claims graph, and what the committed data can answer

## What this is

The first part of plan step S038, a GraphRAG spike. It builds a small knowledge
graph of customer, policy, asset, claim, claim history and policy wording from
the synthetic data, answers six named questions by traversal, and counts what
the committed data holds. It is built to find out whether the data holds a
graph at all before anything is compared with the platform's hybrid search.

- It is **spike code, implemented as a spike and never deployed**. Nothing
  under `src/meridian/` imports it.
- It adds **no dependency**. The graph is plain Python (the standard library).
  It is not a uv project of its own and has no lock file: it runs with the
  repository's root environment. This differs from the S005 spike, which pins a
  framework and so has its own project.
- **No model call, no network, no cluster, no database.** There is no
  embedding in this part. When a later part shows a vector number it says
  "simulated embedding": the platform's `replay` embedding is a hashed bag of
  words and carries no meaning of the text.
- **Synthetic data only**, read in place from `data/synthetic/`:
  `policies.json`, `claims.json`, `claim-history.json` and the four wordings.
  Nothing is written.
- **No oracle leakage.** The labels file and the generator under
  `data/synthetic/generator/` wrote the wordings and the labels. The builder and
  the questions open neither, and import no scorer
  (`tests/test_isolation.py`: the source text, the paths opened at run time and
  the modules loaded in a fresh process).
- **Nothing personal-data-shaped** appears in a test's output, a node's `repr`,
  the census or this file: IDs, counts and clause numbers only
  (`tests/test_personal_data.py`).
- **No comparison and no conclusion here.** The second part sets a decision
  rule first and then compares the graph with the hybrid search.

Every number below is the output of `python -m claimgraph` and is pinned by
`tests/test_census_pins.py`, or it is pinned by the test this file names. A
sentence that is a reading, not a measurement, says so.

## How to run

From the repository root, with the root environment:

```bash
uv run pytest spikes/s038-graphrag/tests -q
PYTHONPATH=spikes/s038-graphrag/src uv run python -m claimgraph
```

The root pytest configuration does not collect `spikes/`, so the path is passed
explicitly. `tests/conftest.py` puts `spikes/s038-graphrag/src` on the import
path. The second command prints the two censuses as JSON, one object each,
labelled "committed data" and "simulated variant (seed 38)".

Lines are `wc -l spikes/s038-graphrag/src/claimgraph/*.py`:

| File | What it holds | Lines |
|---|---|---|
| `src/claimgraph/model.py` | node kinds, the edge kinds in one table, `Node`, `Edge`, `Graph` | 139 |
| `src/claimgraph/keys.py` | what identifies a customer, an address and an asset | 81 |
| `src/claimgraph/wording.py` | the recognisers that read a wording's text | 114 |
| `src/claimgraph/build.py` | the builder: a directory in, a graph out | 323 |
| `src/claimgraph/traverse.py` | `follow`: edge kinds, hops, paths, cost counts | 70 |
| `src/claimgraph/questions.py` | the six named questions | 194 |
| `src/claimgraph/census.py` | the census as plain numbers | 161 |
| `src/claimgraph/variant.py` | the simulated variant | 86 |
| `src/claimgraph/files.py`, `__main__.py`, `__init__.py` | the one place a file is opened; the printer | 8, 24, 3 |

## The schema

Every node and edge carries the file and field it came from (`source_file`,
`source_field`). A node prints its kind and ID and nothing else. The edge kinds
are named in one place, `EDGE_KINDS` in `model.py`, and `Graph.add_edge` refuses
an edge whose ends are not of the kinds its kind names.

| Node kind | ID (key) | Source |
|---|---|---|
| `Customer` | `CUS-` and 12 hex digits of a digest of the holder's e-mail | `policies.json` `holder.email` |
| `Address` | `ADR-` and 12 hex digits of a digest of the holder's address | `policies.json` `holder.address` |
| `Policy` | `POL-nnnn`, the policy number | `policies.json` |
| `Asset` | `AST-` and 12 hex digits of a digest of the registration (a vehicle) or the insured address (a home) | `policies.json` `insured_object` |
| `Claim` | `CLM-nnnn` | `claims.json` |
| `HistoryEntry` | `HIST-nnnn` | `claim-history.json` |
| `Product` | the product code, with its wording version | the wording's header line |
| `Clause` | `<product>/<n.m>`, for example `HOME-STD/2.2` | the wording's clause heading |
| `Peril` | the peril's name, for example `burst_pipe` | `claims.json`, `claim-history.json` and the exclusion sentences |

| Edge kind | From | To | Source |
|---|---|---|---|
| `holds` | `Customer` | `Policy` | `policies.json` `holder.email` |
| `lives_at` | `Customer` | `Address` | `policies.json` `holder.address` |
| `insures` | `Policy` | `Asset` | `policies.json` `insured_object` |
| `claimed_on` | `Claim` | `Policy` | `claims.json` `policy_number` |
| `had` | `Policy` | `HistoryEntry` | `claim-history.json` `policy_number` |
| `of_product` | `Policy` | `Product` | `policies.json` `product` |
| `reported_peril` | `Claim`, `HistoryEntry` | `Peril` | `claims.json`, `claim-history.json` `peril` |
| `part_of` | `Clause` | `Product` | the clause heading (the section is an attribute) |
| `applies_to` | `Clause` | `Peril` | an exclusion's own sentence "applies to claims for ..." |
| `covers` | `Clause` | `Peril` | the title of a clause in "What is covered" |
| `documents_for` | `Clause` | `Peril` | the title of a clause "Documents for ..." |
| `refers_to` | `Clause` | `Clause` | "clause n.m" in a clause body |

### What identifies a customer, an address and an asset

The data has no customer ID, no address ID and no asset ID, so each key is
derived (`keys.py`), and each is **stored and shown only as a digest**: a
prefix and the first 12 hexadecimal digits of a SHA-256 over a namespace and the
normalised value. A digest is a pure function of the field: two policies that
share the field share the ID, and it does not depend on the order the files are
read in. It is not a secret, because a short value can be guessed and hashed
again; it keeps the field's text out of every output of the spike. The ID of a
customer is not an ordinal on purpose: an ordinal would change with the order
of the policies.

- **Customer**: the holder's e-mail, lower-cased and trimmed. No two of the 50
  policies share it: 50 distinct values, 0 policies on a shared one. The other
  candidates give the same 50 (the holder's name) and the same 50 (name and
  address together). Pinned by
  `test_how_often_policies_share_each_candidate_key_in_the_raw_files`.
- **Address** (of a holder): street, city and country, case and spacing
  normalised. 49 distinct values; one is shared by 2 policies.
- **Asset**: a vehicle is its registration (26 distinct values on 26 motor
  policies, none shared); a home is its insured address (24 distinct values on
  24 home policies, none shared).
- **A claimant is not a node.** The claimant's e-mail equals the holder's on 40
  of 40 claims, so a claimant edge would repeat `claimed_on` through `holds`.

## Reading the wording

The four wordings are cut into clauses by the platform's own parser
(`parse_wording`, imported read-only), so a clause here is a clause the hybrid
search indexes: 85 clauses in four products. Four recognisers read the text,
and each counts what it could not read (`Graph.notes`, printed under `notes`).

- **A cross-reference** is "clause n.m" in any case in a clause body, once per
  clause, kept when the target exists in the same product. 17 edges, 0 with no
  target. Text between a section heading and its first clause belongs to no
  clause, so the parser drops it: 8 more references live there and are not
  edges.
- **An exclusion's perils** are those named by its own sentence, "This
  exclusion applies to claims for a, b and c", split on commas and "and". 11
  exclusion clauses, 0 without the sentence, 17 `applies_to` edges.
- **A cover clause's peril** is recognised by its title. A clause in the section
  titled "What is covered" whose title, lower-cased with spaces and hyphens as
  underscores, is a known peril gets a `covers` edge ("Third-party liability"
  gives `third_party_liability`). A peril is known when a claim, a history entry
  or an exclusion sentence names it. **It fails 0 times in 17.**
- **A documents clause's peril** is recognised the same way from "Documents
  for ...": 17 clauses, 0 failures, 17 `documents_for` edges.

The title is the only place a peril is read from. The body is not: 2 of the 17
cover clauses (the two Burglary clauses, in Home Standard and Home Plus) name a
second peril in their body, "theft", which is a different peril of the motor
products. A body reading would have linked them to it. The rule of this spike
does not.

## The questions

Each question is a function in `questions.py` with a docstring that states it in
a sentence, built on one traversal, `follow(graph, start, out=, in_=,
max_hops=, cost=)`: breadth first, each node reached once by a shortest path,
returned with the edges walked. A cost is a count: nodes visited and edges
followed.

| Question | Start | Hops | Trivial answer |
|---|---|---|---|
| `clauses_bearing_on_claim` | a claim | 2 to 3 | one clause or none |
| `events_before_loss` | a claim's policy and loss date | 1 | no earlier event |
| `policies_of_customer` | a customer | 1 | one policy |
| `claims_on_asset` | an asset | 2 | events through one policy only |
| `customers_sharing_address` | a customer | 2 | nobody else |
| `claims_of_customer` | a customer | 2 | claims on one policy only |

`clauses_bearing_on_claim` returns an **ordered** list, with a reason for each
place and a cut-off (`top`). A clause bears on a claim when it belongs to the
product of the claim's policy and either covers the claim's peril, is an
exclusion that names it, or lists its documents (2 hops from the claim), or is
referred to by such a clause (3 hops). Order: hop count, then relation (covers,
applies_to, documents_for, refers_to), then clause number. The reason of a place
is its hop count and the edge kinds walked, for example "2 hops via
reported_peril, covers; ties by relation order, then clause number".

`events_before_loss` takes the 365 days before a loss date as the platform's
`frequent_claims` rule does: the first day included, the loss date itself not.

## The census of the committed data

Label: **committed data**. Nodes and edges:

| Node kind | Nodes | | Edge kind | Edges |
|---|---|---|---|---|
| `Customer` | 50 | | `holds` | 50 |
| `Address` | 49 | | `lives_at` | 50 |
| `Policy` | 50 | | `insures` | 50 |
| `Asset` | 50 | | `claimed_on` | 40 |
| `Claim` | 40 | | `had` | 44 |
| `HistoryEntry` | 44 | | `of_product` | 50 |
| `Product` | 4 | | `reported_peril` | 84 |
| `Clause` | 85 | | `part_of` | 85 |
| `Peril` | 10 | | `applies_to`, `covers`, `documents_for`, `refers_to` | 17 each |

Degree distributions for the edges that make a graph, as "degree: nodes". The
full set, for every node kind and edge kind, is in the output and the pins.

| Node kind and edge | Committed data | Simulated variant |
|---|---|---|
| `Customer` `holds` out | 1: 50 | 1: 32, 2: 6, 3: 2 |
| `Policy` `holds` in | 1: 50 | 1: 50 |
| `Asset` `insures` in | 1: 50 | 1: 36, 2: 7 |
| `Address` `lives_at` in | 1: 48, 2: 1 | 1: 38, 2: 1 |
| `Policy` `claimed_on` in | 0: 10, 1: 40 | 0: 10, 1: 40 |
| `Policy` `had` out | 0: 24, 1: 10, 2: 14, 3: 2 | 0: 24, 1: 10, 2: 14, 3: 2 |

For each question, over every start node: how many answers hold more than the
trivial one, the sizes of the answers, and the cost summed over all starts.

| Question | Starts | More than trivial | Answer sizes (size: starts) | Nodes visited | Edges followed |
|---|---|---|---|---|---|
| `clauses_bearing_on_claim` | 40 | 37 | 1: 3, 2: 15, 3: 6, 4: 10, 5: 6 | 1457 | 1225 |
| `events_before_loss` | 40 | 10 | 0: 30, 1: 8, 3: 2 | 112 | 72 |
| `policies_of_customer` | 50 | 0 | 1: 50 | 100 | 50 |
| `claims_on_asset` | 50 | 0 | 0: 3, 1: 23, 2: 13, 3: 9, 4: 2 | 184 | 134 |
| `customers_sharing_address` | 50 | 2 | 0: 48, 1: 2 | 102 | 102 |
| `claims_of_customer` | 50 | 0 | 0: 10, 1: 40 | 140 | 90 |

## What the committed data cannot show

Counts that say what a "customer, policy, asset, claim" graph cannot be asked
of this data.

- **No customer holds two policies.** 50 customers hold 50 policies, 1 each
  (`holds` out-degree 1: 50). `policies_of_customer` returns more than one
  policy for 0 of 50.
- **No asset is on two policies.** Each of 50 assets is insured by exactly one
  policy (`insures` in-degree 1: 50). `claims_on_asset` returns events through
  more than one policy for 0 of 50, so it adds nothing to a policy's own record.
  Of the 50 assets, 3 have no claim and no history at all.
- **No claim reaches a second policy through a customer.** The 40 claims are on
  40 distinct policies (`claimed_on` in-degree: 40 policies with 1, 10 with 0).
  `claims_of_customer` returns claims on more than one policy for 0 of 50.
- **The one shared address is not a household.** Two customers, whose policies
  are both motor policies, share a holder address (`customers_sharing_address`:
  2 of 50). No home asset is on it. On all 24 home policies the holder's address
  is the insured address, and the spike does not link an `Address` to an
  `Asset`, so no question crosses from a holder to a home.
- **History hangs on one policy.** The 44 history entries are on 26 policies,
  so `events_before_loss` is a one-hop question: 10 of 40 claims have an earlier
  event in the 365 days, and 2 have three. It is the question of T-76's
  `frequent_claims`. A reading: the policy record answers it as well as a graph
  does, because the answer is one hop from the policy.
- **What the data does hold is the wording.** 85 clauses, 17 `covers`, 17
  `documents_for`, 17 `applies_to` and 17 `refers_to` edges.
  `clauses_bearing_on_claim` returns more than one clause for 37 of 40 claims
  (sizes 1 to 5). Whether a graph orders them better than the hybrid search is
  the second part's question and is not answered here.

A reading, not a measurement: the relational half of the graph is a set of
chains with no branch, because the generator made one holder, one asset and at
most one claim per policy. A query through a derived key finds only what the key
joins, and the keys here join nothing across policies.

## The simulated variant

Label: **simulated variant (seed 38)**. Everything in this section is invented
to show what the relational questions return when there is something to return;
no number in it describes the data.

The variant is built in memory by `build_variant`, a pure function of the
committed files and a seed, and is written to nothing. Policies are ranked by
a SHA-256 of the seed and the policy number. The first 18 form 8 groups (of 3,
3, 2, 2, 2, 2, 2 and 2); a group's policies take the customer and the address
of its first. Of the policies that have a claim or a history entry, 4 pairs of
vehicle policies and 3 pairs of home policies each insure one asset. Everything
else, the policies, claims, history, wordings and every edge but `holds`,
`lives_at` and `insures`, is the committed graph's (`tests/test_variant.py`).

| Node kind | Simulated variant | | Question | Starts | More than trivial |
|---|---|---|---|---|---|
| `Customer` | 40 | | `policies_of_customer` | 40 | 8 |
| `Address` | 39 | | `claims_on_asset` | 43 | 7 |
| `Asset` | 43 | | `claims_of_customer` | 40 | 7 |
| `Policy` | 50 | | `customers_sharing_address` | 40 | 2 |

Cost of the questions that changed, simulated variant, over all starts:

| Question | Answer sizes (size: starts) | Nodes visited | Edges followed |
|---|---|---|---|
| `policies_of_customer` | 1: 32, 2: 6, 3: 2 | 90 | 50 |
| `claims_on_asset` | 0: 3, 1: 13, 2: 13, 3: 11, 4: 3 | 177 | 134 |
| `customers_sharing_address` | 0: 38, 1: 2 | 82 | 82 |
| `claims_of_customer` | 0: 9, 1: 24, 2: 5, 3: 2 | 130 | 90 |

The variant leaves the addresses as they were: it makes no new shared address,
so `customers_sharing_address` returns the same 2 starts. The other questions
cost the same as on the committed graph.

## Decisions this part made

- A node kind `Address` and an edge kind `lives_at` were added to the kinds the
  plan names, because "the customers that share an address" needs the holder's
  address and the data holds it nowhere else than on the policy.
- A `documents_for` edge was added beside `covers`: the title of "Documents
  for ..." names its peril as plainly as a cover clause's title.
- A clause ID is `<product>/<n.m>`; its attributes hold the product and the
  number, so a later part can map it to the `(product, clause)` pair the
  hybrid search returns.
- The cut-off of `clauses_bearing_on_claim` is the number of places kept, `top`.
- The census defines "more than the trivial answer" once per question, in the
  docstring of `census.py` and in the table above.
