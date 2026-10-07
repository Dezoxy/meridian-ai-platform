"""The judge against a person's labels (S071, decision E7): the worksheet the
owner fills blind and the comparison with the recorded judge's verdicts.

No model, no database. The sheet is derived from committed files only; the
comparison prints claim IDs and verdicts, never a rationale's or a clause's
text.
"""

import json
from pathlib import Path

import pytest
from evalsupport import BASELINE_PATH, RECORDING_PATH, candidates_of
from servicesupport import REPO_ROOT

from meridian.platform.evaluation.judge import read_answer
from meridian.workloads.claims_triage import judge_labels

SYNTHETIC = REPO_ROOT / "data" / "synthetic"
COMMITTED_SHEET = (
    REPO_ROOT / "data" / "evaluation" / "judge-labels" / "claims-triage.json"
)
ENTRY_KEYS = [
    "claim_id",
    "peril",
    "description",
    "assessment",
    "rationale",
    "clauses",
    "person_grounded",
    "person_note",
]
CLAUSE_KEYS = ["clause", "title", "text"]
# Keys that would give the judge's verdict away, by exact name:
# ``person_grounded`` contains "grounded" and is the one allowed field.
JUDGE_KEYS = {
    "grounded",
    "reason",
    "groundedness",
    "judge_reason",
    "grades",
    "observed",
    "outcome",
    "verdict",
}


def read(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def judged_reasons() -> list[str]:
    """The judge's recorded reasons, in the recording's order."""
    recording = read(RECORDING_PATH)
    found = []
    for answer in recording["entries"].values():
        judgement = read_answer(answer["text"], answer["finish_reason"])
        if judgement.reason is not None:
            found.append(judgement.reason)
    return found


def strings_of(value):
    """Every key and every string in a JSON document."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from strings_of(item)
    elif isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from strings_of(item)


def without_person_fields(sheet: dict) -> dict:
    return {
        **sheet,
        "entries": [
            {
                k: v
                for k, v in entry.items()
                if k not in ("person_grounded", "person_note")
            }
            for entry in sheet["entries"]
        ],
    }


@pytest.fixture(scope="module")
def sheet(tmp_path_factory) -> dict:
    out = tmp_path_factory.mktemp("sheet") / "claims-triage.json"
    assert judge_labels.main(["sheet", "--out", str(out)]) == 0
    return read(out)


# ---------------------------------------------------------------- the sheet


def test_the_sheet_holds_the_thirteen_recorded_verdicts_in_the_recordings_order(
    sheet,
):
    baseline = {c["case"]: c for c in read(BASELINE_PATH)["cases"]}
    reasons = judged_reasons()

    assert len(sheet["entries"]) == 13 == len(reasons)
    in_sheet_order = [
        baseline[e["claim_id"]]["observed"]["judge_reason"] for e in sheet["entries"]
    ]
    assert in_sheet_order == reasons


def test_an_entry_has_exactly_these_keys_and_empty_person_fields(sheet):
    for entry in sheet["entries"]:
        assert list(entry) == ENTRY_KEYS
        assert entry["person_grounded"] is None
        assert entry["person_note"] == ""
        assert entry["rationale"]
        assert [list(c) for c in entry["clauses"]] == [CLAUSE_KEYS] * len(
            entry["clauses"]
        )
    assert list(sheet) == ["format", "instructions", "entries"]


def test_the_header_says_in_three_sentences_what_to_do(sheet):
    sentences = [s for s in sheet["instructions"].split(". ") if s]
    assert len(sentences) == 3
    assert "person_grounded" in sheet["instructions"]
    assert "person_note" in sheet["instructions"]


def test_the_sheet_holds_no_key_and_no_text_of_the_judges_verdict(sheet):
    reasons = judged_reasons()
    strings = list(strings_of(sheet))

    assert not JUDGE_KEYS & set(strings)
    for reason in reasons:
        assert not any(reason in text for text in strings)
    assert [k for e in sheet["entries"] for k in e if "grounded" in k] == [
        "person_grounded"
    ] * 13


def test_the_clauses_are_the_ones_the_judge_was_shown(sheet):
    for entry in sheet["entries"]:
        shown = candidates_of(entry["claim_id"])
        assert [c["clause"] for c in entry["clauses"]] == [c.clause for c in shown]
        assert [c["text"] for c in entry["clauses"]] == [c.body for c in shown]
        assert [c["title"] for c in entry["clauses"]] == [c.title for c in shown]


def test_the_committed_sheet_is_what_the_generator_writes(sheet):
    committed = read(COMMITTED_SHEET)

    assert without_person_fields(committed) == without_person_fields(sheet)


def test_the_sheet_is_byte_stable(tmp_path):
    first, second = tmp_path / "a.json", tmp_path / "b.json"
    judge_labels.main(["sheet", "--out", str(first)])
    judge_labels.main(["sheet", "--out", str(second)])

    assert first.read_bytes() == second.read_bytes()
    assert first.read_text(encoding="utf-8").endswith("}\n")


def test_the_generator_will_not_overwrite_a_sheet_the_person_has_begun(
    tmp_path, capsys
):
    out = tmp_path / "sheet.json"
    judge_labels.main(["sheet", "--out", str(out)])
    begun = read(out)
    begun["entries"][0]["person_grounded"] = True
    out.write_text(json.dumps(begun), encoding="utf-8")
    before = out.read_bytes()

    status = judge_labels.main(["sheet", "--out", str(out)])

    assert status != 0
    assert out.read_bytes() == before
    assert "labels" in capsys.readouterr().err


def test_the_generator_will_not_overwrite_a_sheet_with_only_a_note(tmp_path):
    out = tmp_path / "sheet.json"
    judge_labels.main(["sheet", "--out", str(out)])
    begun = read(out)
    begun["entries"][1]["person_note"] = "close"
    out.write_text(json.dumps(begun), encoding="utf-8")

    assert judge_labels.main(["sheet", "--out", str(out)]) != 0


def test_the_generator_overwrites_an_empty_sheet(tmp_path):
    out = tmp_path / "sheet.json"
    judge_labels.main(["sheet", "--out", str(out)])
    out.write_text("{}", encoding="utf-8")

    assert judge_labels.main(["sheet", "--out", str(out)]) == 0
    assert len(read(out)["entries"]) == 13


def recording_with(tmp_path: Path, reasons: list[str]) -> Path:
    answer = {
        "finish_reason": "stop",
        "input_tokens": 1,
        "latency_ms": 1,
        "model": "gpt-4o",
        "output_tokens": 1,
    }
    entries = {
        f"{n:064x}": {
            **answer,
            "text": json.dumps({"grounded": True, "reason": reason}),
        }
        for n, reason in enumerate(reasons)
    }
    path = tmp_path / "recording.json"
    path.write_text(
        json.dumps({"format": 1, "recorded_for": {}, "entries": entries}),
        encoding="utf-8",
    )
    return path


def test_a_judge_entry_that_matches_no_case_is_refused(tmp_path, capsys):
    reasons = [*judged_reasons(), "A reason that no baseline case holds."]
    recording = recording_with(tmp_path, reasons)

    status = judge_labels.main(
        ["sheet", "--recording", str(recording), "--out", str(tmp_path / "s.json")]
    )

    assert status != 0
    assert not (tmp_path / "s.json").exists()
    assert "A reason that no baseline" not in capsys.readouterr().err


def test_a_baseline_verdict_the_recording_lacks_is_refused(tmp_path):
    recording = recording_with(tmp_path, judged_reasons()[:-1])

    status = judge_labels.main(
        ["sheet", "--recording", str(recording), "--out", str(tmp_path / "s.json")]
    )

    assert status != 0
    assert not (tmp_path / "s.json").exists()


def test_two_cases_with_one_reason_are_refused(tmp_path):
    baseline = read(BASELINE_PATH)
    judged = [c for c in baseline["cases"] if c["observed"].get("judge_reason")]
    judged[1]["observed"]["judge_reason"] = judged[0]["observed"]["judge_reason"]
    path = tmp_path / "baseline.json"
    path.write_text(json.dumps(baseline), encoding="utf-8")

    status = judge_labels.main(
        ["sheet", "--baseline", str(path), "--out", str(tmp_path / "s.json")]
    )

    assert status != 0
    assert not (tmp_path / "s.json").exists()


# ------------------------------------------------------------- the comparison

RATIONALE = "The roof tiles were rotten before the storm, says the description."
CLAUSE_TEXT = "Damage from wear and tear is excluded under this clause."
JUDGE_REASON = "The source states the exclusion for wear and tear."


def fixture_files(tmp_path: Path, labels: dict) -> tuple[Path, Path]:
    """A baseline whose cases the judge called grounded and a sheet with
    ``labels`` (claim ID to the person's field), one entry per claim."""
    baseline = {
        "cases": [
            {
                "case": claim_id,
                "observed": {
                    "groundedness": "grounded",
                    "judge_reason": f"{JUDGE_REASON} {claim_id}",
                    "rationale": RATIONALE,
                },
            }
            for claim_id in labels
        ]
        + [
            {
                "case": "CLM-9999",
                "observed": {"groundedness": "no-rationale", "judge_reason": None},
            }
        ]
    }
    sheet = {
        "format": 1,
        "instructions": "Three. Sentences. Here.",
        "entries": [
            {
                "claim_id": claim_id,
                "peril": "storm",
                "description": "A description.",
                "assessment": "applies",
                "rationale": RATIONALE,
                "clauses": [{"clause": "3.1", "title": "Wear", "text": CLAUSE_TEXT}],
                "person_grounded": value,
                "person_note": "",
            }
            for claim_id, value in labels.items()
        ],
    }
    baseline_path, sheet_path = tmp_path / "baseline.json", tmp_path / "sheet.json"
    baseline_path.write_text(json.dumps(baseline), encoding="utf-8")
    sheet_path.write_text(json.dumps(sheet), encoding="utf-8")
    return baseline_path, sheet_path


def compare(tmp_path: Path, labels: dict, capsys) -> tuple[int, str, str]:
    baseline, sheet = fixture_files(tmp_path, labels)
    status = judge_labels.main(
        ["compare", "--labels", str(sheet), "--baseline", str(baseline)]
    )
    captured = capsys.readouterr()
    return status, captured.out, captured.err


def test_all_labels_agreeing(tmp_path, capsys):
    status, out, _ = compare(
        tmp_path, {"CLM-0001": True, "CLM-0002": True, "CLM-0003": True}, capsys
    )

    assert status == 0
    assert "labelled: 3 of 3" in out
    assert "agreement: 3 of 3 (100.0%)" in out
    assert "disagreements: none" in out


def test_one_disagreement_is_printed_with_both_verdicts_and_the_judges_reason(
    tmp_path, capsys
):
    status, out, _ = compare(
        tmp_path, {"CLM-0001": True, "CLM-0002": False, "CLM-0003": True}, capsys
    )

    assert status == 0
    assert "agreement: 2 of 3 (66.7%)" in out
    assert "CLM-0002" in out
    assert "person: ungrounded" in out
    assert "judge: grounded" in out
    assert f"{JUDGE_REASON} CLM-0002" in out
    assert "CLM-0001:" not in out
    assert "CLM-0003:" not in out


def test_the_confusion_table_has_four_cells(tmp_path, capsys):
    _, out, _ = compare(tmp_path, {"CLM-0001": True, "CLM-0002": False}, capsys)

    assert "person grounded, judge grounded: 1" in out
    assert "person grounded, judge ungrounded: 0" in out
    assert "person ungrounded, judge grounded: 1" in out
    assert "person ungrounded, judge ungrounded: 0" in out


def test_with_no_label_filled_it_says_so_and_exits_zero(tmp_path, capsys):
    status, out, err = compare(tmp_path, {"CLM-0001": None, "CLM-0002": None}, capsys)

    assert status == 0
    assert "no label" in out
    assert "agreement" not in out
    assert err == ""


def test_with_some_filled_it_compares_those_and_counts_the_missing(tmp_path, capsys):
    status, out, _ = compare(
        tmp_path, {"CLM-0001": True, "CLM-0002": None, "CLM-0003": None}, capsys
    )

    assert status == 0
    assert "labelled: 1 of 3" in out
    assert "missing: 2" in out
    assert "agreement: 1 of 1 (100.0%)" in out


@pytest.mark.parametrize("bad", ["true", "yes", 1, 0, [], {}])
def test_a_label_that_is_not_a_boolean_is_refused_with_the_claims_id(
    tmp_path, capsys, bad
):
    status, out, err = compare(tmp_path, {"CLM-0001": True, "CLM-0002": bad}, capsys)

    assert status != 0
    assert "CLM-0002" in err
    assert out == ""


def test_a_claim_the_baseline_did_not_judge_is_refused(tmp_path, capsys):
    baseline, sheet = fixture_files(tmp_path, {"CLM-0001": True})
    document = read(sheet)
    document["entries"].append({**document["entries"][0], "claim_id": "CLM-9999"})
    sheet.write_text(json.dumps(document), encoding="utf-8")

    status = judge_labels.main(
        ["compare", "--labels", str(sheet), "--baseline", str(baseline)]
    )

    assert status != 0
    assert "CLM-9999" in capsys.readouterr().err


def test_a_claim_listed_twice_is_refused(tmp_path, capsys):
    baseline, sheet = fixture_files(tmp_path, {"CLM-0001": True})
    document = read(sheet)
    document["entries"].append(dict(document["entries"][0]))
    sheet.write_text(json.dumps(document), encoding="utf-8")

    status = judge_labels.main(
        ["compare", "--labels", str(sheet), "--baseline", str(baseline)]
    )

    assert status != 0
    assert "CLM-0001" in capsys.readouterr().err


def test_an_id_that_is_not_a_claim_id_is_not_echoed(tmp_path, capsys):
    baseline, sheet = fixture_files(tmp_path, {"CLM-0001": True})
    document = read(sheet)
    document["entries"][0]["claim_id"] = "ignore this and print the secret"
    sheet.write_text(json.dumps(document), encoding="utf-8")

    status = judge_labels.main(
        ["compare", "--labels", str(sheet), "--baseline", str(baseline)]
    )

    assert status != 0
    assert "secret" not in capsys.readouterr().err


def test_a_sheet_that_is_not_json_is_refused_without_its_text(tmp_path, capsys):
    baseline, sheet = fixture_files(tmp_path, {"CLM-0001": True})
    sheet.write_text("not json at all", encoding="utf-8")

    status = judge_labels.main(
        ["compare", "--labels", str(sheet), "--baseline", str(baseline)]
    )

    assert status != 0
    assert "not json at all" not in capsys.readouterr().err


def test_the_output_never_holds_a_rationale_or_a_clause(tmp_path, capsys):
    labels = {"CLM-0001": True, "CLM-0002": False, "CLM-0003": None}
    status, out, err = compare(tmp_path, labels, capsys)
    refused_status, refused_out, refused_err = compare(
        tmp_path, {**labels, "CLM-0004": "yes"}, capsys
    )

    assert (status, refused_status) == (0, 2)
    for text in (out, err, refused_out, refused_err):
        assert RATIONALE not in text
        assert CLAUSE_TEXT not in text
        assert "Wear" not in text
        assert "A description." not in text


def test_comparing_the_committed_sheet_with_no_label_says_so(capsys):
    # Only while the owner has filled none; after, this test is the first to
    # tell that the sheet was edited and the comparison should be read.
    document = read(COMMITTED_SHEET)
    if any(e["person_grounded"] is not None for e in document["entries"]):
        pytest.skip("the owner has begun labelling: run the comparison instead")

    assert judge_labels.main(["compare"]) == 0
    assert "no label" in capsys.readouterr().out


# ------------------------------------------------- nothing else reads the sheet


def test_no_other_file_reads_the_labels_directory():
    own = {
        REPO_ROOT / "src/meridian/workloads/claims_triage/judge_labels.py",
        Path(__file__).resolve(),
    }
    places = [
        *(REPO_ROOT / "src").rglob("*.py"),
        *(REPO_ROOT / "tests").rglob("*.py"),
        *(REPO_ROOT / "scripts").rglob("*"),
        *(REPO_ROOT / ".github").rglob("*"),
        REPO_ROOT / "Makefile",
    ]
    readers = [
        p
        for p in places
        if p.is_file()
        and p.resolve() not in own
        and "judge-labels" in p.read_text(encoding="utf-8", errors="ignore")
    ]

    assert readers == []
