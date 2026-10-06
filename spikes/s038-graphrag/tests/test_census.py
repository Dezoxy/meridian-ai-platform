"""What the committed data answers: the census as plain numbers. A regenerated
data set changes a number here and fails loudly."""

from claimgraph.census import census
from claimgraph.model import Graph


def test_the_census_of_the_committed_graph_carries_its_label(graph: Graph) -> None:
    result = census(graph)

    assert result["label"] == "committed data"
    assert set(result) == {"label", "nodes", "edges", "degrees", "questions", "notes"}


def test_the_degree_of_each_node_kind_for_each_edge_kind_is_counted(
    graph: Graph,
) -> None:
    degrees = census(graph)["degrees"]

    assert degrees["Customer"]["holds.out"] == {1: 50}
    assert degrees["Policy"]["holds.in"] == {1: 50}
    assert degrees["Policy"]["had.out"] == {0: 24, 1: 10, 2: 14, 3: 2}
    assert degrees["Policy"]["claimed_on.in"] == {0: 10, 1: 40}
    assert degrees["Asset"]["insures.in"] == {1: 50}
    assert degrees["Address"]["lives_at.in"] == {1: 48, 2: 1}
    assert degrees["Clause"]["part_of.out"] == {1: 85}
    assert 0 not in degrees["Peril"]["reported_peril.in"]


def test_the_committed_data_gives_no_relational_question_more_than_its_trivial_answer(
    graph: Graph,
) -> None:
    questions = census(graph)["questions"]

    assert questions["policies_of_customer"]["non_trivial"] == 0
    assert questions["claims_on_asset"]["non_trivial"] == 0
    assert questions["claims_of_customer"]["non_trivial"] == 0
    assert questions["customers_sharing_address"]["non_trivial"] == 2
    assert questions["customers_sharing_address"]["starts"] == 50


def test_the_questions_that_the_data_does_answer_are_counted(graph: Graph) -> None:
    questions = census(graph)["questions"]

    before = questions["events_before_loss"]
    assert before["starts"] == 40
    assert before["non_trivial"] == 10
    assert before["answer_sizes"] == {0: 30, 1: 8, 3: 2}
    bearing = questions["clauses_bearing_on_claim"]
    assert bearing["starts"] == 40
    assert bearing["non_trivial"] == 37


def test_every_question_reports_its_cost_as_counts(graph: Graph) -> None:
    questions = census(graph)["questions"]

    costs = [q["cost"] for q in questions.values()]
    assert all(c["nodes_visited"] > 0 and c["edges_followed"] > 0 for c in costs)
    assert all(isinstance(v, int) for c in costs for v in c.values())
