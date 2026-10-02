"""The terms picked from retrieved wording clauses (S014): pure, no database.

The first group reads the real wordings, chunked the way ingestion chunks them,
and compares the result with the generator's catalogue, which wrote them. The
second group pins the probes. The third feeds hand-made chunks to the rules a
real wording never breaks.
"""

from collections.abc import Iterable
from typing import Any, get_args

import pytest
from generator import catalogue
from servicesupport import REPO_ROOT

from meridian.platform.knowledge_mcp.chunking import parse_wording
from meridian.workloads.claims_triage.models import Peril
from meridian.workloads.claims_triage.wording import (
    PERIL_TITLES,
    Clause,
    Terms,
    probes,
    select_terms,
)

WORDINGS = REPO_ROOT / "data" / "synthetic" / "wordings"
AMOUNTS_PROBE = "Deductible. Limit."
TIMING_PROBE = "Reporting a claim. Period of cover. Lapse for non-payment."


def real_chunks(product: catalogue.Product) -> list[dict[str, Any]]:
    text = (WORDINGS / f"{product.code}.md").read_text(encoding="utf-8")
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


def product_perils() -> list[tuple[str, str]]:
    return [
        (product.code, peril)
        for product in catalogue.PRODUCTS.values()
        for peril in catalogue.LINE_PERILS[product.line]
    ]


def numbers(*clauses: Clause | None) -> tuple[str | None, ...]:
    return tuple(None if clause is None else clause.clause for clause in clauses)


def test_peril_titles_are_the_catalogues_and_cover_every_peril() -> None:
    assert dict(PERIL_TITLES) == catalogue.PERIL_TITLES
    assert set(PERIL_TITLES) == set(get_args(Peril))


@pytest.mark.parametrize(("code", "peril"), product_perils())
def test_terms_of_the_real_wording_match_the_catalogue(code: str, peril: str) -> None:
    product = catalogue.PRODUCTS[code]
    covered = peril in product.covered
    expected_peril_exclusion = [
        catalogue.exclusion_clause(product, e.code)
        for e in product.exclusions
        if e.kind == catalogue.KIND_PERIL and peril in e.perils
    ]
    expected_candidates = [
        catalogue.exclusion_clause(product, e.code)
        for e in product.exclusions
        if e.kind == catalogue.KIND_CIRCUMSTANCE and peril in e.perils
    ]

    terms = select_terms(peril, real_chunks(product))  # type: ignore[arg-type]

    assert numbers(terms.cover) == (
        catalogue.cover_clause(product, peril) if covered else None,
    )
    assert numbers(terms.documents) == (
        catalogue.documents_clause(product, peril) if covered else None,
    )
    assert numbers(terms.peril_exclusion) == (
        expected_peril_exclusion[0] if expected_peril_exclusion else None,
    )
    assert [c.clause for c in terms.candidates] == expected_candidates
    assert terms.exclusions_complete is True
    assert numbers(
        terms.deductible, terms.limit, terms.reporting, terms.period, terms.lapse
    ) == (
        catalogue.DEDUCTIBLE_CLAUSE,
        catalogue.LIMIT_CLAUSE,
        catalogue.REPORTING_CLAUSE,
        catalogue.PERIOD_CLAUSE,
        catalogue.LAPSE_CLAUSE,
    )


def test_clauses_carry_the_title_and_body_of_their_chunk() -> None:
    product = catalogue.PRODUCTS["HOME-STD"]
    chunks = real_chunks(product)

    terms = select_terms("burst_pipe", chunks)

    assert terms.cover == Clause(
        "2.3", "Burst pipe", next(c["body"] for c in chunks if c["clause"] == "2.3")
    )
    assert terms.documents is not None
    assert terms.documents.title == "Documents for burst pipe"
    assert terms.deductible is not None
    assert terms.deductible.title == "Deductible"
    assert "EUR 250" in terms.deductible.body


def test_terms_do_not_depend_on_chunk_order_or_repeats() -> None:
    chunks = real_chunks(catalogue.PRODUCTS["HOME-STD"])

    forward = select_terms("storm", chunks)
    shuffled = select_terms("storm", [*reversed(chunks), *chunks[::2]])

    assert shuffled == forward


def test_probes_of_a_policy_in_force_ask_for_the_peril_its_exclusions_and_terms() -> (
    None
):
    assert probes("third_party_liability", in_force=True) == (
        "Third-party liability",
        "This exclusion applies to claims for third-party liability.",
        AMOUNTS_PROBE,
        TIMING_PROBE,
    )
    assert probes("burst_pipe", in_force=True) == (
        "Burst pipe",
        "This exclusion applies to claims for burst pipe.",
        AMOUNTS_PROBE,
        TIMING_PROBE,
    )


def test_probes_of_a_policy_not_in_force_ask_for_the_timing_terms_only() -> None:
    assert probes("burst_pipe", in_force=False) == (TIMING_PROBE,)


@pytest.mark.parametrize("peril", get_args(Peril))
def test_probes_are_built_from_the_peril_alone(peril: Peril) -> None:
    label = PERIL_TITLES[peril].lower()

    assert probes(peril, in_force=True)[:2] == (
        PERIL_TITLES[peril],
        f"This exclusion applies to claims for {label}.",
    )


# -- hand-made chunks ---------------------------------------------------------


def chunk(clause: str, title: str, body: str = "Text.") -> dict[str, Any]:
    return {
        "clause": clause,
        "section": "Any",
        "title": title,
        "body": body,
        "keyword_match": False,
    }


def circumstance(clause: str, names: str, title: str = "Wear") -> dict[str, Any]:
    body = f"Prose.\n\nThis exclusion applies to claims for {names}."
    return chunk(clause, title, body)


def peril_excluded(clause: str, names: str, title: str = "Flood") -> dict[str, Any]:
    return chunk(
        clause,
        title,
        f"Prose.\n\nThis exclusion applies to claims for {names}, which this product "
        "does not cover.",
    )


def select(peril: Peril, chunks: Iterable[dict[str, Any]]) -> Terms:
    return select_terms(peril, chunks)


def test_no_chunk_gives_no_terms_and_incomplete_exclusions() -> None:
    assert select("fire", []) == Terms(
        cover=None,
        documents=None,
        peril_exclusion=None,
        candidates=(),
        exclusions_complete=False,
        deductible=None,
        limit=None,
        reporting=None,
        period=None,
        lapse=None,
    )


def test_chunks_without_a_section_three_clause_leave_the_exclusions_incomplete() -> (
    None
):
    chunks = [chunk("2.1", "Fire"), chunk("4.1", "Deductible")]

    assert select("fire", chunks).exclusions_complete is False


def test_one_readable_section_three_clause_makes_the_exclusions_complete() -> None:
    assert select("fire", [circumstance("3.1", "storm")]).exclusions_complete is True


def test_a_gap_in_section_three_makes_the_exclusions_incomplete() -> None:
    terms = select(
        "storm", [circumstance("3.1", "storm"), circumstance("3.3", "storm")]
    )

    assert terms.exclusions_complete is False
    assert [c.clause for c in terms.candidates] == ["3.1", "3.3"]


def test_section_three_that_does_not_start_at_one_is_incomplete() -> None:
    assert select("storm", [circumstance("3.2", "storm")]).exclusions_complete is False


def test_consecutive_section_three_clauses_are_complete() -> None:
    chunks = [circumstance("3.2", "storm"), circumstance("3.1", "burst pipe")]

    assert select("storm", chunks).exclusions_complete is True


def test_a_missing_applies_to_sentence_leaves_the_clause_out_and_incomplete() -> None:
    terms = select(
        "flood", [chunk("3.1", "Flood", "This product does not cover flood.")]
    )

    assert terms.exclusions_complete is False
    assert terms.peril_exclusion is None
    assert terms.candidates == ()


def test_an_unknown_peril_name_leaves_the_clause_out_and_incomplete() -> None:
    unknown = circumstance("3.1", "storm and meteor strike")

    terms = select("storm", [unknown, circumstance("3.2", "storm")])

    assert terms.exclusions_complete is False
    assert [c.clause for c in terms.candidates] == ["3.2"]


def test_an_applies_to_sentence_wrapped_over_two_lines_is_read() -> None:
    wrapped = chunk(
        "3.1",
        "Wear and tear",
        "Prose that\nends here.\n\n"
        "This exclusion applies to claims for storm\nand burst pipe.",
    )

    terms = select("burst_pipe", [wrapped])

    assert terms.exclusions_complete is True
    assert [c.clause for c in terms.candidates] == ["3.1"]


def test_a_peril_exclusion_wrapped_over_lines_is_read() -> None:
    wrapped = chunk(
        "3.1",
        "Own vehicle damage",
        "This exclusion applies to claims for collision, theft, fire, glass\n"
        "and storm, which this product\ndoes not cover.",
    )

    terms = select("glass", [wrapped])

    assert terms.exclusions_complete is True
    assert terms.peril_exclusion is not None
    assert terms.peril_exclusion.clause == "3.1"
    assert terms.candidates == ()


def test_a_circumstance_does_not_name_the_peril_when_it_names_another() -> None:
    terms = select("fire", [circumstance("3.1", "storm")])

    assert terms.candidates == ()
    assert terms.peril_exclusion is None
    assert terms.exclusions_complete is True


def test_a_peril_exclusion_is_not_a_candidate_and_the_reverse() -> None:
    terms = select(
        "flood", [peril_excluded("3.1", "flood"), circumstance("3.2", "flood")]
    )

    assert numbers(terms.peril_exclusion) == ("3.1",)
    assert [c.clause for c in terms.candidates] == ["3.2"]


def test_a_sentence_in_the_middle_of_a_body_is_not_the_applies_to_sentence() -> None:
    body = "This exclusion applies to claims for storm. More prose follows."

    terms = select("storm", [chunk("3.1", "Wear", body)])

    assert terms.exclusions_complete is False
    assert terms.candidates == ()


def test_duplicated_chunks_are_taken_once() -> None:
    chunks = [circumstance("3.1", "storm")] * 3 + [chunk("4.1", "Deductible")] * 2

    terms = select("storm", chunks)

    assert [c.clause for c in terms.candidates] == ["3.1"]
    assert terms.exclusions_complete is True
    assert numbers(terms.deductible) == ("4.1",)


def test_clause_3_10_sorts_after_3_9() -> None:
    chunks = [circumstance(f"3.{n}", "storm") for n in (10, 2, 9, 1, 3, 4, 5, 6, 7, 8)]

    terms = select("storm", chunks)

    assert [c.clause for c in terms.candidates] == [f"3.{n}" for n in range(1, 11)]
    assert terms.exclusions_complete is True


def test_the_section_one_deductible_alone_gives_no_deductible() -> None:
    terms = select(
        "fire",
        [
            chunk("1.3", "Deductible"),
            chunk("1.5", "Period of cover"),
        ],
    )

    assert terms.deductible is None
    assert terms.period is None


def test_the_section_decides_between_clauses_of_the_same_title() -> None:
    terms = select(
        "fire",
        [
            chunk("1.3", "Deductible", "Definition."),
            chunk("4.1", "Deductible", "Amount."),
            chunk("6.1", "Period of cover", "Dates."),
            chunk("1.5", "Period of cover", "Definition."),
        ],
    )

    assert terms.deductible == Clause("4.1", "Deductible", "Amount.")
    assert terms.period == Clause("6.1", "Period of cover", "Dates.")


def test_titles_are_compared_exactly() -> None:
    terms = select(
        "burst_pipe",
        [
            chunk("2.1", "burst pipe"),
            chunk("2.2", "Burst pipes"),
            chunk("5.2", "Documents for Burst pipe"),
            chunk("4.2", "limit"),
        ],
    )

    assert terms.cover is None
    assert terms.documents is None
    assert terms.limit is None


def test_the_peril_cover_and_documents_clauses_are_found_by_title() -> None:
    terms = select(
        "third_party_liability",
        [
            chunk("2.1", "Third-party liability", "Cover."),
            chunk("5.2", "Documents for third-party liability", "Docs."),
            chunk("5.1", "Reporting a claim", "Report."),
            chunk("6.2", "Lapse for non-payment", "Lapse."),
            chunk("4.2", "Limit", "Limit."),
        ],
    )

    assert terms.cover == Clause("2.1", "Third-party liability", "Cover.")
    assert terms.documents == Clause(
        "5.2", "Documents for third-party liability", "Docs."
    )
    assert numbers(terms.reporting, terms.lapse, terms.limit) == ("5.1", "6.2", "4.2")


def test_a_clause_number_that_is_not_n_dot_m_is_refused() -> None:
    with pytest.raises(ValueError, match="clause"):
        select("fire", [chunk("three", "Flood")])
