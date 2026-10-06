"""Build the graph from the data files and the wording text, and from nothing
else: policies, claim history, claims and the four wordings, read in place.
The labels file and the generator that wrote the data are neither read nor
imported. No global state: a directory in, a graph out.
"""

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import files
from .keys import Identity, asset_family, asset_field, derived_identity
from .model import Edge, Graph, Node
from .wording import ClauseFacts, WordingFacts, read_wording, slug

POLICIES = "policies.json"
CLAIMS = "claims.json"
HISTORY = "claim-history.json"
WORDINGS = "wordings"


@dataclass(frozen=True, slots=True)
class Records:
    """The files, parsed. ``wordings`` pairs each wording's relative path with
    what its text says."""

    policies: tuple[dict[str, Any], ...]
    claims: tuple[dict[str, Any], ...]
    history: tuple[dict[str, Any], ...]
    wordings: tuple[tuple[str, WordingFacts], ...]


def load_records(data_dir: Path) -> Records:
    def load(name: str) -> tuple[dict[str, Any], ...]:
        return tuple(json.loads(files.read_text(data_dir / name)))

    paths = sorted((data_dir / WORDINGS).glob("*.md"))
    wordings = tuple(
        (f"{WORDINGS}/{path.name}", read_wording(files.read_text(path)))
        for path in paths
    )
    return Records(load(POLICIES), load(CLAIMS), load(HISTORY), wordings)


def build_graph(
    data_dir: Path, identities: Mapping[str, Identity] | None = None
) -> Graph:
    """The graph of the files in ``data_dir``. ``identities`` maps a policy
    number to the customer, address and asset IDs it links to; without it
    they are derived from the policy's own fields (``keys.py``)."""
    return build_from(load_records(data_dir), identities)


def _perils(records: Records) -> dict[str, tuple[str, str]]:
    """Every peril the data files and the exclusion sentences name, with the
    first place it was seen."""
    seen: dict[str, tuple[str, str]] = {}
    for name, rows in ((CLAIMS, records.claims), (HISTORY, records.history)):
        for row in rows:
            seen.setdefault(row["peril"], (name, "peril"))
    for path, wording in records.wordings:
        for clause in wording.clauses:
            for peril in clause.excluded_perils:
                seen.setdefault(peril, (path, "exclusion sentence"))
    return dict(sorted(seen.items()))


def build_from(
    records: Records,
    identities: Mapping[str, Identity] | None = None,
    label: str = "committed data",
) -> Graph:
    graph = Graph(label)
    perils = _perils(records)
    for peril, (file, column) in perils.items():
        graph.add_node(Node("Peril", peril, file, column))
    _add_wordings(graph, records, set(perils))
    _add_policies(graph, records, identities)
    _add_events(graph, records)
    return graph


def _clause_id(product: str, number: str) -> str:
    return f"{product}/{number}"


def _add_wordings(graph: Graph, records: Records, perils: set[str]) -> None:
    notes = {
        "cover_clauses": 0,
        "cover_clauses_without_a_known_peril": 0,
        "cover_clauses_naming_another_peril": 0,
        "exclusion_clauses": 0,
        "exclusion_clauses_without_a_peril_sentence": 0,
        "documents_clauses": 0,
        "documents_clauses_without_a_known_peril": 0,
        "references_without_a_target": 0,
        "references_in_section_introductions": 0,
    }
    for path, wording in records.wordings:
        graph.add_node(
            Node(
                "Product",
                wording.product,
                path,
                "header line",
                {"wording_version": wording.wording_version},
            )
        )
        for clause in wording.clauses:
            graph.add_node(_clause_node(path, wording.product, clause))
    for path, wording in records.wordings:
        numbers = {c.number for c in wording.clauses}
        for clause in wording.clauses:
            _add_clause_edges(
                graph, path, wording.product, clause, numbers, perils, notes
            )
        notes["references_in_section_introductions"] += wording.introduction_references
    graph.notes.update(notes)


def _clause_node(path: str, product: str, clause: ClauseFacts) -> Node:
    return Node(
        "Clause",
        _clause_id(product, clause.number),
        path,
        f"clause {clause.number}",
        {
            "product": product,
            "number": clause.number,
            "title": clause.title,
            "section_number": clause.section_number,
            "section_title": clause.section_title,
        },
    )


def _add_clause_edges(
    graph: Graph,
    path: str,
    product: str,
    clause: ClauseFacts,
    numbers: set[str],
    perils: set[str],
    notes: dict[str, int],
) -> None:
    here = _clause_id(product, clause.number)
    heading = f"clause {clause.number} heading"
    graph.add_edge(Edge("part_of", here, product, path, heading))
    for target in clause.references:
        if target in numbers:
            body = f"clause {clause.number} body"
            graph.add_edge(
                Edge("refers_to", here, _clause_id(product, target), path, body)
            )
        else:
            notes["references_without_a_target"] += 1
    for peril in clause.excluded_perils:
        sentence = f"clause {clause.number} exclusion sentence"
        graph.add_edge(Edge("applies_to", here, peril, path, sentence))
    _add_peril_by_title(graph, path, here, clause, perils, notes)
    if clause.is_exclusion:
        notes["exclusion_clauses"] += 1
        notes[
            "exclusion_clauses_without_a_peril_sentence"
        ] += not clause.excluded_perils


def names_another_peril(body: str, own: str, perils: set[str]) -> bool:
    """Does the body name a peril other than ``own``, by its name with spaces
    or hyphens? The census counts it; no edge is made from it."""
    for peril in perils - {own}:
        phrase = r"[ -]".join(re.escape(word) for word in peril.split("_"))
        if re.search(rf"\b{phrase}\b", body, re.IGNORECASE):
            return True
    return False


def _add_peril_by_title(
    graph: Graph,
    path: str,
    here: str,
    clause: ClauseFacts,
    perils: set[str],
    notes: dict[str, int],
) -> None:
    title = f"clause {clause.number} title"
    if clause.is_cover:
        notes["cover_clauses"] += 1
        peril = slug(clause.title)
        if peril in perils:
            graph.add_edge(Edge("covers", here, peril, path, title))
            others = names_another_peril(clause.body, peril, perils)
            notes["cover_clauses_naming_another_peril"] += others
        else:
            notes["cover_clauses_without_a_known_peril"] += 1
    if clause.documents_peril is not None:
        notes["documents_clauses"] += 1
        if clause.documents_peril in perils:
            graph.add_edge(
                Edge("documents_for", here, clause.documents_peril, path, title)
            )
        else:
            notes["documents_clauses_without_a_known_peril"] += 1


def _add_policies(
    graph: Graph, records: Records, identities: Mapping[str, Identity] | None
) -> None:
    versions = {
        wording.product: wording.wording_version for _, wording in records.wordings
    }
    mismatched = 0
    for policy in records.policies:
        number = policy["policy_number"]
        identity = (
            identities[number] if identities is not None else derived_identity(policy)
        )
        mismatched += versions[policy["product"]] != policy["wording_version"]
        graph.add_node(
            Node(
                "Policy",
                number,
                POLICIES,
                "policy_number",
                {
                    "product": policy["product"],
                    "status": policy["status"],
                    "start_date": policy["start_date"],
                    "end_date": policy["end_date"],
                    "lapsed_on": policy["lapsed_on"],
                },
            )
        )
        _add_holder_and_asset(graph, policy, identity)
        graph.add_edge(
            Edge("of_product", number, policy["product"], POLICIES, "product")
        )
    graph.notes["policies_on_another_wording_version"] = mismatched


def _add_holder_and_asset(
    graph: Graph, policy: Mapping[str, Any], identity: Identity
) -> None:
    number = policy["policy_number"]
    if not graph.has_node(identity.address):
        graph.add_node(Node("Address", identity.address, POLICIES, "holder.address"))
    if not graph.has_node(identity.customer):
        graph.add_node(Node("Customer", identity.customer, POLICIES, "holder.email"))
        graph.add_edge(
            Edge(
                "lives_at",
                identity.customer,
                identity.address,
                POLICIES,
                "holder.address",
            )
        )
    if not graph.has_node(identity.asset):
        graph.add_node(
            Node(
                "Asset",
                identity.asset,
                POLICIES,
                asset_field(policy),
                {"family": asset_family(policy)},
            )
        )
    graph.add_edge(Edge("holds", identity.customer, number, POLICIES, "holder.email"))
    graph.add_edge(
        Edge("insures", number, identity.asset, POLICIES, asset_field(policy))
    )


def _add_events(graph: Graph, records: Records) -> None:
    for claim in records.claims:
        claim_id = claim["claim_id"]
        graph.add_node(
            Node(
                "Claim",
                claim_id,
                CLAIMS,
                "claim_id",
                {
                    "loss_date": claim["loss_date"],
                    "reported_on": claim["reported_on"],
                    "peril": claim["peril"],
                    "claimed_amount": claim["claimed_amount"],
                },
            )
        )
        graph.add_edge(
            Edge(
                "claimed_on", claim_id, claim["policy_number"], CLAIMS, "policy_number"
            )
        )
        graph.add_edge(
            Edge("reported_peril", claim_id, claim["peril"], CLAIMS, "peril")
        )
    for entry in records.history:
        entry_id = entry["history_id"]
        graph.add_node(
            Node(
                "HistoryEntry",
                entry_id,
                HISTORY,
                "history_id",
                {
                    "loss_date": entry["loss_date"],
                    "peril": entry["peril"],
                    "paid_amount": entry["paid_amount"],
                    "status": entry["status"],
                },
            )
        )
        graph.add_edge(
            Edge("had", entry["policy_number"], entry_id, HISTORY, "policy_number")
        )
        graph.add_edge(
            Edge("reported_peril", entry_id, entry["peril"], HISTORY, "peril")
        )
