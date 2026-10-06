"""The two rankings that need no database, both asked with a claim's structured
fields and never its description:

- ``graph``: the ordered list of ``clauses_bearing_on_claim``;
- ``production``: the clauses the production triage names for the claim's
  peril, as a set. ``select_terms`` runs over the whole wording, the reference
  the triage's own retrieval test compares its probes with; the set is its
  cover, documents, peril exclusion, candidate exclusions and the fixed terms
  (deductible, limit, reporting, period, lapse).

This module does not import the scorer.
"""

from collections.abc import Sequence
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

from claimgraph import files
from claimgraph.build import load_records
from claimgraph.model import Graph
from claimgraph.questions import clauses_bearing_on_claim

from meridian.platform.knowledge_mcp.chunking import parse_wording
from meridian.workloads.claims_triage.wording import Clause, select_terms


@dataclass(frozen=True, slots=True)
class ClaimInput:
    """What a claim is asked with by the graph and by the production lookup."""

    claim_id: str
    policy_number: str
    peril: str
    product: str
    wording_version: str


def claim_inputs(data_dir: Path) -> list[ClaimInput]:
    records = load_records(data_dir)
    policies = {p["policy_number"]: p for p in records.policies}
    return [
        ClaimInput(
            c["claim_id"],
            c["policy_number"],
            c["peril"],
            policies[c["policy_number"]]["product"],
            policies[c["policy_number"]]["wording_version"],
        )
        for c in records.claims
    ]


def graph_lists(
    graph: Graph, inputs: Sequence[ClaimInput], *, top: int | None = None
) -> dict[str, list[str]]:
    return {
        i.claim_id: [
            str(b.clause.attrs["number"])
            for b in clauses_bearing_on_claim(graph, i.claim_id, top=top)
        ]
        for i in inputs
    }


def whole_wording(data_dir: Path, product: str) -> list[dict[str, Any]]:
    """Every clause of a product's wording, as ``wording_search`` returns them
    (the same rows as the platform tests' ``whole_wording``)."""
    text = files.read_text(data_dir / "wordings" / f"{product}.md")
    return [
        {
            "clause": chunk.clause,
            "section": chunk.section,
            "title": chunk.title,
            "body": chunk.body,
            "keyword_match": False,
        }
        for chunk in parse_wording(text).chunks
    ]


def _number(clause: str) -> tuple[int, ...]:
    return tuple(int(part) for part in clause.split("."))


def production_sets(
    data_dir: Path, inputs: Sequence[ClaimInput]
) -> dict[str, tuple[str, ...]]:
    """The clauses ``select_terms`` names for each claim's peril over the whole
    wording of its policy's product and version, in clause order."""
    wordings = {p: whole_wording(data_dir, p) for p in {i.product for i in inputs}}
    found: dict[str, tuple[str, ...]] = {}
    for i in inputs:
        terms = select_terms(
            i.peril,  # type: ignore[arg-type]
            wordings[i.product],
            product=i.product,
            wording_version=i.wording_version,
        )
        clauses: set[str] = set()
        for field in fields(terms):
            value = getattr(terms, field.name)
            if isinstance(value, Clause):
                clauses.add(value.clause)
            elif isinstance(value, tuple):
                clauses.update(c.clause for c in value)
        found[i.claim_id] = tuple(sorted(clauses, key=_number))
    return found


def scope_sizes(data_dir: Path) -> dict[tuple[str, str], int]:
    """The clauses in each product and wording version: the rows a search of
    that wording looks at."""
    sizes: dict[tuple[str, str], int] = {}
    for i in claim_inputs(data_dir):
        key = (i.product, i.wording_version)
        if key not in sizes:
            sizes[key] = len(whole_wording(data_dir, i.product))
    return sizes
