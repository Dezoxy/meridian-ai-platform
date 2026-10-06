"""The two rankings that need no database: the graph's ordered list for a
claim, and the set of clauses the production triage names for its peril."""

import json
from pathlib import Path

import pytest
from claimgraph.model import Graph
from claimgraph.questions import clauses_bearing_on_claim
from comparison import rankings
from stacksupport import whole_wording


@pytest.fixture(scope="module")
def inputs(data_dir: Path) -> list[rankings.ClaimInput]:
    return rankings.claim_inputs(data_dir)


def test_a_claim_input_holds_structured_fields_and_never_the_description(
    inputs: list[rankings.ClaimInput], data_dir: Path
) -> None:
    claims = json.loads((data_dir / "claims.json").read_text(encoding="utf-8"))
    descriptions = {c["description"] for c in claims}

    blob = repr(inputs)

    assert len(inputs) == 40
    assert not any(d in blob for d in descriptions)
    assert set(rankings.ClaimInput.__dataclass_fields__) == {
        "claim_id",
        "policy_number",
        "peril",
        "product",
        "wording_version",
    }


def test_the_graph_list_of_a_claim_is_the_clauses_in_the_order_of_the_question(
    graph: Graph, inputs: list[rankings.ClaimInput]
) -> None:
    lists = rankings.graph_lists(graph, inputs)

    assert lists["CLM-0001"] == ["2.2", "3.1", "5.3"]
    assert lists["CLM-0008"] == ["2.1", "3.1", "3.2", "3.3", "5.2"]
    assert lists["CLM-0020"] == ["3.1"]
    assert set(lists) == {i.claim_id for i in inputs}


def test_a_cut_off_keeps_the_front_of_the_list_and_never_pads_it(
    graph: Graph, inputs: list[rankings.ClaimInput]
) -> None:
    lists = rankings.graph_lists(graph, inputs)

    for claim, full in lists.items():
        for top in (1, 3, 5, 10):
            cut = [
                b.clause.attrs["number"]
                for b in clauses_bearing_on_claim(graph, claim, top=top)
            ]
            assert cut == full[:top]
            assert len(cut) == min(top, len(full))
    assert max(len(full) for full in lists.values()) == 5


def test_the_production_set_is_what_select_terms_names_over_the_whole_wording(
    inputs: list[rankings.ClaimInput], data_dir: Path
) -> None:
    sets = rankings.production_sets(data_dir, inputs)

    assert sets["CLM-0001"] == ("2.2", "3.1", "4.1", "4.2", "5.1", "5.3", "6.1", "6.2")
    assert sets["CLM-0020"] == ("3.1", "4.1", "4.2", "5.1", "6.1", "6.2")
    assert set(sets) == {i.claim_id for i in inputs}
    assert all(list(s) == sorted(s, key=_number) for s in sets.values())


def _number(clause: str) -> tuple[int, int]:
    section, _, n = clause.partition(".")
    return int(section), int(n)


def test_the_whole_wording_read_here_equals_the_one_the_platform_tests_read(
    data_dir: Path,
) -> None:
    for product in ("HOME-PLUS", "HOME-STD", "MOTOR-COMP", "MOTOR-TPL"):
        assert rankings.whole_wording(data_dir, product) == whole_wording(product)


def test_the_scope_of_a_claim_is_the_clauses_of_its_wording(data_dir: Path) -> None:
    sizes = rankings.scope_sizes(data_dir)

    assert sizes == {
        ("HOME-PLUS", "2026-01"): 24,
        ("HOME-STD", "2026-01"): 22,
        ("MOTOR-COMP", "2026-01"): 25,
        ("MOTOR-TPL", "2026-01"): 14,
    }
    assert sum(sizes.values()) == 85
