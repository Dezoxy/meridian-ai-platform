"""The README's decision rule, applied to the measurements as it is worded.

Point 1: the graph's ordered list holds more of the labelled clauses within the
same cut-off than the fused search and than its keyword half alone, at rank 5
and at rank 10, for all labels and for the exclusion labels on their own, and
query by query wins at least as often as it loses.

Point 2: the graph returns labelled clauses that the production lookup
(``select_terms`` over the whole wording) does not hold, for at least one claim.
If the two are the same for all 40 claims, the graph adds nothing.

Point 3: at least one written relational need has an answer that needs more than
a lookup by policy number (``needs.py``).

The rule says "all labels". The scorer's three sets are ``narrative`` (the
retrieval check's own labels, sections 2 and 3), ``exclusion`` and ``all`` (every
citation of every claim). The rule's own words, "the clauses each claim cites",
read as ``all``; ``narrative`` is also applied, so that a reader who reads "all
labels" as the retrieval check's labels sees what that changes. Nothing is
softened: the outcome is given for each reading and ``readings_agree`` says
whether they are the same.
"""

from typing import Any

CUTOFFS_CHECKED = (5, 10)
AGAINST = ("fused", "keyword")
READINGS = ("all", "narrative")


def _checks(result: dict[str, Any], label_set: str) -> list[dict[str, Any]]:
    cutoffs = result["cutoffs"]
    found = []
    for k in CUTOFFS_CHECKED:
        place = cutoffs.index(k)
        for other in AGAINST:
            recall = result["recall"][label_set]
            tally = result["wins"][label_set][f"graph_vs_{other}"][str(k)]
            graph, theirs = recall["graph"]["hits"][place], recall[other]["hits"][place]
            found.append(
                {
                    "label_set": label_set,
                    "rank": k,
                    "against": other,
                    "graph_hits": graph,
                    "other_hits": theirs,
                    "graph_finds_more": graph > theirs,
                    "wins": tally["win"],
                    "ties": tally["tie"],
                    "losses": tally["loss"],
                    "wins_at_least_losses": tally["win"] >= tally["loss"],
                }
            )
    return found


def _point1(result: dict[str, Any], reading: str) -> dict[str, Any]:
    checks = [*_checks(result, reading), *_checks(result, "exclusion")]
    return {
        "checks": checks,
        "holds": all(
            c["graph_finds_more"] and c["wins_at_least_losses"] for c in checks
        ),
    }


def _point2(result: dict[str, Any], reading: str) -> dict[str, Any]:
    table = result["point2"][reading]
    return {
        "graph_finds_more_on_claims": table["graph_finds_more"],
        "holds": table["graph_finds_more"] > 0,
    }


def _outcome(first: bool, second: bool, third: bool) -> str:
    if not first:
        return "no"
    return "yes" if second and third else "not now"


def apply(result: dict[str, Any]) -> dict[str, Any]:
    point3 = {
        "counted_needs": [n["need"] for n in result["needs"] if n["counted"]],
        "holds": any(n["counted"] for n in result["needs"]),
    }
    one = {r: _point1(result, r) for r in READINGS}
    two = {r: _point2(result, r) for r in READINGS}
    outcome = {
        r: _outcome(one[r]["holds"], two[r]["holds"], point3["holds"]) for r in READINGS
    }
    return {
        "point1": one,
        "point2": two,
        "point3": point3,
        "outcome": outcome,
        "readings_agree": len(set(outcome.values())) == 1,
    }
