"""The catalogue, the wording documents and the citations that tie them together."""

import re
from pathlib import Path

import pytest
from generator import catalogue

MOTOR = ["collision", "theft", "fire", "glass", "storm", "third_party_liability"]
HOME = ["fire", "storm", "flood", "burst_pipe", "burglary", "accidental_damage"]
# code -> (line, covered perils, exclusions as (code, kind, perils), deductible,
# fixed limit)
PRODUCT_TABLE = {
    "MOTOR-TPL": (
        "motor",
        ["third_party_liability"],
        [
            (
                "own_vehicle_damage",
                "peril",
                ["collision", "theft", "fire", "glass", "storm"],
            ),
            ("racing", "circumstance", ["third_party_liability"]),
        ],
        0,
        1_000_000,
    ),
    "MOTOR-COMP": (
        "motor",
        MOTOR,
        [
            ("racing", "circumstance", ["collision"]),
            ("driving_under_influence", "circumstance", ["collision"]),
            ("unlicensed_driver", "circumstance", ["collision"]),
        ],
        300,
        None,
    ),
    "HOME-STD": (
        "home",
        ["fire", "storm", "burst_pipe", "burglary"],
        [
            ("flood", "peril", ["flood"]),
            ("accidental_damage", "peril", ["accidental_damage"]),
            ("wear_and_tear", "circumstance", ["storm", "burst_pipe"]),
            ("gradual_leak", "circumstance", ["burst_pipe"]),
        ],
        250,
        None,
    ),
    "HOME-PLUS": (
        "home",
        HOME,
        [
            ("wear_and_tear", "circumstance", ["storm", "burst_pipe"]),
            ("gradual_leak", "circumstance", ["burst_pipe"]),
        ],
        150,
        None,
    ),
}
REQUIRED = {
    "collision": ["photos", "repair_estimate"],
    "theft": ["police_report"],
    "fire": ["photos"],
    "glass": ["photos"],
    "storm": ["photos"],
    "flood": ["photos"],
    "burst_pipe": ["photos", "repair_estimate"],
    "burglary": ["police_report", "photos"],
    "accidental_damage": ["photos"],
    "third_party_liability": ["accident_statement"],
}
EXCLUSION_TITLE = {
    "own_vehicle_damage": "Own vehicle damage",
    "racing": "Racing",
    "driving_under_influence": "Driving under the influence",
    "unlicensed_driver": "Unlicensed driver",
    "flood": "Flood",
    "accidental_damage": "Accidental damage",
    "wear_and_tear": "Wear and tear",
    "gradual_leak": "Gradual leaks",
}
FIXED_TITLE = {
    "4.1": "Deductible",
    "4.2": "Limit",
    "5.1": "Reporting a claim",
    "6.1": "Period of cover",
    "6.2": "Lapse for non-payment",
}
HEADING = re.compile(r"^### (\d+\.\d+) (.+)$", re.MULTILINE)
WORDING_WIDTH = 80


def wording_headings(synthetic_dir: Path, code: str) -> dict[str, str]:
    text = (synthetic_dir / "wordings" / f"{code}.md").read_text(encoding="utf-8")
    return dict(HEADING.findall(text))


def peril_words(peril: str) -> str:
    return peril.replace("_", " ")


# -- the catalogue ------------------------------------------------------------
def test_the_catalogue_matches_the_product_table():
    assert list(catalogue.PRODUCTS) == list(PRODUCT_TABLE)
    for code, (line, covered, exclusions, deductible, limit) in PRODUCT_TABLE.items():
        product = catalogue.PRODUCTS[code]
        assert product.line == line
        assert list(product.covered) == covered
        assert [
            (e.code, e.kind, list(e.perils)) for e in product.exclusions
        ] == exclusions
        assert product.deductible == deductible
        assert product.fixed_limit == limit


def test_every_peril_of_the_line_is_covered_or_named_by_a_peril_exclusion():
    for product in catalogue.PRODUCTS.values():
        line = MOTOR if product.line == "motor" else HOME
        excluded = {
            peril
            for exclusion in product.exclusions
            if exclusion.kind == "peril"
            for peril in exclusion.perils
        }
        for peril in line:
            assert (peril in product.covered) != (peril in excluded), (
                product.code,
                peril,
            )


def test_the_catalogue_validates_itself():
    catalogue.validate_catalogue()


def test_required_documents_follow_the_contract_and_the_catalogue_order():
    assert {k: list(v) for k, v in catalogue.REQUIRED_DOCUMENTS.items()} == REQUIRED
    order = catalogue.DOCUMENT_ORDER
    for documents in catalogue.REQUIRED_DOCUMENTS.values():
        assert list(documents) == sorted(documents, key=order.index)


def test_the_parameters_follow_the_contract():
    assert catalogue.AUTO_APPROVAL_LIMIT == 2500
    assert catalogue.REPORTING_WINDOW_DAYS == 30
    assert catalogue.EARLY_LOSS_DAYS == 30
    assert catalogue.FREQUENT_CLAIMS_COUNT == 2
    assert catalogue.FREQUENT_CLAIMS_WINDOW_DAYS == 365
    assert catalogue.REFERENCE_DATE.isoformat() == "2026-09-01"


# -- the wordings -------------------------------------------------------------
@pytest.mark.parametrize("code", list(PRODUCT_TABLE))
def test_every_covered_peril_and_exclusion_has_a_clause(synthetic_dir: Path, code):
    headings = wording_headings(synthetic_dir, code)
    product = catalogue.PRODUCTS[code]
    for peril in product.covered:
        cover = catalogue.cover_clause(product, peril)
        documents = catalogue.documents_clause(product, peril)
        assert cover.startswith("2.")
        assert headings[cover].lower().replace("-", " ") == peril_words(peril)
        assert headings[documents] == f"Documents for {peril_words(peril)}".replace(
            "third party", "third-party"
        )
    for exclusion in product.exclusions:
        clause = catalogue.exclusion_clause(product, exclusion.code)
        assert clause.startswith("3.")
        assert headings[clause] == EXCLUSION_TITLE[exclusion.code]


@pytest.mark.parametrize("code", list(PRODUCT_TABLE))
def test_the_wording_layout_follows_the_contract(synthetic_dir: Path, code):
    text = (synthetic_dir / "wordings" / f"{code}.md").read_text(encoding="utf-8")
    product = catalogue.PRODUCTS[code]
    sections = re.findall(r"^## (\d)\. (.+)$", text, re.MULTILINE)
    assert text.startswith(f"# Meridian Insurance — {product.name}\n")
    assert f"`{code}`" in text
    assert "2026-01" in text
    assert "synthetic and fictional" in text
    assert sections == [
        ("1", "Definitions"),
        ("2", "What is covered"),
        ("3", "What is not covered"),
        ("4", "Deductible and limits"),
        ("5", "Making a claim"),
        ("6", "Period of cover"),
    ]
    clauses = list(wording_headings(synthetic_dir, code))
    assert clauses == sorted(clauses, key=lambda c: tuple(map(int, c.split("."))))
    assert len(clauses) == len(set(clauses))
    for clause, title in FIXED_TITLE.items():
        assert wording_headings(synthetic_dir, code)[clause] == title


@pytest.mark.parametrize("code", list(PRODUCT_TABLE))
def test_wording_text_is_wrapped_and_clean(synthetic_dir: Path, code):
    text = (synthetic_dir / "wordings" / f"{code}.md").read_text(encoding="utf-8")
    assert text.endswith("\n")
    assert not text.endswith("\n\n")
    for number, line in enumerate(text.splitlines(), 1):
        assert line == line.rstrip(), (code, number)
        if not line.startswith("#"):
            assert len(line) <= WORDING_WIDTH, (code, number)


@pytest.mark.parametrize("code", list(PRODUCT_TABLE))
def test_each_clause_is_two_to_five_sentences(synthetic_dir: Path, code):
    text = (synthetic_dir / "wordings" / f"{code}.md").read_text(encoding="utf-8")
    body = re.split(r"^### \d+\.\d+ .+$", text, flags=re.MULTILINE)[1:]
    for clause in body:
        clause = re.sub(r"^## .*$", "", clause, flags=re.MULTILINE)
        prose = " ".join(
            line for line in clause.split("\n") if line and not line.startswith("- ")
        )
        sentences = re.findall(r"[.!?](?:\s|$)", prose)
        assert 2 <= len(sentences) <= 5, (code, clause[:60])


def test_flood_is_covered_by_home_plus_and_excluded_by_home_standard(
    synthetic_dir: Path,
):
    plus = wording_headings(synthetic_dir, "HOME-PLUS")
    standard = wording_headings(synthetic_dir, "HOME-STD")
    assert plus["2.3"] == "Flood"
    assert standard["3.1"] == "Flood"
    assert "2.3" not in standard or standard["2.3"] != "Flood"


# -- the citations ------------------------------------------------------------
def test_every_citation_resolves_to_a_heading_in_the_cited_wording(
    synthetic_dir: Path, outcomes
):
    for outcome in outcomes:
        assert outcome["citations"], outcome["claim_id"]
        for citation in outcome["citations"]:
            path = synthetic_dir / "wordings" / f"{citation['wording']}.md"
            text = path.read_text(encoding="utf-8")
            heading = rf"^### {re.escape(citation['clause'])} "
            assert re.search(heading, text, re.MULTILINE), (
                outcome["claim_id"],
                citation,
            )


def test_citations_name_the_wording_of_the_claims_policy(claims, policies, outcomes):
    policy_of = {p["policy_number"]: p for p in policies}
    claim_of = {c["claim_id"]: c for c in claims}
    for outcome in outcomes:
        product = policy_of[claim_of[outcome["claim_id"]]["policy_number"]]["product"]
        assert {c["wording"] for c in outcome["citations"]} == {product}


def test_each_citation_points_at_the_clause_the_label_is_about(
    synthetic_dir: Path, claims, policies, outcomes
):
    policy_of = {p["policy_number"]: p for p in policies}
    claim_of = {c["claim_id"]: c for c in claims}
    for outcome in outcomes:
        claim = claim_of[outcome["claim_id"]]
        product = policy_of[claim["policy_number"]]["product"]
        headings = wording_headings(synthetic_dir, product)
        peril = peril_words(claim["peril"])
        titles = [headings[c["clause"]] for c in outcome["citations"]]
        reason = outcome["reason"]
        if reason in ("within_threshold", "over_threshold", "fraud_indicator"):
            capped = (
                claim["claimed_amount"] > policy_of[claim["policy_number"]]["limit"]
            )
            tail = ["Deductible"]
            tail += ["Limit"] if capped else []
            tail += (
                ["Reporting a claim"]
                if "late_report" in outcome["fraud_indicators"]
                else []
            )
            assert titles[0].lower().replace("-", " ") == peril
            assert titles[1:] == tail
        elif reason == "excluded":
            assert titles == [EXCLUSION_TITLE[outcome["exclusion"]]]
        elif reason == "missing_documents":
            assert titles[0].lower().replace("-", " ") == f"documents for {peril}"
        else:
            assert titles in (["Period of cover"], ["Lapse for non-payment"])
