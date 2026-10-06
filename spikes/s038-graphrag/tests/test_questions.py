"""The named questions, each on hand-picked nodes of the committed data. The
assertions hold IDs, clause numbers and counts only."""

from datetime import date, timedelta

import pytest
from claimgraph.model import Graph
from claimgraph.questions import (
    claims_of_customer,
    claims_on_asset,
    clauses_bearing_on_claim,
    customers_sharing_address,
    events_before_loss,
    policies_of_customer,
)


def _customer_of(graph: Graph, policy: str) -> str:
    return graph.in_edges(policy, {"holds"})[0].src


def _asset_of(graph: Graph, policy: str) -> str:
    return graph.out_edges(policy, {"insures"})[0].dst


def _clauses(graph: Graph, claim: str, **kwargs) -> list[str]:
    return [b.clause.id for b in clauses_bearing_on_claim(graph, claim, **kwargs)]


def test_a_fire_claim_on_home_standard_gets_cover_documents_then_the_limit(
    graph: Graph,
) -> None:
    ranked = clauses_bearing_on_claim(graph, "CLM-0006")

    assert [b.clause.id for b in ranked] == [
        "HOME-STD/2.1",
        "HOME-STD/5.2",
        "HOME-STD/4.2",
    ]
    assert [b.hops for b in ranked] == [2, 2, 3]
    assert [b.relation for b in ranked] == ["covers", "documents_for", "refers_to"]


def test_a_storm_claim_on_home_plus_gets_the_exclusion_that_names_storm(
    graph: Graph,
) -> None:
    assert _clauses(graph, "CLM-0018") == [
        "HOME-PLUS/2.2",
        "HOME-PLUS/3.1",
        "HOME-PLUS/5.3",
    ]


def test_a_flood_claim_on_home_standard_gets_only_the_exclusion(graph: Graph) -> None:
    ranked = clauses_bearing_on_claim(graph, "CLM-0022")

    assert [b.clause.id for b in ranked] == ["HOME-STD/3.1"]
    assert ranked[0].relation == "applies_to"


def test_a_liability_claim_on_motor_tpl_gets_cover_exclusion_documents_and_limit(
    graph: Graph,
) -> None:
    assert _clauses(graph, "CLM-0007") == [
        "MOTOR-TPL/2.1",
        "MOTOR-TPL/3.2",
        "MOTOR-TPL/5.2",
        "MOTOR-TPL/4.2",
    ]


def test_the_cut_off_keeps_the_first_places_of_the_same_order(graph: Graph) -> None:
    full = _clauses(graph, "CLM-0007")

    assert len(full) == 4
    assert _clauses(graph, "CLM-0007", top=2) == full[:2]
    assert _clauses(graph, "CLM-0007", top=0) == []
    assert _clauses(graph, "CLM-0007", top=99) == full


def test_each_place_carries_its_reason_in_ids_and_kinds_only(graph: Graph) -> None:
    ranked = clauses_bearing_on_claim(graph, "CLM-0007")

    assert ranked[0].reason == (
        "2 hops via reported_peril, covers; ties by relation order, then clause number"
    )
    assert ranked[3].reason == (
        "3 hops via reported_peril, covers, refers_to; "
        "ties by relation order, then clause number"
    )
    assert [repr(e) for e in ranked[3].via] == [
        "reported_peril(CLM-0007 -> third_party_liability)",
        "covers(MOTOR-TPL/2.1 -> third_party_liability)",
        "refers_to(MOTOR-TPL/2.1 -> MOTOR-TPL/4.2)",
    ]


def test_no_claim_gets_a_clause_of_another_product_than_its_policys(
    graph: Graph,
) -> None:
    other_product = 0
    empty = 0
    for claim in graph.nodes("Claim"):
        policy = graph.out_edges(claim.id, {"claimed_on"})[0].dst
        product = graph.node(policy).attrs["product"]
        answer = clauses_bearing_on_claim(graph, claim.id)
        empty += not answer
        other_product += sum(1 for b in answer if b.clause.attrs["product"] != product)

    assert other_product == 0
    assert empty == 0


def test_the_events_of_a_policy_in_the_365_days_before_a_loss_date(
    graph: Graph,
) -> None:
    prior = events_before_loss(graph, "POL-0046", date(2026, 6, 20))

    assert len(prior.history) == 3
    assert prior.claims == []
    assert all(graph.in_edges(h.id, {"had"}) for h in prior.history)


def test_the_window_includes_its_first_day_and_excludes_the_loss_date_itself(
    graph: Graph,
) -> None:
    entry = events_before_loss(graph, "POL-0004", date(2026, 7, 1)).history[0]
    on = date.fromisoformat(entry.attrs["loss_date"])

    def ids(loss_date: date) -> list[str]:
        return [h.id for h in events_before_loss(graph, "POL-0004", loss_date).history]

    assert entry.id in ids(on + timedelta(days=365))
    assert entry.id not in ids(on + timedelta(days=366))
    assert entry.id not in ids(on)
    assert entry.id in ids(on + timedelta(days=1))


def test_the_events_of_a_policy_hold_its_claims_as_well_as_its_history(
    graph: Graph,
) -> None:
    prior = events_before_loss(graph, "POL-0002", date(2026, 12, 31), window_days=3650)

    assert [c.id for c in prior.claims] == ["CLM-0029"]
    assert len(prior.history) == 3


def test_a_policy_with_no_events_has_an_empty_answer(graph: Graph) -> None:
    prior = events_before_loss(graph, "POL-0015", date(2026, 12, 31))

    assert prior.claims == []
    assert prior.history == []


def test_the_policies_of_one_customer(graph: Graph) -> None:
    customer = _customer_of(graph, "POL-0004")

    assert [p.id for p in policies_of_customer(graph, customer)] == ["POL-0004"]


def test_the_claims_on_one_asset_come_with_the_history_and_the_path(
    graph: Graph,
) -> None:
    reached = claims_on_asset(graph, _asset_of(graph, "POL-0002"))

    assert sorted(r.node.id for r in reached if r.node.kind == "Claim") == ["CLM-0029"]
    assert sum(1 for r in reached if r.node.kind == "HistoryEntry") == 3
    assert {r.hops for r in reached} == {2}
    claim = next(r for r in reached if r.node.kind == "Claim")
    assert [e.kind for e in claim.path] == ["insures", "claimed_on"]


def test_the_customers_that_share_an_address_are_the_two_motor_holders(
    graph: Graph,
) -> None:
    first = _customer_of(graph, "POL-0016")
    second = _customer_of(graph, "POL-0024")

    assert [c.id for c in customers_sharing_address(graph, first)] == [second]
    assert [c.id for c in customers_sharing_address(graph, second)] == [first]
    alone = _customer_of(graph, "POL-0004")
    assert customers_sharing_address(graph, alone) == []


def test_the_claims_of_one_customer_across_policies(graph: Graph) -> None:
    with_claim = claims_of_customer(graph, _customer_of(graph, "POL-0002"))
    without = claims_of_customer(graph, _customer_of(graph, "POL-0012"))

    assert [r.node.id for r in with_claim] == ["CLM-0029"]
    assert [e.kind for e in with_claim[0].path] == ["holds", "claimed_on"]
    assert without == []


def test_a_question_about_a_node_of_the_wrong_kind_is_refused(graph: Graph) -> None:
    with pytest.raises(ValueError, match="not a Claim"):
        clauses_bearing_on_claim(graph, "POL-0002")
