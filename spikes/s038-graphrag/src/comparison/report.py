"""The measurements of the comparison as plain numbers: one function from the
graph, the data directory and the search's lists to the dict that is written to
``results/comparison.json``. Everything is a count, a clause number or a claim
ID; no description and no name.

The search's lists (keyword, vector, fused, ten clauses each) are an input and
are stored in the file, so every other number can be recomputed from the file
and the committed data without a database; a test with a database checks that a
fresh search gives the lists that are stored.
"""

import json
import statistics
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from datetime import date
from pathlib import Path
from typing import Any

from claimgraph import files
from claimgraph.model import Graph
from claimgraph.questions import clauses_bearing_on_claim, events_before_loss
from claimgraph.traverse import Cost
from retrievalsupport import EMBEDDING_BATCH

from meridian.workloads.claims_triage.rules import FREQUENT_CLAIMS_COUNT

from . import needs, rankings, rule, score

RANKED = ("graph", "keyword", "vector", "fused")
SEARCHES = ("keyword", "vector", "fused")
CUTOFFS = score.CUTOFFS
WIN_CUTOFFS = (5, 10)
GRAPH_MODULES = ("model", "keys", "wording", "build", "traverse", "questions")
EMBEDDING = (
    "simulated embedding (the platform's replay embedding, a hashed bag of words)"
)
RESULTS = Path(__file__).resolve().parents[2] / "results" / "comparison.json"
GRAPH_SOURCE = Path(__file__).resolve().parents[1] / "claimgraph"

Lists = Mapping[str, Mapping[str, Sequence[str]]]


def _by_ranking(lists: Lists) -> dict[str, dict[str, list[str]]]:
    """Ranking name to claim ID to its list."""
    return {r: {c: list(found[r]) for c, found in lists.items()} for r in RANKED}


def _recall(
    by_ranking: Mapping[str, Mapping[str, list[str]]],
    production: Mapping[str, tuple[str, ...]],
    labelled: score.ByClaim,
    scope: Mapping[str, int],
) -> dict[str, Any]:
    total = sum(len(cs) for cs in labelled.values())
    table: dict[str, Any] = {"claims": len(labelled), "labelled": total}
    for name in RANKED:
        found = [score.recall(by_ranking[name], labelled, k)[0] for k in CUTOFFS]
        table[name] = {"hits": found, "share": [round(h / total, 4) for h in found]}
    held = score.recall({c: list(s) for c, s in production.items()}, labelled, 99)[0]
    table["production"] = {"hits": held, "share": round(held / total, 4)}
    chance = [score.chance_hits(labelled, scope, k) for k in CUTOFFS]
    table["chance"] = {
        "hits": [round(c, 2) for c in chance],
        "share": [round(c / total, 4) for c in chance],
    }
    return table


def _sizes(
    by_ranking: Mapping[str, Mapping[str, list[str]]],
    production: Mapping[str, tuple[str, ...]],
) -> dict[str, Any]:
    sized: dict[str, Any] = {}
    for name in RANKED:
        lengths = [len(found) for found in by_ranking[name].values()]
        sized[name] = {
            "claims": len(lengths),
            "sizes": dict(sorted(Counter(lengths).items())),
            "shorter_than_k": {str(k): sum(n < k for n in lengths) for k in CUTOFFS},
        }
    held = [len(found) for found in production.values()]
    sized["production"] = {
        "claims": len(held),
        "sizes": dict(sorted(Counter(held).items())),
    }
    return sized


def _wins(
    by_ranking: Mapping[str, Mapping[str, list[str]]], labelled: score.ByClaim
) -> dict[str, Any]:
    return {
        f"graph_vs_{other}": {
            str(k): score.tally(by_ranking["graph"], by_ranking[other], labelled, k)
            for k in WIN_CUTOFFS
        }
        for other in ("fused", "keyword")
    }


def _point2(
    graph_lists: Mapping[str, list[str]],
    production: Mapping[str, tuple[str, ...]],
    labelled: score.ByClaim,
) -> dict[str, Any]:
    """Per claim, the labelled clauses the graph's whole list holds and the
    production set holds, and how the two differ."""
    per_claim: dict[str, Any] = {}
    for claim, clauses in labelled.items():
        in_graph = [c for c in clauses if c in graph_lists[claim]]
        in_production = [c for c in clauses if c in production[claim]]
        per_claim[claim] = {
            "labelled": list(clauses),
            "graph": in_graph,
            "production": in_production,
            "graph_only": [c for c in in_graph if c not in in_production],
            "production_only": [c for c in in_production if c not in in_graph],
            "graph_size": len(graph_lists[claim]),
            "production_size": len(production[claim]),
        }
    more = sum(bool(p["graph_only"]) for p in per_claim.values())
    fewer = sum(bool(p["production_only"]) for p in per_claim.values())
    return {
        "claims": len(per_claim),
        "identical": sum(
            not p["graph_only"] and not p["production_only"] for p in per_claim.values()
        ),
        "graph_finds_more": more,
        "graph_finds_fewer": fewer,
        "graph_holds": sum(len(p["graph"]) for p in per_claim.values()),
        "production_holds": sum(len(p["production"]) for p in per_claim.values()),
        "per_claim": per_claim,
    }


def _unfound(
    by_ranking: Mapping[str, Mapping[str, list[str]]],
    labels: score.Labels,
) -> list[dict[str, Any]]:
    """Labelled clauses that none of the four rankings has in its first ten."""
    missing = []
    for claim, clauses in labels.by_set["all"].items():
        for clause in clauses:
            if not any(clause in by_ranking[r].get(claim, ())[:10] for r in RANKED):
                missing.append(
                    {
                        "claim": claim,
                        "clause": clause,
                        "in_sets": [
                            s
                            for s in ("narrative", "exclusion")
                            if clause in labels.by_set[s].get(claim, ())
                        ],
                    }
                )
    return missing


def _only(labelled: score.ByClaim, keep: Callable[[str], bool]) -> score.ByClaim:
    """The labels of ``labelled`` that satisfy ``keep``; a claim left with none
    is dropped."""
    kept = {c: tuple(x for x in cs if keep(x)) for c, cs in labelled.items()}
    return {c: cs for c, cs in kept.items() if cs}


def _hits_at_10(
    by_ranking: Mapping[str, Mapping[str, list[str]]],
    labelled: score.ByClaim,
    scope: Mapping[str, int],
) -> dict[str, Any]:
    """Each ranking's hits at rank 10 on ``labelled``, and chance's."""
    found: dict[str, Any] = {
        "claims": len(labelled),
        "labelled": sum(len(cs) for cs in labelled.values()),
    }
    for name in RANKED:
        found[name] = score.recall(by_ranking[name], labelled, 10)[0]
    found["chance"] = round(score.chance_hits(labelled, scope, 10), 2)
    return found


def _by_section(
    by_ranking: Mapping[str, Mapping[str, list[str]]],
    everything: score.ByClaim,
    scope: Mapping[str, int],
) -> dict[str, Any]:
    """The 63 labels split by the section of the clause (2 and 3 against 4 to
    6), and clause 4.1 alone, at rank 10: what decides the failing check."""
    parts = {
        "sections_2_3": lambda x: x.split(".")[0] in ("2", "3"),
        "sections_4_6": lambda x: x.split(".")[0] in ("4", "5", "6"),
        "clause_4_1": lambda x: x == "4.1",
    }
    return {
        name: _hits_at_10(by_ranking, _only(everything, keep), scope)
        for name, keep in parts.items()
    }


def _places(
    by_ranking: Mapping[str, Mapping[str, list[str]]],
    production: Mapping[str, tuple[str, ...]],
    everything: score.ByClaim,
) -> dict[str, Any]:
    """Over the 40 claims, the places each ranking lists (the graph's whole
    list, the searches' first ten, the lookup's set) and the labelled clauses
    among them: precision, which the recall tables do not show."""
    listed = {"graph": 99, "keyword": 10, "vector": 10, "fused": 10}
    places: dict[str, Any] = {}
    for name, k in listed.items():
        found = by_ranking[name]
        places[name] = {
            "places": sum(len(found[c][:k]) for c in found),
            "labelled": score.recall(found, everything, k)[0],
        }
    held = {c: list(s) for c, s in production.items()}
    places["production"] = {
        "places": sum(len(s) for s in held.values()),
        "labelled": score.recall(held, everything, 99)[0],
    }
    return places


def _exclusion_places(
    graph_lists: Mapping[str, list[str]], everything: score.ByClaim
) -> dict[str, int]:
    """The exclusion clauses (section 3) the graph lists, against the ones a
    claim cites: it lists every exclusion that names the peril."""

    def exclusions(clauses: Sequence[str]) -> list[str]:
        return [c for c in clauses if c.startswith(f"{score.EXCLUSION_SECTION}.")]

    listing = [c for c, found in graph_lists.items() if exclusions(found)]
    return {
        "listed": sum(len(exclusions(found)) for found in graph_lists.values()),
        "cited": sum(
            len(set(exclusions(found)) & set(everything.get(c, ())))
            for c, found in graph_lists.items()
        ),
        "claims_listing_one": len(listing),
        "claims_listing_one_and_citing_none": sum(
            not exclusions(everything.get(c, ())) for c in listing
        ),
        "claims_citing_one": sum(bool(exclusions(cs)) for cs in everything.values()),
    }


def _frequent_claims(graph: Graph) -> dict[str, int]:
    """Claims with an earlier event in the 365 days before the loss, and with
    as many as the triage's ``frequent_claims`` needs (``FREQUENT_CLAIMS_COUNT``
    claims and history entries)."""
    events = []
    for claim in graph.nodes("Claim"):
        policy = graph.out_edges(claim.id, ("claimed_on",))[0].dst
        loss = date.fromisoformat(str(claim.attrs["loss_date"]))
        prior = events_before_loss(graph, policy, loss)
        events.append(len(prior.claims) + len(prior.history))
    return {
        "threshold": FREQUENT_CLAIMS_COUNT,
        "claims_with_an_earlier_event": sum(n >= 1 for n in events),
        "claims_with_threshold_events": sum(n >= FREQUENT_CLAIMS_COUNT for n in events),
        "claims": len(events),
    }


def _lines(module: str) -> int:
    return len(files.read_text(GRAPH_SOURCE / f"{module}.py").splitlines())


def _spread(values: list[int]) -> dict[str, float]:
    return {"median": statistics.median(values), "max": max(values)}


def _cost(
    graph: Graph,
    inputs: Sequence[rankings.ClaimInput],
    scope: Mapping[str, int],
    wordings: Mapping[tuple[str, str], int],
) -> dict[str, Any]:
    costs = []
    for i in inputs:
        cost = Cost()
        clauses_bearing_on_claim(graph, i.claim_id, cost=cost)
        costs.append(cost)
    in_scope = list(scope.values())
    return {
        "graph": {
            "queries": len(costs),
            "nodes_held": len(list(graph.nodes())),
            "edges_held": len(list(graph.edges())),
            "per_query_nodes_visited": _spread([c.nodes_visited for c in costs]),
            "per_query_edges_followed": _spread([c.edges_followed for c in costs]),
            "source_lines": {m: _lines(m) for m in GRAPH_MODULES},
            "source_lines_total": sum(_lines(m) for m in GRAPH_MODULES),
            "parser_notes": dict(graph.notes),
        },
        "search": {
            "queries": len(inputs),
            "rows_in_store": sum(wordings.values()),
            "rows_in_scope_per_query": {**_spread(in_scope), "min": min(in_scope)},
            "embeddings_per_query": 1,
            "gateway_calls_for_all_queries": -(-len(inputs) // EMBEDDING_BATCH),
        },
    }


def build_results(
    graph: Graph,
    data_dir: Path,
    search_lists: Mapping[str, Mapping[str, Sequence[str]]],
    labels: score.Labels | None = None,
) -> dict[str, Any]:
    """The whole comparison. ``search_lists`` maps a claim ID to its keyword,
    vector and fused lists (from a database run, or as stored in the file)."""
    labels = labels or score.load_labels(data_dir)
    inputs = rankings.claim_inputs(data_dir)
    graph_lists = rankings.graph_lists(graph, inputs)
    production = rankings.production_sets(data_dir, inputs)
    sizes = rankings.scope_sizes(data_dir)
    scope = {i.claim_id: sizes[(i.product, i.wording_version)] for i in inputs}
    lists = {c: {"graph": graph_lists[c], **dict(search_lists[c])} for c in graph_lists}
    by_ranking = _by_ranking(lists)
    result: dict[str, Any] = {
        "embedding": EMBEDDING,
        "cutoffs": list(CUTOFFS),
        "lists": {c: {r: list(lists[c][r]) for r in RANKED} for c in lists},
        "production": {c: list(s) for c, s in production.items()},
        "recall": {
            s: _recall(by_ranking, production, labels.by_set[s], scope)
            for s in score.LABEL_SETS
        },
        "list_sizes": _sizes(by_ranking, production),
        "wins": {s: _wins(by_ranking, labels.by_set[s]) for s in score.LABEL_SETS},
        "point2": {
            s: _point2(graph_lists, production, labels.by_set[s])
            for s in ("all", "narrative")
        },
        "unfound_at_10": _unfound(by_ranking, labels),
        "needs": needs.answers(graph),
        "cost": _cost(graph, inputs, scope, sizes),
    }
    result["rule"] = rule.apply(result)
    everything = labels.by_set["all"]
    result["by_section_at_10"] = _by_section(by_ranking, everything, scope)
    result["places_listed"] = _places(by_ranking, production, everything)
    result["exclusion_places"] = _exclusion_places(graph_lists, everything)
    result["frequent_claims_counts"] = _frequent_claims(graph)
    return result


def canonical(results: Mapping[str, Any]) -> dict[str, Any]:
    """The results as the file holds them (JSON keys are text)."""
    return json.loads(json.dumps(results))


def write(path: Path, results: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(results, indent=1) + "\n", encoding="utf-8")


def load(path: Path = RESULTS) -> dict[str, Any]:
    return json.loads(files.read_text(path))
