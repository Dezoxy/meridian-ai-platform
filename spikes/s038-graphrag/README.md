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

## The decision rule, set before anything was compared

Written on 2026-10-06, after the graph and its census and before the first
comparison ran, so that the numbers cannot choose their own reading. The
commit history shows the order.

The question is whether retrieval over a graph deserves a step of its own
on this platform. The answer is **yes** only if all three hold on the
committed data:

1. **It finds a claim's clauses better than the search does.** On the
   claims of the golden set, with the clauses each claim cites as the
   labels: the graph's ordered list holds more of the labelled clauses
   within the same cut-off than the fused hybrid search and than its
   keyword half alone, at rank 5 and at rank 10, for all labels and for
   the exclusion labels on their own; and query by query it wins at least
   as often as it loses.
2. **It finds what the production triage does not already find by
   structure.** The triage does not send free text to the search: it asks
   by the claim's peril and picks clauses by section and title
   (`select_terms`). If the graph returns the same labelled clauses as
   that lookup for the 40 claims, the gain of point 1 is the gain of
   using structure at all, which the platform already has, and the graph
   adds nothing.
3. **It answers a question the plan asks that one hop cannot.** At least
   one relational need written down in the plan, the backlog or the
   threat model has an answer on the committed data that needs more than
   a lookup by policy number.

If 1 holds and 2 or 3 does not, the answer is **not now**: the graph is a
tidier way to hold what the platform already does, worth returning to when
the data has customers with several policies or assets with several
claims. If 1 does not hold, the answer is **no**.

What no outcome here can say, and the result must not be read as saying:

- **Anything about real embeddings.** The only embedding available without
  a paid call is the platform's simulated one, a hashed bag of words. The
  vector half of the hybrid search therefore ranks by shared word forms.
  A real model might close the gap that the comparison shows, or widen
  it; that is unmeasured.
- **Anything about free-text questions.** The graph is asked with the
  claim's structured fields (its policy, its peril). The search is asked
  with the claim's description. They are two different inputs for the
  same task, "given this claim, find its clauses", and the comparison is
  of the task, not of the inputs.
- **Anything about a larger or richer data set.** Fifty policies, forty
  claims, eighty-five clauses.

## The comparison

Label for every number in this section: **committed data**, the platform's own
search run in its test harness. Every vector or fused number is a **simulated
embedding**: the platform's `replay` embedding is a hashed bag of words and
carries no meaning of the text. The spike is implemented as a spike and never
deployed; nothing here describes a deployed system.

### The rankings compared

Each is an ordered list of clause numbers for a claim, in the claim's own
product and wording version (`src/comparison/`).

- `graph`: `clauses_bearing_on_claim` on the committed data. Asked with the
  claim's policy and reported peril, never its description. The whole list is
  kept, at most five clauses, and a list shorter than the cut-off is not padded.
- `keyword`, `vector` (simulated embedding), `fused` (simulated embedding): the
  platform's `rank_halves` and `hybrid_search` on the four real wordings
  ingested through the real gateway app in replay mode, as the retrieval check
  of S012 does it. Asked with the claim's description. The first ten clauses of
  each are kept.
- `production`: the clauses `select_terms` names for the claim's peril over the
  whole wording: the cover, the documents, the peril's exclusion and candidate
  exclusions, and the five fixed terms (deductible, limit, reporting, period,
  lapse). A set of 6 to 10 clauses, not a ranking, so it has no recall at a
  cut-off: the tables give how many labelled clauses it holds.
- `chance`: the hits a random ranking of the claim's wording would give, as
  `retrievalsupport` computes it.

The labels are the clauses each claim cites (`src/comparison/score.py`, the only
module that opens the labels file), in three sets: `narrative` (the retrieval
check's own labels, sections 2 and 3: 28 clauses on 28 claims, 20 in section 2
and 8 in section 3), `exclusion` (its section-3 subset: 8) and `all` (every
citation of every claim: 63 on 40 claims). Recall at k is the labelled clauses
among the first k places, as hits and as a share of the labelled clauses. A win
is a claim for which the graph finds more labelled clauses within k than the
other ranking, a tie the same number, a loss fewer.

Lines are `wc -l spikes/s038-graphrag/src/comparison/*.py`:

| File | What it holds | Lines |
|---|---|---|
| `src/comparison/score.py` | the three label sets, recall, wins, ties, losses, chance; the only module that opens the labels | 110 |
| `src/comparison/rankings.py` | the graph's lists and the production sets; the scope sizes | 119 |
| `src/comparison/search.py` | the platform's search asked with each claim's description (database) | 87 |
| `src/comparison/needs.py` | the written relational needs and the question that answers each | 139 |
| `src/comparison/report.py` | every number of the results file, from the rankings and the labels | 248 |
| `src/comparison/rule.py` | the decision rule applied to those numbers | 97 |
| `src/comparison/tables.py` | the README's tables, from the results file | 257 |
| `src/comparison/__main__.py` | prints the tables | 16 |
| `src/comparison/__init__.py` | the package's docstring | 2 |

### How to run it, without make

From the repository root. The first command needs no database; the comparison's
search tests skip without one, as the repository's other database tests do.

```bash
uv run pytest spikes/s038-graphrag/tests -q
```

The comparison with a database (a throwaway PostgreSQL with pgvector, the image
the Makefile pins as `PYTEST_DB_IMAGE`; the name and port are yours to choose):

```bash
IMAGE=$(sed -n 's/^PYTEST_DB_IMAGE *:= *//p' Makefile)
docker run -d --name meridian-pytest-db-s038 --tmpfs /var/lib/postgresql/data \
  -p 127.0.0.1:55638:5432 -e POSTGRES_HOST_AUTH_METHOD=trust "$IMAGE"
until docker exec meridian-pytest-db-s038 pg_isready -U postgres -h 127.0.0.1; do
  sleep 1
done
export MERIDIAN_TEST_DATABASE_URL=postgresql://postgres@127.0.0.1:55638/postgres
export MERIDIAN_REQUIRE_DB=1
uv run pytest -n 4 spikes/s038-graphrag/tests -q
```

To regenerate `results/comparison.json` (the one test that writes it, as
`make eval` writes its report), with the same database up and the same two
variables exported; then run the whole suite again and print the tables:

```bash
S038_WRITE_RESULTS=1 uv run pytest spikes/s038-graphrag/tests/test_results_file.py \
  -k fresh_search -q
uv run pytest -n 4 spikes/s038-graphrag/tests -q
PYTHONPATH=spikes/s038-graphrag/src uv run python -m comparison
docker rm -f meridian-pytest-db-s038
```

The repository's gate runs the same files through its own target, with a name
and a port of its own for the container:

```bash
GITHUB_ACTIONS=true make pytest-db PYTEST_DB_CONTAINER=meridian-pytest-db-s038 \
  PYTEST_DB_PORT=55638 PYTEST_WORKERS=4 PYTEST_ARGS="spikes/s038-graphrag/tests -q"
```

The file holds the search's three lists of ten clauses for every claim, so every
other number in it is recomputed from the file and the committed data by a test
that needs no database (`tests/test_results_file.py`); the database variant
makes the whole file again and fails when it differs.

### How the database fixtures are reached

The fixtures (`fresh_database`, `gateway`) live in `tests/meridian/conftest.py`
and `tests/meridian/knowledge_mcp/conftest.py`, which pytest does not load for a
test under `spikes/`. `tests/conftest.py` of the spike lists the two as
plugins, `pytest_plugins = ["conftest", "knowledge_mcp.conftest"]`, and both
resolve through the root `pythonpath` (`tests/meridian`). Nothing is copied and
nothing outside `spikes/s038-graphrag/` changed. It works because the spike's
tests directory is the command-line argument, which makes its `conftest.py` an
initial one, where pytest accepts `pytest_plugins`. Under `-n 4` the registered
plugin's `pytest_configure_node` still hands the workers the same role
passwords (the run in the tables used four workers).

### The platform's own numbers first

Before anything was compared, the narrative numbers of the retrieval check were
reproduced through this harness: the hits of the keyword half, the vector half
and the fusion, for the narrative labels and for sections 2 and 3 on their own
(9 rows of `FLOORS`, 4 cut-offs each), equal the floors exactly, and the
per-claim lists rebuilt here give the same hits as `retrievalsupport.measure`
(`tests/test_search.py`). The floors were measured on 2026-10-02 on pgvector
0.8.6 and this run is on the 0.8.7 image; the numbers are the same. A
difference between the graph and the search below is therefore not a difference
of harness.

### Recall of each ranking

Command: `PYTHONPATH=spikes/s038-graphrag/src uv run python -m comparison`
(from `results/comparison.json`, pinned by `tests/test_results.py`). Each cell
is hits over labelled clauses, then the share.

Table: Recall, narrative labels (sections 2 and 3)

| Ranking | @1 | @3 | @5 | @10 |
|---|---|---|---|---|
| graph | 23/28 0.82 | 27/28 0.96 | 28/28 1.00 | 28/28 1.00 |
| keyword half | 14/28 0.50 | 19/28 0.68 | 21/28 0.75 | 22/28 0.79 |
| vector half (simulated embedding) | 3/28 0.11 | 9/28 0.32 | 13/28 0.46 | 19/28 0.68 |
| fused (simulated embedding) | 11/28 0.39 | 19/28 0.68 | 20/28 0.71 | 23/28 0.82 |
| chance | 1.3/28 0.05 | 4.0/28 0.14 | 6.7/28 0.24 | 13.3/28 0.48 |
| production (a set, no cut-off) | 28/28 1.00 |  |  |  |

Table: Recall, exclusion labels (section 3)

| Ranking | @1 | @3 | @5 | @10 |
|---|---|---|---|---|
| graph | 3/8 0.38 | 7/8 0.88 | 8/8 1.00 | 8/8 1.00 |
| keyword half | 3/8 0.38 | 4/8 0.50 | 5/8 0.62 | 5/8 0.62 |
| vector half (simulated embedding) | 0/8 0.00 | 0/8 0.00 | 1/8 0.12 | 1/8 0.12 |
| fused (simulated embedding) | 1/8 0.12 | 2/8 0.25 | 3/8 0.38 | 5/8 0.62 |
| chance | 0.4/8 0.05 | 1.2/8 0.15 | 2.0/8 0.25 | 4.0/8 0.50 |
| production (a set, no cut-off) | 8/8 1.00 |  |  |  |

Table: Recall, all labels (every citation)

| Ranking | @1 | @3 | @5 | @10 |
|---|---|---|---|---|
| graph | 23/63 0.37 | 31/63 0.49 | 34/63 0.54 | 34/63 0.54 |
| keyword half | 14/63 0.22 | 23/63 0.37 | 26/63 0.41 | 30/63 0.48 |
| vector half (simulated embedding) | 3/63 0.05 | 14/63 0.22 | 28/63 0.44 | 41/63 0.65 |
| fused (simulated embedding) | 11/63 0.17 | 20/63 0.32 | 28/63 0.44 | 36/63 0.57 |
| chance | 3.0/63 0.05 | 8.9/63 0.14 | 14.9/63 0.24 | 29.8/63 0.47 |
| production (a set, no cut-off) | 63/63 1.00 |  |  |  |

Table: List sizes

| List | Claims | Size: claims | Shorter than 1, 3, 5, 10 |
|---|---|---|---|
| graph | 40 | 1: 3, 2: 15, 3: 6, 4: 10, 5: 6 | 0, 18, 34, 40 |
| keyword | 40 | 2: 3, 3: 1, 4: 3, 5: 1, 6: 2, 7: 2, 8: 1, 9: 4, 10: 23 | 0, 3, 7, 17 |
| vector | 40 | 10: 40 | 0, 0, 0, 0 |
| fused | 40 | 10: 40 | 0, 0, 0, 0 |
| production | 40 | 6: 3, 7: 18, 8: 9, 9: 4, 10: 6 | a set |

The graph's list is shorter than 5 for 34 of 40 claims and never longer than 5,
so its hits at 10 are its hits at 5. The keyword half lists fewer than ten
clauses for 17 of 40 descriptions; the vector half and the fusion always list
ten. At rank 10 a ranking reads 10 of the 14 to 25 clauses of a wording (40 to
71 percent), which is why the chance row is 0.47 of the labelled clauses there.

### Query by query

Command: the same. A query is a claim with at least one label in the set.

Table: Wins, ties and losses of the graph, query by query

| Labels | Graph against | Rank | Wins | Ties | Losses |
|---|---|---|---|---|---|
| narrative | fused | 5 | 8 | 20 | 0 |
| narrative | fused | 10 | 5 | 23 | 0 |
| narrative | keyword | 5 | 7 | 21 | 0 |
| narrative | keyword | 10 | 6 | 22 | 0 |
| exclusion | fused | 5 | 5 | 3 | 0 |
| exclusion | fused | 10 | 3 | 5 | 0 |
| exclusion | keyword | 5 | 3 | 5 | 0 |
| exclusion | keyword | 10 | 3 | 5 | 0 |
| all | fused | 5 | 10 | 27 | 3 |
| all | fused | 10 | 6 | 27 | 7 |
| all | keyword | 5 | 9 | 30 | 1 |
| all | keyword | 10 | 7 | 31 | 2 |

### Against the production lookup (point 2)

Command: the same. For each of the 40 claims, the labelled clauses the graph's
whole list holds and the labelled clauses the `production` set holds.
`production` holds all 63 labelled clauses of the 40 claims. The graph holds 34.

Table: Point 2, graph against production

| Labels | Claims | Identical | Graph finds more | Graph finds fewer | Held by graph | Held by production |
|---|---|---|---|---|---|---|
| all | 40 | 14 | 0 | 26 | 34 | 63 |
| narrative | 28 | 28 | 0 | 0 | 28 | 28 |

Table: Point 2, every claim on which the two differ (all labels)

| Claim | Only the graph holds | Only production holds |
|---|---|---|
| CLM-0002 | none | 6.2 |
| CLM-0004 | none | 4.1 |
| CLM-0005 | none | 4.1 |
| CLM-0006 | none | 4.1 |
| CLM-0007 | none | 4.1 |
| CLM-0008 | none | 4.1 |
| CLM-0010 | none | 4.1 |
| CLM-0011 | none | 4.1 |
| CLM-0012 | none | 4.1, 5.1 |
| CLM-0013 | none | 4.1 |
| CLM-0014 | none | 6.1 |
| CLM-0015 | none | 4.1 |
| CLM-0016 | none | 4.1 |
| CLM-0017 | none | 6.2 |
| CLM-0018 | none | 6.1 |
| CLM-0019 | none | 4.1 |
| CLM-0021 | none | 4.1 |
| CLM-0023 | none | 4.1 |
| CLM-0024 | none | 4.1, 4.2 |
| CLM-0025 | none | 4.1 |
| CLM-0028 | none | 4.1 |
| CLM-0029 | none | 4.1 |
| CLM-0034 | none | 4.1, 5.1 |
| CLM-0036 | none | 6.1 |
| CLM-0039 | none | 6.1 |
| CLM-0040 | none | 4.1 |

Every difference has the same direction: a clause the production set holds and
the graph's list does not. They are 29 clauses: 4.1 on 20 claims, 4.2 on 1, 5.1
on 2, 6.1 on 4 and 6.2 on 2. On the narrative labels the two hold the same
labelled clauses for all 28 claims.

### What no ranking finds

Command: the same. The labelled clauses that none of `graph`, `keyword`,
`vector` and `fused` has among its first ten, by claim and clause.

Table: Labelled clauses no ranking finds at 10

| Claim | Clause | In narrative labels |
|---|---|---|
| CLM-0002 | 6.2 | no |
| CLM-0016 | 4.1 | no |
| CLM-0017 | 6.2 | no |
| CLM-0018 | 6.1 | no |
| CLM-0025 | 4.1 | no |
| CLM-0028 | 4.1 | no |
| CLM-0034 | 4.1 | no |
| CLM-0039 | 6.1 | no |

A reading, not a measurement, for each: the wordings and the policy fields say
which relation would have reached it.

- CLM-0002 and CLM-0017, clause 6.2 (Lapse for non-payment): the policy's own
  record says it lapsed, and the clause says cover ends on the lapse date. A
  relation from a policy's lapse date to the clause titled "Lapse for
  non-payment" would reach it. The graph has no edge from a policy's status or
  dates to a clause, and its recognisers read peril relations only.
- CLM-0018 and CLM-0039, clause 6.1 (Period of cover): the loss date falls
  outside the policy's start and end dates, and the clause says a loss outside
  them is outside the period of cover. A relation from the policy's period to
  the clause titled "Period of cover" would reach it; the graph has none.
- CLM-0016, CLM-0025, CLM-0028 and CLM-0034, clause 4.1 (Deductible): each claim
  is paid, and the clause says the deductible applies to "every claim". A
  relation from a payable claim, or simply from a product, to its clause titled
  "Deductible" would reach it, the way `covers` reaches a cover clause by its
  title; the graph does not read that title.

These are the clauses of sections 4 to 6 that the production lookup names as
fixed terms for every claim. In each case the missing relation is one more
title or date read from the wording, not a longer path in the graph.

### The written needs (point 3)

Command: the same. The plan (`docs/meridian-plan.md`), the backlog in it, the
threat model (`docs/architecture/security/threat-model.md`) and the triage's
rules (`src/meridian/workloads/claims_triage/`) were read for relational needs.
Four are written down; lines are those of the commit this branch was cut from
and move when the documents change, so a test checks the quoted phrase is in the
file, not the line (`src/comparison/needs.py` holds each phrase).

Table: Point 3, the written needs

| Need | Where written | Question | Hops | Non-trivial | Counted |
|---|---|---|---|---|---|
| frequent_claims | rules.py:163; meridian-plan.md:350; meridian-plan.md:403; threat-model.md:161 | events_before_loss | 1 | 10 of 40 | no |
| open_at_the_same_time | meridian-plan.md:403; meridian-plan.md:510; threat-model.md:161 | events_before_loss (the claims it returns) | 1 | 0 of 40 | no |
| holder_of_a_policy | threat-model.md:107; threat-model.md:161 | policies_of_customer | 1 | 0 of 50 | no |
| terms_of_a_claim | meridian-plan.md:340; threat-model.md:149 | clauses_bearing_on_claim | 3 | 37 of 40 | no |

- `frequent_claims`: the fraud indicator counts a policy's earlier claims and
  history entries in the 365 days before a loss. One hop from the policy.
  `events_before_loss` finds an earlier event for 10 of 40 claims.
- `open_at_the_same_time`: backlog step S067 would count a policy's claims that
  are open at the same time. One hop from the policy. No policy of the data has
  two claims (40 claims on 40 policies), so the answer is empty for all 40.
- `holder_of_a_policy`: T-22 and T-76 say the policy store holds no holder to
  check a policy number against, until identity (S021). One hop from the
  customer. All 50 customers hold one policy, so no answer holds a second one.
- `terms_of_a_claim`: the triage retrieves the terms that bear on a claim (T-64
  is the risk that the search misses one). Two to three hops. This is the
  retrieval task that points 1 and 2 measure, not a relation between customers,
  policies, assets and claims, so it is listed and not counted (a decision, see
  below): counted, it would make point 3 true of any retrieval.

No document writes down a need for a customer's other policies, an asset on two
policies, a claim across a customer's policies or customers who share an
address, although the committed data holds one address shared by two customers
(`customers_sharing_address`, 2 of 50). A need that nobody wrote down is not
counted.

### Costs, as counts

Command: the same. Per query is over the 40 claims, for the question that finds
a claim's clauses.

Table: Costs, as counts

| Cost | Graph | Search (keyword, vector, fused) |
|---|---|---|
| Held | 382 nodes, 521 edges | 85 rows |
| Per query, looked at | nodes visited: median 36, max 42; edges followed: median 31, max 37 | rows in scope: min 14, median 24, max 25 |
| Per query, calls | none | 1 embedding (3 gateway calls for 40 queries, 16 texts to a call) |
| Lines of source | 921 (model, keys, wording, build, traverse, questions) | the platform's own, not counted here |

What each needs in order to exist at all:

- The graph needs a parser of the wording's sentences. On the four wordings it
  fails 0 times in 17 cover titles, 0 in 17 documents titles and 0 in 11
  exclusion sentences (`parser_notes` in the results file). Two Burglary cover
  clauses name a second peril in their body that it does not read, and 8
  cross-references in section introductions are dropped by the platform's own
  clause parser. These counts are of four generated wordings that share one
  layout; a wording laid out otherwise is not measured.
- The search needs an embedding deployment and pgvector. Here the deployment is
  the gateway's `replay` one and pgvector is 0.8.7 on PostgreSQL 17 in a
  throwaway container; in the platform the same two are the gateway's embedding
  route and the database's extension (T-54, T-56).

### Decisions of the comparison

- The comparison lives in `src/comparison/`, a second package beside
  `claimgraph`, so that the leakage tests of contract 1, which read every module
  of `claimgraph`, still hold without exception.
- "All labels" in the rule is read as every clause each claim cites (`all`);
  the narrative reading is shown beside it, in a table of its own.
- Point 2 is read as "yes only if the graph holds a labelled clause the
  production set does not, for at least one claim"; the rule's words say the
  graph adds nothing if the two are the same.
- The graph's list is the whole list of `clauses_bearing_on_claim` (five clauses
  at most), the same as its list at 5 and at 10.
- The production set is taken over the whole wording and ignores whether the
  policy is in force, as the triage's own reference test does; the triage's
  probes for a policy that is not in force ask the timing probe only.
- Point 3 counts a need when it is a relation between the graph's entities, its
  question needs more than one hop, and its answer is non-trivial for at least
  one start. The first is a judgement (`needs.py`); the other two are numbers.
- The search is asked for every claim, not only the claims with labels, so that
  the three label sets are scored on the same lists.

## What the rule says

The rule is the section above, unchanged. Each point is answered from the
tables.

**Point 1: the graph finds a claim's clauses better than the search does.** No.
The checks are these, "all labels" read as every clause each claim cites (W/T/L
is wins, ties and losses; W>=L is whether it wins at least as often as it
loses):

Table: Point 1, all labels read as every citation

| Labels | Rank | Against | Graph | Other | More | W/T/L | W>=L |
|---|---|---|---|---|---|---|---|
| all | 5 | fused | 34 | 28 | yes | 10/27/3 | yes |
| all | 5 | keyword | 34 | 26 | yes | 9/30/1 | yes |
| all | 10 | fused | 34 | 36 | no | 6/27/7 | no |
| all | 10 | keyword | 34 | 30 | yes | 7/31/2 | yes |
| exclusion | 5 | fused | 8 | 3 | yes | 5/3/0 | yes |
| exclusion | 5 | keyword | 8 | 5 | yes | 3/5/0 | yes |
| exclusion | 10 | fused | 8 | 5 | yes | 3/5/0 | yes |
| exclusion | 10 | keyword | 8 | 5 | yes | 3/5/0 | yes |

Seven of the eight checks hold. The eighth fails: on all labels at rank 10
against the fused search the graph holds 34 labelled clauses and the fusion 36,
and the graph wins 6 queries, ties 27 and loses 7. On the exclusion labels the
graph holds 8 of 8 at both ranks against 3 and 5 (fused) and 5 and 5 (keyword),
with no loss. The graph's list lacks 29 of the 63 labelled
clauses: the deductible (4.1) for 20 claims, the limit (4.2), the reporting
clause (5.1) and the period and lapse clauses (6.1, 6.2), which no relation of
the graph links to the claim.

On the other reading of "all labels", the retrieval check's narrative labels,
point 1 holds: 28 of 28 at rank 5 and at rank 10 against 20 and 23 (fused) and
21 and 22 (keyword), and no query lost.

Table: Point 1, all labels read as the narrative labels

| Labels | Rank | Against | Graph | Other | More | W/T/L | W>=L |
|---|---|---|---|---|---|---|---|
| narrative | 5 | fused | 28 | 20 | yes | 8/20/0 | yes |
| narrative | 5 | keyword | 28 | 21 | yes | 7/21/0 | yes |
| narrative | 10 | fused | 28 | 23 | yes | 5/23/0 | yes |
| narrative | 10 | keyword | 28 | 22 | yes | 6/22/0 | yes |
| exclusion | 5 | fused | 8 | 3 | yes | 5/3/0 | yes |
| exclusion | 5 | keyword | 8 | 5 | yes | 3/5/0 | yes |
| exclusion | 10 | fused | 8 | 5 | yes | 3/5/0 | yes |
| exclusion | 10 | keyword | 8 | 5 | yes | 3/5/0 | yes |

**Point 2: the graph finds what the production triage does not already find by
structure.** No. The graph finds a labelled clause the production lookup does
not on 0 of 40 claims and finds fewer on 26; the two are the same on 14. On the
narrative labels the two hold the same labelled clauses for all 28 claims. A
reading: the graph's gain on the narrative labels is the gain of using
structure, which the triage already does.

**Point 3: the graph answers a question the plan asks that one hop cannot.** No.
Of the four needs written down, none is counted: the three relational ones are
one hop from a policy or a customer, with non-trivial answers for 10 of 40, 0
of 40 and 0 of 50 starts, and the fourth is the retrieval task of points 1
and 2.

**Outcome: no**, with "all labels" read as the rule's words say: the clauses
each claim cites are the 63 of `all`, and point 1 does not hold on them. The
rule would give **not now** on the narrative reading, where point 1 holds and
points 2 and 3 do not. The two readings differ, so which one "all labels" means
decides between `no` and `not now`, and that is for the main session and the
owner, not for this part. The rule gives `yes` on neither.

Where the rule's wording did not fit what could be measured:

- "All labels" has two readings (above), and they give different outcomes.
- "Within the same cut-off": the graph's list never has more than five clauses,
  while the search lists have ten. Rank 10 compares a list that stops at five
  with one that continues.
- Point 2 says the graph adds nothing if it returns "the same labelled clauses"
  as the lookup; on `all` it returns fewer, never more. Read as above.
- "A relational need": whether the triage's retrieval of terms is one was a
  judgement. Counted, point 3 would hold (37 of 40 answers non-trivial, up to 3
  hops); the outcome would not change, because point 2 fails on both readings
  (`not now` on the narrative reading, `no` on the other).
- The floors of the retrieval check are floors; the reproduction asked for
  "exactly" and got equality on all 9 rows, so nothing needed an exception.

## What this does not show

The rule's three limits stand: nothing here says anything about **real
embeddings** (the vector numbers are a hashed bag of words and a real model
might close the gap or widen it), about **free-text questions** (the graph was
asked with a claim's policy and peril and the search with its description: two
inputs for one task), or about **a larger or richer data set** (50 policies, 40
claims, 85 clauses). The measurement added these:

- The narrative and exclusion labels are the cover clause of the claim's peril
  and the exclusions that name it. The generator wrote the wordings and the
  labels from one catalogue, and the graph reads the same titles and the
  "applies to" sentence that the catalogue wrote. A reading, not a
  measurement: the graph's 28 of 28 and 8 of 8 are the recognisers reading
  sentences the labels were made from, which is also why `production` holds
  all of them.
- One label per narrative query and 28, 8 and 40 queries: a win or a loss is one
  clause, and one claim moves a table by one. The one failed check of point 1 is
  two clauses and one query wide.
- Rank 10 reads 10 of the 14 to 25 clauses of a wording. The vector half's 41 of
  63 at rank 10 against 29.8 for chance is partly the size of the wording.
- The comparison is of the platform's search in its test harness (the gateway
  app in process, replay mode, PostgreSQL in a container), not of a deployed
  search.
- Point 3 read four documents. A need discussed elsewhere, or not yet written,
  is not counted, and the data would show little of one: no customer holds two
  policies and no asset is on two (one address is shared by two customers).
- The production set has no cut-off and 6 to 10 clauses a claim of 14 to 25. It
  is a lookup of fixed terms for the peril, so it holds the clauses of
  sections 4 to 6 for every claim; that is how it holds all 63.

## Review

Filled in by the main session after the review.
