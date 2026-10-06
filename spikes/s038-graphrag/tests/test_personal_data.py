"""Nothing personal-data-shaped leaves the spike: not in a node's repr, an
attribute, an edge, the census, the printed output or the README. The checks
count offenders and assert on the count, so a failure prints a number and
never the text it found."""

import json
import subprocess
import sys
from pathlib import Path

from claimgraph.census import census
from claimgraph.model import Graph

SPIKE = Path(__file__).resolve().parents[1]
README = SPIKE / "README.md"


def _personal_strings(data_dir: Path) -> set[str]:
    policies = json.loads((data_dir / "policies.json").read_text(encoding="utf-8"))
    claims = json.loads((data_dir / "claims.json").read_text(encoding="utf-8"))
    found: set[str] = set()
    for policy in policies:
        holder = policy["holder"]
        found |= {holder["name"], holder["email"], holder["address"]["street"]}
        insured = policy["insured_object"]
        found.add(insured.get("registration") or insured["address"]["street"])
    for claim in claims:
        found |= {claim["claimant"]["name"], claim["claimant"]["email"]}
        found.add(claim["description"])
    return {s for s in found if s}


def _everything_printable(graph: Graph) -> str:
    parts: list[str] = [json.dumps(census(graph), default=str)]
    for node in graph.nodes():
        parts += [repr(node), str(node), json.dumps(dict(node.attrs), default=str)]
        parts += [node.id, node.source_file, node.source_field]
    for edge in graph.edges():
        parts += [repr(edge), edge.source_file, edge.source_field]
    return "\n".join(parts)


def test_the_files_hold_the_personal_strings_the_checks_look_for(
    data_dir: Path,
) -> None:
    strings = _personal_strings(data_dir)

    assert len(strings) > 150


def test_no_node_edge_or_census_of_either_graph_holds_a_personal_string(
    data_dir: Path, graph: Graph, variant: Graph
) -> None:
    strings = _personal_strings(data_dir)

    offenders = 0
    for built in (graph, variant):
        blob = _everything_printable(built)
        offenders += sum(1 for s in strings if s in blob)

    assert offenders == 0


def test_a_node_prints_its_kind_and_its_id_and_nothing_else(graph: Graph) -> None:
    different = sum(1 for n in graph.nodes() if repr(n) != f"{n.kind}({n.id})")
    edge_different = sum(
        1 for e in graph.edges() if repr(e) != f"{e.kind}({e.src} -> {e.dst})"
    )

    assert different == 0
    assert edge_different == 0


def test_the_ids_of_customers_addresses_and_assets_are_digests_not_the_field(
    graph: Graph,
) -> None:
    shaped = 0
    for kind, prefix in (("Customer", "CUS-"), ("Address", "ADR-"), ("Asset", "AST-")):
        for node in graph.nodes(kind):
            digits = node.id.removeprefix(prefix)
            shaped += not (
                node.id.startswith(prefix)
                and len(digits) == 12
                and all(c in "0123456789abcdef" for c in digits)
            )

    assert shaped == 0


def test_the_printed_census_and_the_readme_hold_no_personal_string(
    data_dir: Path,
) -> None:
    strings = _personal_strings(data_dir)
    done = subprocess.run(
        [sys.executable, "-m", "claimgraph"],
        capture_output=True,
        text=True,
        check=True,
        cwd=SPIKE.parents[1],
        env={"PYTHONPATH": str(SPIKE / "src"), "PATH": ""},
    )

    blob = done.stdout + README.read_text(encoding="utf-8")

    assert done.stdout.count('"label"') == 2
    assert sum(1 for s in strings if s in blob) == 0
