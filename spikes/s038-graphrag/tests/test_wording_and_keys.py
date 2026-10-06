"""The recognisers on small texts, and the keys on made-up records: both sides
of each rule, so a rule that stops working is seen here and not only in a
count of the committed data."""

from claimgraph.build import Records, build_from
from claimgraph.keys import address_id, asset_id, customer_id
from claimgraph.wording import excluded_perils, read_wording, references, slug


def test_a_slug_is_the_peril_name_for_a_title_with_spaces_or_hyphens() -> None:
    assert slug("Burst pipe") == "burst_pipe"
    assert slug("Third-party liability") == "third_party_liability"
    assert slug("  Glass ") == "glass"


def test_an_exclusion_sentence_names_its_perils_across_a_line_break() -> None:
    body = (
        "Text.\n\nThis exclusion applies to claims for collision, theft, fire,\n"
        "glass and storm, which this product does not cover."
    )

    assert excluded_perils(body) == ("collision", "theft", "fire", "glass", "storm")


def test_an_exclusion_sentence_that_ends_at_a_full_stop_names_one_peril() -> None:
    body = "Text.\n\nThis exclusion applies to claims for third-party liability."

    assert excluded_perils(body) == ("third_party_liability",)


def test_a_body_without_the_sentence_names_no_peril() -> None:
    body = "We do not pay for damage that results from gradual deterioration."

    assert excluded_perils(body) == ()


def test_a_reference_is_clause_and_a_number_in_any_case_once() -> None:
    body = "As clause 4.1 explains. Clause 6.1 and, again, clause 4.1; claused 9.9."

    assert references(body) == ("4.1", "6.1")


WORDING = """# Title

Product code: `X-1`. Wording version: 2030-01.

## 1. What is covered

Intro naming clause 1.1 and Clause 9.9.

### 1.1 Storm

We cover storm. See clause 2.1 and clause 7.7.

### 1.2 Falling meteors

We cover them.

## 2. Making a claim

### 2.1 Documents for storm

Photos.
"""


def test_cover_and_documents_clauses_are_read_from_their_section_and_title() -> None:
    facts = read_wording(WORDING)

    cover = [c for c in facts.clauses if c.is_cover]
    assert [c.title for c in cover] == ["Storm", "Falling meteors"]
    assert [c.documents_peril for c in facts.clauses] == [None, None, "storm"]


def test_text_before_a_sections_first_clause_is_counted_not_linked() -> None:
    facts = read_wording(WORDING)

    assert facts.introduction_references == 2
    assert facts.clauses[0].references == ("2.1", "7.7")


def test_the_graph_counts_what_it_could_not_read_in_a_small_wording() -> None:
    policy = {
        "policy_number": "POL-9001",
        "product": "X-1",
        "wording_version": "2030-01",
        "status": "active",
        "start_date": "2030-01-01",
        "end_date": "2030-12-31",
        "lapsed_on": None,
        "holder": _policy("a@example.invalid", "SYN-0001", "Elm 1")["holder"],
        "insured_object": {"registration": "SYN-0001"},
    }
    claim = {
        "claim_id": "CLM-9001",
        "policy_number": "POL-9001",
        "reported_on": "2030-02-01",
        "loss_date": "2030-02-01",
        "peril": "storm",
        "claimed_amount": 100,
    }
    records = Records(
        (policy,), (claim,), (), (("wordings/X-1.md", read_wording(WORDING)),)
    )

    graph = build_from(records)

    assert graph.notes["cover_clauses"] == 2
    assert graph.notes["cover_clauses_without_a_known_peril"] == 1
    assert graph.notes["references_without_a_target"] == 1
    assert {e.dst for e in graph.edges("covers")} == {"storm"}
    assert {e.dst for e in graph.edges("refers_to")} == {"X-1/2.1"}


def _policy(email: str, registration: str, street: str) -> dict:
    return {
        "holder": {
            "email": email,
            "address": {"street": street, "city": "Linz", "country": "AT"},
        },
        "insured_object": {"registration": registration},
    }


def test_two_policies_with_the_same_email_in_any_case_have_one_customer() -> None:
    first = _policy("a@example.invalid", "SYN-0001", "Elm 1")
    again = _policy(" A@Example.invalid ", "SYN-0002", "Elm 2")
    other = _policy("b@example.invalid", "SYN-0003", "Elm 3")

    assert customer_id(first) == customer_id(again)
    assert customer_id(first) != customer_id(other)


def test_one_registration_in_any_case_and_spacing_is_one_vehicle() -> None:
    first = _policy("a@example.invalid", "SYN-0001", "Elm 1")
    again = _policy("b@example.invalid", "syn-0001 ", "Elm 2")
    other = _policy("c@example.invalid", "SYN-0002", "Elm 3")

    assert asset_id(first) == asset_id(again)
    assert asset_id(first) != asset_id(other)


def test_an_address_is_street_city_and_country_normalised_for_case() -> None:
    first = _policy("a@example.invalid", "SYN-0001", "Elm  Road 1")
    again = _policy("b@example.invalid", "SYN-0002", "elm road 1")
    other = _policy("c@example.invalid", "SYN-0003", "Elm Road 2")

    assert address_id(first) == address_id(again)
    assert address_id(first) != address_id(other)
