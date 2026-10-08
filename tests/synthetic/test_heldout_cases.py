"""The held-out injection cases: 72 sentences kept as their writer wrote them.

The texts are synthetic attack test data for an evaluation report. Nothing here
acts on them; the tests only count and compare them, and a failure prints an
index, never a sentence.
"""

import hashlib
import json
from pathlib import Path

import pytest
from generator import heldout, heldout_text, injection
from generator.scenarios import build_dataset

FIRST_CASE = 5001
EXPECTED_COUNTS = {
    "cases": 72,
    "attack": 48,
    "benign": 24,
    "description": 72,
    "clause": 0,
}
EXPECTED_FAMILIES = {
    "attack/override": 6,
    "attack/role": 6,
    "attack/role-marker": 6,
    "attack/answer-format": 6,
    "attack/authority": 6,
    "attack/obfuscated": 6,
    "attack/other-language": 6,
    "attack/indirect": 6,
    "benign/look-alike": 24,
}
# SHA-256 of each sentence's UTF-8 text, in the order the writer gave them (the
# handoff file of the blind writer, decoded). An edit of any sentence, a comma
# included, changes its digest and fails the test: a held-out set that was
# repaired is no longer held out.
SENTENCE_SHA256 = (
    "56888cec3ea7a575619ee5aefcbdd2b6403e1e9f2a92e5581dcb5475aeb7d8a2",
    "63f0ae3b1beb2beb6ada041b42acef0a9edb08260badad5790bf499431dae55c",
    "a6dcead523e2ac05c7790577b148ed4323795b79b645e3b80d207d58ec58e305",
    "77ea9568c214b4ee2c0e0fda14406aa0eb80711f6ebefd06bb10ca4f36a5b5c1",
    "cc9d87af8c75ff6c4fc1ec8c564b9418f76f37fb467a7d817be80563a04ad341",
    "b4aaa30c545091f1b8b531a53720870c3db539e8866f3b239821d5f97c7ef5b1",
    "eefb587895bef542afb4a2eb91c0dae48ee243a039023607dee950e5f93f1c74",
    "1d77cfabb243025e647502f397cb381610d06b1eb34be56ffe18f934f00d9b55",
    "56ac274f6f0ae2c7734437b88cab98d4501b3e28c0c57226dcfa8b0c5d1eb029",
    "842a60d9b61a731b89dc2c33985a36e9e65ab292d1117e0f6b2f2034f1c4cc39",
    "3575457eb810fa8dfe77e04c34d591e10e1706e439da1d72d3506084db12cd63",
    "5ceb585bf1dd7b45316c03c497886cd9152b66eccbb0110f9e0d8ba907231b26",
    "04a526dd9f40fdd5c1cd0976184bbf4677e08fea1fa83b52330565084f281b04",
    "8618ecd44f07b98d94074849d7e69deb2964ef0861a75308493aa452ef832236",
    "3692035832e536e0dcfc537b065b3be0b3e9c4d33ab0c5ddf2e25fa51f399b51",
    "85183275f27a3c19d815f8af0d2e70b37e83fa8785676969d1fa6284d986e4f1",
    "2d10f236ad2e7034998bf3642adeae875796d342e6719bd78ec429ffe60e115f",
    "42ad2a680996b63cc3a1fd7be18552e3951a9f7f17e245276f670fab8e869186",
    "21a73323206dac835eb44c871a7e6350588a290c6a4e3dcfa5d809d5ae430551",
    "3b62de338a1654123b4c3a9d8f88d781f7ce3fdf967e6d15970acfb6685b9a17",
    "7b6e2a9e1c8776ac108ebb4bdbc78c62944b5ff1c677df09a39768e4ea8c6ab3",
    "0d44cbfa5d10d077a5e08473694ff1c8bf04af90bf8f35f350dbf1107f097fd8",
    "2111b43cd80050552050f51a833c8e7b3509eb220688760d3df30681b9f16c27",
    "45e78c55058b2eeea262c38fa45eb4d0ca1b1c9320fb4665004a2ddf01d78d4e",
    "32d4a8989d4314a04cadebe8c828274358598f69fb30653a8a4f56b9c54385b3",
    "25ce1dc0c3a20025c76c6571a2a6720da1cb76cc935f9bb24c8fb7b5d7b13cd6",
    "6097ee9ca9f53a9d1e6d7c87b29f23e777aad7a1b7d87b7a3d0607d80aaf10a0",
    "c0f7b33d1ba2e0c46e93820cc8cbcce957a4182d48be598c74c7f3c5ec06f933",
    "0d73fabecfb058992e81de8fae7ca8c02347aee94ddf35cabcae7d4aac58c5c1",
    "4a6dbdca6dc9810b7fd18b89381a2ad759dd08e56e898cd6653afdbe7fe1eb01",
    "0c747e8459e05b586e6e6120a7352ccbe25dcc3c405a3ce00314d94ac941ff31",
    "f03072dc598769d4b10535fdd714d115e60529328fab0499e8579a870584eda7",
    "1a03167dfd6b2235e3a03190753bddc981b7973194c989a8b1dfff00ff498fb8",
    "977df0fd3bef5ce345757e99f71d3405fedb398eda896231a53e41ca9acf66a0",
    "bbe735ec2861b1bc7bfca1fb14e8475983d378f738d755b52b3a1ba855b5bd9a",
    "ce3f9c4c3ccef6822cda1d77dedeeb5e21c3f4e006498197f8dbe910d9379faf",
    "956878e52b23812c524ddc0055a95ecb9af255a4fca803e91c4790c7becb9374",
    "3ae28f95024d0f7318907a245c1b5b6968e7854f813230c1b1123246710528c0",
    "ea80fcaf68b7cb6b3b409b75bceb05c129b6e02956d3b9488d992aa9b28b8509",
    "d2101d4f87f6e72882173f1e89a9a509be172e1350fc45054e47957ca3ac72ae",
    "d4088afb8afd04ff03ab332b1038d4a6fd78a88cb144417c00858b60c6498dd2",
    "c00f5b8d14bfdbfc984c3205588e5a2cccd678e510b3ea527575b820cd2a7851",
    "2120ac85844724a9d80c21c9b2b9818744667136a6290dfdcca80cc73017306d",
    "b20834eb876feaca079e85dd60df94e175a9f117fe0a3048d28c66d540c95138",
    "c24647a12f35e5e65c0b5498c433765a75610919e1d1bf4d21bea7bf4d88f016",
    "4c99476ed4dbd44c41dc75e0c544fd755008378263c83b7d65abf4acc9edad59",
    "31ee7cf0f71c2bfdd6f12a2fcf6bcac40bf553df9326d187861ba19110c09e78",
    "f88c3e92c0b546d093870545883cb966a4de42ae70a21ba45fb3a9be673633fa",
    "db1c5515f985931f05eba57c40d3f204ece52d1ca32652eba526d68044f3b919",
    "8af3531062e825f6695a9afd81223561d160fb6ad8e662d3c0e0deb8dfc7f7e3",
    "f1fa9fc03683ddd5ba0513f988f6af790755d6187cf8e3e0f2b2c9d2d667500e",
    "8811c6e0f41bf22b6b4039ce7926266cf85af54e8f50447ef5a9973bd81a9894",
    "66d4ff887baff8428d0e0d11549ab0f97dcf2f7a6da9ef318606dda8dcbb4bcf",
    "7bf4f4c842f704551e56e7d236ee3b268baa8319d5ab38b768984fbed82fc7c0",
    "f1ec2de3bf8e3a1cca5bdd3f26faa873b3a029e4a80330eb748d9106294f1986",
    "60892cc6c5c52e5da13eef50d4797287c1bccefdb9a46c23b7919ebd0f144574",
    "0d9f069ef952a84bf4ad6d6ca5cb7ce1bb819bd30092f2e4edfe70bf3ecee768",
    "0e1da178228dcc6b25e899b20a25ee34dfde84c0ecb8e531b566e36ee69afdd0",
    "9ce6be3a496491c84e4426c9a8e55abe6050dfa283283532676fbd67cac0b6c3",
    "2cab5b947313458ea9d82e2dd19259b3a67218cf8ec916a82d826a191c5548ff",
    "a9f5fb635f122a6cdc67f3a00d74552d34ca33e24148ab5599f241ad8d10ad7b",
    "04dfad8a7a2df52e044ddb211c6cc912df8a7f89f840bff42808cb27f3f5fbe4",
    "f223944d834cc4b1ecfce93d1faf1ebaf5322bd2edb2ba72e2a04df211b06e91",
    "bd67600aa0b34bf0a360ed9db45e46d8502a89873fd1406eb060abdc00a09b11",
    "1b58bc7ab8dcfefe25d82fde555ffb93a24cae6358f095e486c9a34e95b109a0",
    "f2d1e60c8debae4fab734badd85f2a55fc156d4b00e745d89eafca6f822ffc43",
    "0d18fe06a4c301b7c652dc6ba24cd681ecf4f2541597b19b3ca4fa944ec4e63d",
    "320e19513878b9cbd73bffc6d50a5e0af2665b22e5a891255070ccd595264df4",
    "dfef5d124618ee10eedc86b3fd1f0f8dad49b3250d4eecb1481e3be266f3aec3",
    "fbef289961e07abf12a8035654a00c948f78193c5601abe3496ea686684f3383",
    "fee29515843ac4f6c37db7dd2f7f907be3b1be151aa04720d17eb8b102c2f387",
    "d1b7cdf7f0674fc1567f2bd3bba804f73073484a56b80d9923a996e40d25e361",
)
FOLDER = "injection-heldout"


@pytest.fixture(scope="module")
def heldout_dir(synthetic_dir: Path) -> Path:
    return synthetic_dir / FOLDER


@pytest.fixture(scope="module")
def cases(heldout_dir: Path) -> list[dict]:
    return json.loads((heldout_dir / "cases.json").read_text("ascii"))


@pytest.fixture(scope="module")
def held_out_manifest(heldout_dir: Path) -> dict:
    return json.loads((heldout_dir / "manifest.json").read_text("utf-8"))


@pytest.fixture(scope="module")
def base_descriptions(synthetic_dir: Path) -> dict[str, str]:
    claims = json.loads((synthetic_dir / "claims.json").read_text("utf-8"))
    return {claim["claim_id"]: claim["description"] for claim in claims}


def added_text(case: dict, base_descriptions: dict[str, str]) -> str:
    """The sentence joined to the base description: the join is an append with
    one space, so the added text is what follows the description and a space."""
    description = case["claim"]["description"]
    base = base_descriptions[case["base_claim"]]
    assert description.startswith(base + " "), case["case"]
    return description[len(base) + 1 :]


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_the_cases_file_is_what_the_generator_writes(
    generated: dict[str, bytes], synthetic_dir: Path
):
    # Arrange
    committed = {
        name: (synthetic_dir / FOLDER / name).read_bytes()
        for name in ("cases.json", "manifest.json")
    }

    # Act
    fresh = {name: generated[f"{FOLDER}/{name}"] for name in committed}

    # Assert
    assert fresh == committed


def test_the_generator_changes_no_file_of_the_gate(
    generated: dict[str, bytes], synthetic_dir: Path
):
    # Arrange: every file of the golden set and of the injection set
    existing = [
        path
        for path in generated
        if not path.startswith(f"{FOLDER}/") and (synthetic_dir / path).is_file()
    ]

    # Act
    changed = [
        path
        for path in existing
        if (synthetic_dir / path).read_bytes() != generated[path]
    ]

    # Assert
    assert len(existing) > 10
    assert changed == []


def test_the_sentence_list_has_72_different_digests():
    assert len(SENTENCE_SHA256) == 72
    assert len(set(SENTENCE_SHA256)) == 72


def test_the_text_module_holds_the_72_sentences_unchanged():
    # Act
    digests = tuple(digest(entry.text) for entry in heldout_text.HELDOUT_TEXTS)

    # Assert
    assert digests == SENTENCE_SHA256


def test_every_case_adds_one_of_the_72_sentences_unchanged(
    cases: list[dict], base_descriptions: dict[str, str]
):
    # Act
    digests = [digest(added_text(case, base_descriptions)) for case in cases]

    # Assert: in the writer's order, one case per sentence
    assert digests == list(SENTENCE_SHA256)


def test_the_cases_are_numbered_from_5001_in_the_writers_order(cases: list[dict]):
    assert [case["case"] for case in cases] == [
        f"CLM-{FIRST_CASE + index}" for index in range(72)
    ]
    assert all(case["claim"]["claim_id"] == case["case"] for case in cases)
    assert all(case["carrier"] == "description" for case in cases)
    assert all(case["clause"] is None for case in cases)


def test_the_labels_and_families_are_the_writers(
    cases: list[dict], held_out_manifest: dict
):
    # Arrange
    texts = heldout_text.HELDOUT_TEXTS
    per_case = held_out_manifest["cases"]

    # Assert
    for case, entry in zip(cases, texts, strict=True):
        assert case["label"] == ("attack" if entry.label == "attack" else "benign")
        assert case["family"] == entry.family
        assert per_case[case["case"]] == {
            "h_id": entry.h_id,
            "language": entry.language,
            "family": entry.family,
        }


def test_the_base_claims_rotate_as_the_existing_cases_do(cases: list[dict]):
    # Arrange: attacks over the six attack bases, shifted by one after each round
    # of six; look-alikes over the six benign bases in turn
    attack = [case["base_claim"] for case in cases if case["label"] == "attack"]
    benign = [case["base_claim"] for case in cases if case["label"] == "benign"]
    bases = injection.ATTACK_BASES

    # Assert
    assert attack == [bases[(i + i // 6) % 6] for i in range(48)]
    assert benign == [injection.BENIGN_BASES[i % 6] for i in range(24)]
    for family_start in range(0, 48, 6):
        assert set(attack[family_start : family_start + 6]) == set(bases)


def test_a_case_changes_only_its_description_and_id(
    cases: list[dict], synthetic_dir: Path
):
    # Arrange
    claims = json.loads((synthetic_dir / "claims.json").read_text("utf-8"))
    by_id = {claim["claim_id"]: claim for claim in claims}

    # Assert
    for case in cases:
        base = dict(by_id[case["base_claim"]])
        claim = dict(case["claim"])
        for key in ("claim_id", "description"):
            base.pop(key)
            claim.pop(key)
        assert claim == base, case["case"]


def test_no_case_number_or_base_claim_of_the_other_sets_is_reused(
    cases: list[dict], synthetic_dir: Path
):
    # Arrange
    injection = json.loads(
        (synthetic_dir / "injection" / "cases.json").read_text("ascii")
    )
    claims = json.loads((synthetic_dir / "claims.json").read_text("utf-8"))
    used = {c["case"] for c in injection} | {c["claim_id"] for c in claims}

    # Assert
    assert used.isdisjoint(case["case"] for case in cases)


def test_the_manifest_counts_and_hash_describe_the_file(
    cases: list[dict], held_out_manifest: dict, heldout_dir: Path
):
    # Arrange
    file_hash = hashlib.sha256((heldout_dir / "cases.json").read_bytes()).hexdigest()
    golden = hashlib.sha256(
        (heldout_dir.parent / "manifest.json").read_bytes()
    ).hexdigest()

    # Assert
    assert held_out_manifest["held_out"] is True
    assert held_out_manifest["synthetic"] is True
    assert held_out_manifest["counts"] == EXPECTED_COUNTS
    assert held_out_manifest["families"] == EXPECTED_FAMILIES
    assert held_out_manifest["files"] == {"cases.json": file_hash}
    assert held_out_manifest["golden_set"] == golden
    assert list(held_out_manifest["cases"]) == [case["case"] for case in cases]


def test_the_writers_goal_field_is_in_no_data_file(
    cases: list[dict], held_out_manifest: dict
):
    # The manifest keeps the H-id, the language and the family and nothing else.
    keys = {frozenset(entry) for entry in held_out_manifest["cases"].values()}

    assert keys == {frozenset({"h_id", "language", "family"})}
    assert all("goal" not in case for case in cases)


def test_the_cases_file_is_ascii_only_so_no_look_alike_hides(heldout_dir: Path):
    # Act: decoding as ASCII raises when any other character is in the file
    text = (heldout_dir / "cases.json").read_text("ascii")

    # Assert
    assert text.endswith("\n")


def test_the_builder_is_deterministic():
    # Act
    first = injection.render_cases(heldout.build_cases(build_dataset(20260929)))
    second = injection.render_cases(heldout.build_cases(build_dataset(20260929)))

    # Assert
    assert first == second
