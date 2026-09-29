"""Claim descriptions never contradict their circumstance, the season or the home.

The sweeps call the description builder for every combination that the
scenario builders can produce: each peril with and without a late report, and
each circumstance exclusion of the catalogue with the perils it applies to.
"""

import random
import re
from datetime import date

import pytest
from generator import catalogue, narratives

SAMPLE_DATE = date(2026, 7, 15)
SEEDS = range(25)
CIRCUMSTANCE_KEYWORD = {
    "racing": "track day",
    "driving_under_influence": "drinks",
    "unlicensed_driver": "licence",
    "wear_and_tear": "rotten",
    "gradual_leak": "months",
}
WINTER = re.compile(
    r"\b(cold|froze|frozen|freezing|frost\w*|snow\w*|ice|icy|winter)\b", re.IGNORECASE
)
# What an innocent claimant's collision sounds like.
INNOCENT_COLLISION = re.compile(
    r"junction|pulled out|reversed|car park|hit by another|gave me his|other driver",
    re.IGNORECASE,
)
DRIVER_NAMED = re.compile(r"driving|drove|wheel|driver", re.IGNORECASE)
OWN_ROOF_OR_FLOOR = re.compile(r"(roof|ground floor) of my apartment", re.IGNORECASE)
SINGLE_VEHICLE = ("driving_under_influence", "unlicensed_driver")


def circumstance_cases() -> list[tuple[str, str, str]]:
    """(code, peril, line) for every circumstance exclusion the catalogue has."""
    return sorted(
        {
            (exclusion.code, peril, product.line)
            for product in catalogue.PRODUCTS.values()
            for exclusion in product.exclusions
            if exclusion.kind == catalogue.KIND_CIRCUMSTANCE
            for peril in exclusion.perils
        }
    )


def scene_texts(scene: narratives.Scene) -> list[str]:
    return [*scene.openers, *scene.details, *scene.sentences]


def every_text() -> list[str]:
    texts = list(narratives.DELAY_REASONS)
    for openers, details in narratives.TEMPLATES.values():
        texts += [*openers, *details]
    for scene in narratives.CIRCUMSTANCE_SCENES.values():
        texts += scene_texts(scene)
    return texts


def describe(line, peril, *, subject="my car", **options) -> str:
    rng = random.Random(options.pop("seed", 0))  # noqa: S311 (seeded, not secret)
    return narratives.describe(
        rng, line, peril, subject=subject, city="Graz", loss_date=SAMPLE_DATE, **options
    )


# -- scenes -------------------------------------------------------------------
def test_every_circumstance_the_catalogue_can_produce_has_exactly_one_scene():
    cases = {(code, peril) for code, peril, _ in circumstance_cases()}
    assert set(narratives.CIRCUMSTANCE_SCENES) == cases


@pytest.mark.parametrize(("code", "peril", "line"), circumstance_cases())
def test_every_variant_of_a_circumstance_states_it(code, peril, line):
    scene = narratives.CIRCUMSTANCE_SCENES[(code, peril)]
    for sentence in scene.sentences:
        assert CIRCUMSTANCE_KEYWORD[code] in sentence.lower()


def test_an_unlicensed_driver_scene_does_not_say_who_was_driving():
    scenes = [
        scene
        for (code, _), scene in narratives.CIRCUMSTANCE_SCENES.items()
        if code == "unlicensed_driver"
    ]
    assert scenes
    for scene in scenes:
        for text in (*scene.openers, *scene.details):
            assert not DRIVER_NAMED.search(text), text


def test_single_vehicle_scenes_describe_no_other_road_user():
    scenes = [
        scene
        for (code, peril), scene in narratives.CIRCUMSTANCE_SCENES.items()
        if code in SINGLE_VEHICLE or (code, peril) == ("racing", "collision")
    ]
    assert len(scenes) == 3
    for scene in scenes:
        for text in (*scene.openers, *scene.details):
            assert not INNOCENT_COLLISION.search(text), text


def test_a_drink_driving_scene_has_the_claimant_at_the_wheel_late_in_the_day():
    scene = narratives.CIRCUMSTANCE_SCENES[("driving_under_influence", "collision")]
    for opener in scene.openers:
        assert re.search(r"\bI (was driving|lost control|was at the wheel)", opener)
        assert re.search(r"evening|midnight|night", opener)


@pytest.mark.parametrize("peril", ["collision", "third_party_liability"])
def test_a_racing_scene_happens_on_the_circuit_near_the_city(peril):
    scene = narratives.CIRCUMSTANCE_SCENES[("racing", peril)]
    for opener in scene.openers:
        assert "racing circuit near {city}" in opener


def test_a_circumstance_without_a_scene_is_an_error():
    with pytest.raises(ValueError, match="no scene"):
        describe("motor", "theft", circumstance="racing")


# -- season, home and delay ---------------------------------------------------
def test_no_template_mentions_a_season():
    for text in every_text():
        assert not WINTER.search(text), text


def test_no_home_description_gives_an_apartment_a_roof_or_ground_floor_of_its_own():
    home = [(line, peril) for line, peril in narratives.TEMPLATES if line == "home"]
    for line, peril in home:
        for seed in SEEDS:
            text = describe(line, peril, subject="my apartment", seed=seed)
            assert not OWN_ROOF_OR_FLOOR.search(text), text
            text = describe(line, peril, subject="my house", seed=seed)
            assert "roof above my house" not in text, text


def test_a_late_report_gives_a_reason_that_comes_after_the_loss():
    for reason in narratives.DELAY_REASONS:
        assert "afterwards" in reason or "did not realise" in reason, reason


# -- sweeps -------------------------------------------------------------------
@pytest.mark.parametrize(("line", "peril"), list(narratives.TEMPLATES))
@pytest.mark.parametrize("delayed", [False, True])
def test_every_peril_reads_as_two_to_three_finished_sentences(line, peril, delayed):
    for seed in SEEDS:
        text = describe(line, peril, delayed=delayed, seed=seed)
        sentences = re.findall(r"[.!?](?:\s|$)", text)
        assert len(sentences) == (3 if delayed else 2), text
        assert "{" not in text and "}" not in text, text


@pytest.mark.parametrize(("code", "peril", "line"), circumstance_cases())
def test_every_circumstance_reads_as_three_sentences_with_its_keyword(
    code, peril, line
):
    for seed in SEEDS:
        text = describe(line, peril, circumstance=code, seed=seed)
        assert len(re.findall(r"[.!?](?:\s|$)", text)) == 3, text
        assert CIRCUMSTANCE_KEYWORD[code] in text.lower(), text
        assert "{" not in text and "}" not in text, text


# -- committed descriptions ---------------------------------------------------
def test_no_committed_description_mentions_a_season(claims):
    for claim in claims:
        assert not WINTER.search(claim["description"]), claim["claim_id"]


def test_committed_motor_circumstance_claims_are_not_innocent_collisions(
    claims, outcomes
):
    claim_of = {claim["claim_id"]: claim for claim in claims}
    checked = []
    for outcome in outcomes:
        if outcome["exclusion"] in ("driving_under_influence", "unlicensed_driver"):
            text = claim_of[outcome["claim_id"]]["description"]
            assert not INNOCENT_COLLISION.search(text), text
            checked.append(outcome["exclusion"])
        if outcome["exclusion"] == "racing":
            text = claim_of[outcome["claim_id"]]["description"]
            assert "racing circuit near" in text, text
            checked.append("racing")
    assert sorted(checked) == ["driving_under_influence", "racing", "unlicensed_driver"]


def test_committed_apartment_claims_have_no_roof_or_ground_floor_of_their_own(
    claims, policies
):
    policy_of = {policy["policy_number"]: policy for policy in policies}
    for claim in claims:
        holder_home = policy_of[claim["policy_number"]]["insured_object"]
        if holder_home.get("building_type") == "apartment":
            assert not OWN_ROOF_OR_FLOOR.search(claim["description"]), claim["claim_id"]
