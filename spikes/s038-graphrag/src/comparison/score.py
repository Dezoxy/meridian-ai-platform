"""The scorer: the only module that reads ``expected-outcomes.json``.

Labels are the clauses a claim cites, in three sets, by claim ID (a claim with
no label in a set is not in it):

- ``narrative``: the claim's citations in sections 2 and 3, exactly as the
  platform's retrieval check labels them (``retrievalsupport.narrative_queries``
  is reused, not re-derived): 28 clauses, 20 in section 2 and 8 in section 3.
- ``exclusion``: the section-3 subset of ``narrative`` (8 clauses).
- ``all``: every citation of every claim (63 clauses).

Recall at k is the labelled clauses found among the first k places of a ranked
list. A list shorter than k is never padded: it simply finds what it holds.
"""

import json
from collections import defaultdict, deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from claimgraph.files import read_text
from retrievalsupport import CUTOFFS, narrative_queries
from servicesupport import REPO_ROOT

DATA_DIR = REPO_ROOT / "data" / "synthetic"
LABELS_FILE = "expected-outcomes.json"
LABEL_SETS = ("narrative", "exclusion", "all")
EXCLUSION_SECTION = "3"

# Claim ID to the clauses it cites, in one label set.
ByClaim = Mapping[str, tuple[str, ...]]
# Claim ID to a ranked list of clause numbers.
Rankings = Mapping[str, Sequence[str]]

__all__ = ["CUTOFFS", "LABEL_SETS", "Labels", "load_labels"]


@dataclass(frozen=True, slots=True)
class Labels:
    by_set: Mapping[str, ByClaim]


def _narrative(data_dir: Path) -> dict[str, tuple[str, ...]]:
    """The retrieval check's labelled queries, each given its claim's ID by the
    description it was asked with (descriptions are unique)."""
    claims = json.loads(read_text(data_dir / "claims.json"))
    waiting: dict[str, deque[str]] = defaultdict(deque)
    for claim in claims:
        waiting[claim["description"]].append(claim["claim_id"])
    return {
        waiting[query.text].popleft(): query.relevant
        for query in narrative_queries(data_dir)
    }


def load_labels(data_dir: Path = DATA_DIR) -> Labels:
    outcomes = json.loads(read_text(data_dir / LABELS_FILE))
    everything = {
        o["claim_id"]: tuple(c["clause"] for c in o["citations"])
        for o in outcomes
        if o["citations"]
    }
    narrative = _narrative(data_dir)
    exclusion = {
        claim: tuple(c for c in clauses if c.startswith(f"{EXCLUSION_SECTION}."))
        for claim, clauses in narrative.items()
        if any(c.startswith(f"{EXCLUSION_SECTION}.") for c in clauses)
    }
    return Labels({"narrative": narrative, "exclusion": exclusion, "all": everything})


def hits_at(ranking: Sequence[str], labelled: Sequence[str], k: int) -> int:
    """The labelled clauses among the first ``k`` places of ``ranking``."""
    return len(set(ranking[:k]) & set(labelled))


def recall(rankings: Rankings, labelled: ByClaim, k: int) -> tuple[int, int]:
    """Hits and labelled clauses over every claim of ``labelled``; a claim with
    no list finds nothing and still counts."""
    hits = sum(hits_at(rankings.get(c, ()), cs, k) for c, cs in labelled.items())
    return hits, sum(len(cs) for cs in labelled.values())


def share(hits: int, labelled: int) -> float:
    return hits / labelled


def verdict(first: int, second: int) -> str:
    """``win`` when the first finds more labelled clauses, ``tie`` when the same,
    ``loss`` when fewer."""
    if first > second:
        return "win"
    return "tie" if first == second else "loss"


def tally(first: Rankings, second: Rankings, labelled: ByClaim, k: int) -> dict:
    """Per query (claim) wins, ties and losses of ``first`` against ``second``."""
    counts = {"win": 0, "tie": 0, "loss": 0}
    for claim, clauses in labelled.items():
        a = hits_at(first.get(claim, ()), clauses, k)
        b = hits_at(second.get(claim, ()), clauses, k)
        counts[verdict(a, b)] += 1
    return counts


def chance_hits(labelled: ByClaim, scope: Mapping[str, int], k: int) -> float:
    """The hits a random ranking of each claim's scope (its wording's clauses)
    would give at ``k``: for each labelled clause, ``min(k, N) / N``."""
    return sum(len(cs) * min(k, scope[c]) / scope[c] for c, cs in labelled.items())
