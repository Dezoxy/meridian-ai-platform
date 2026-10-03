"""The evaluation report: its shape, its canonical file and its loader."""

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from meridian.platform.evaluation import report as report_module
from meridian.platform.evaluation.report import (
    REPORT_FORMAT,
    AnsweredBy,
    Report,
    ReportError,
    dump_report,
    load_report,
    write_report,
)

DIGEST = "ab" * 32
MARKER = "SECRET-MARKER-9f3a"


def report_data(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "format": REPORT_FORMAT,
        "workload": "demo-workload",
        "answered_by": {"kind": "scripted", "label": "simulated"},
        "fingerprints": {
            "prompt": DIGEST,
            "tools": DIGEST,
            "golden_set": {
                "generator_version": "1",
                "seed": 7,
                "files": {"a.json": DIGEST},
            },
        },
        "absolute": ["alpha"],
        "targets": {"beta": 0.5},
        "cases": [
            {
                "case": "c-1",
                "grades": {"alpha": True, "beta": True},
                "observed": {"alpha": "x", "beta": 3, "gamma": None},
            },
            {
                "case": "c-2",
                "grades": {"alpha": True, "beta": False},
                "observed": {},
            },
        ],
    }
    data.update(overrides)
    return data


def write_data(path: Path, data: dict[str, Any]) -> Path:
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_a_report_survives_dump_and_load_byte_for_byte(tmp_path: Path) -> None:
    report = Report.model_validate(report_data())
    path = tmp_path / "report.json"

    write_report(report, path)
    loaded = load_report(path)

    assert loaded == report
    assert dump_report(loaded) == path.read_text(encoding="utf-8")
    assert path.read_bytes() == dump_report(report).encode("utf-8")


def test_the_dump_is_sorted_indented_and_ends_with_a_newline() -> None:
    text = dump_report(Report.model_validate(report_data()))

    assert text.endswith("}\n")
    assert text.startswith('{\n  "absolute": [')
    assert dump_report(Report.model_validate(report_data())) == text


def test_the_dump_keeps_non_ascii_text_as_it_is() -> None:
    data = report_data()
    data["cases"][0]["observed"] = {"alpha": "café"}

    text = dump_report(Report.model_validate(data))

    assert "café" in text


def test_write_report_creates_no_directory(tmp_path: Path) -> None:
    report = Report.model_validate(report_data())

    with pytest.raises(FileNotFoundError):
        write_report(report, tmp_path / "missing" / "report.json")

    assert not (tmp_path / "missing").exists()


def reversed_cases() -> dict[str, Any]:
    return report_data(cases=list(reversed(report_data()["cases"])))


def duplicate_cases() -> dict[str, Any]:
    cases = report_data()["cases"]
    return report_data(cases=[cases[0], cases[0]])


def unequal_graders() -> dict[str, Any]:
    cases = report_data()["cases"]
    cases[1]["grades"] = {"alpha": True}
    return report_data(cases=cases)


def a_grader_is_missing_from_the_cases() -> dict[str, Any]:
    return report_data(absolute=["alpha", "nothing"])


def a_target_names_no_grader() -> dict[str, Any]:
    return report_data(targets={"nothing": 0.5})


def duplicate_absolute() -> dict[str, Any]:
    return report_data(absolute=["alpha", "alpha"])


def zero_target() -> dict[str, Any]:
    return report_data(targets={"beta": 0})


def target_above_one() -> dict[str, Any]:
    return report_data(targets={"beta": 1.01})


def live_but_simulated() -> dict[str, Any]:
    return report_data(answered_by={"kind": "live", "label": "simulated"})


def scripted_but_real() -> dict[str, Any]:
    return report_data(answered_by={"kind": "scripted", "label": "real"})


def replay_but_real() -> dict[str, Any]:
    return report_data(answered_by={"kind": "replay", "label": "real"})


def an_extra_key() -> dict[str, Any]:
    return report_data(surprise=MARKER)


def an_extra_nested_key() -> dict[str, Any]:
    data = report_data()
    data["fingerprints"]["golden_set"]["surprise"] = 1
    return data


def a_bad_grader_name() -> dict[str, Any]:
    return report_data(absolute=["Alpha"])


def a_bad_case_id() -> dict[str, Any]:
    data = report_data()
    data["cases"][0]["case"] = "-c-1"
    return data


def a_grade_that_is_not_a_boolean() -> dict[str, Any]:
    data = report_data()
    data["cases"][0]["grades"]["alpha"] = 1
    return data


def a_bad_digest() -> dict[str, Any]:
    data = report_data()
    data["fingerprints"]["prompt"] = "ABC"
    return data


def no_golden_files() -> dict[str, Any]:
    data = report_data()
    data["fingerprints"]["golden_set"]["files"] = {}
    return data


def no_cases() -> dict[str, Any]:
    return report_data(cases=[])


def another_format() -> dict[str, Any]:
    return report_data(format=2)


def a_bad_workload() -> dict[str, Any]:
    return report_data(workload="Bad Workload")


def a_case_without_grades() -> dict[str, Any]:
    return report_data(
        absolute=[],
        targets={},
        cases=[{"case": "c-1", "grades": {}, "observed": {}}],
    )


INVALID = [
    pytest.param(reversed_cases, id="unsorted-cases"),
    pytest.param(duplicate_cases, id="duplicate-case-ids"),
    pytest.param(unequal_graders, id="unequal-grader-sets"),
    pytest.param(a_grader_is_missing_from_the_cases, id="absolute-names-no-grader"),
    pytest.param(a_target_names_no_grader, id="target-names-no-grader"),
    pytest.param(duplicate_absolute, id="duplicate-absolute"),
    pytest.param(zero_target, id="target-zero"),
    pytest.param(target_above_one, id="target-above-one"),
    pytest.param(live_but_simulated, id="live-labelled-simulated"),
    pytest.param(scripted_but_real, id="scripted-labelled-real"),
    pytest.param(replay_but_real, id="replay-labelled-real"),
    pytest.param(an_extra_key, id="extra-key"),
    pytest.param(an_extra_nested_key, id="extra-nested-key"),
    pytest.param(a_bad_grader_name, id="bad-grader-name"),
    pytest.param(a_bad_case_id, id="bad-case-id"),
    pytest.param(a_grade_that_is_not_a_boolean, id="grade-not-boolean"),
    pytest.param(a_bad_digest, id="bad-digest"),
    pytest.param(no_golden_files, id="no-golden-files"),
    pytest.param(no_cases, id="no-cases"),
    pytest.param(another_format, id="another-format"),
    pytest.param(a_bad_workload, id="bad-workload"),
    pytest.param(a_case_without_grades, id="case-without-grades"),
]


@pytest.mark.parametrize("make", INVALID)
def test_the_model_refuses_an_invalid_report(make: Any) -> None:
    with pytest.raises(ValidationError):
        Report.model_validate(make())


@pytest.mark.parametrize("make", INVALID)
def test_load_report_refuses_an_invalid_file_with_a_report_error(
    make: Any, tmp_path: Path
) -> None:
    path = write_data(tmp_path / "report.json", make())

    with pytest.raises(ReportError):
        load_report(path)


def test_a_recorded_run_may_be_labelled_either_way() -> None:
    for label in ("simulated", "real"):
        answered_by = AnsweredBy(kind="recorded", label=label)
        assert answered_by.label == label


def test_the_model_is_immutable() -> None:
    report = Report.model_validate(report_data())

    with pytest.raises(ValidationError):
        report.workload = "other"  # type: ignore[misc]


def test_load_report_names_a_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ReportError, match="not found"):
        load_report(tmp_path / "nope.json")


def test_load_report_refuses_a_file_over_the_limit_before_parsing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(report_module, "MAX_REPORT_BYTES", 100)
    path = write_data(tmp_path / "report.json", report_data())
    assert path.stat().st_size > 100

    with pytest.raises(ReportError, match="too large"):
        load_report(path)


def test_load_report_accepts_a_file_at_the_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "report.json"
    write_report(Report.model_validate(report_data()), path)
    monkeypatch.setattr(report_module, "MAX_REPORT_BYTES", path.stat().st_size)

    assert load_report(path).workload == "demo-workload"


def test_the_limit_is_five_mebibytes() -> None:
    assert report_module.MAX_REPORT_BYTES == 5 * 1024 * 1024


def test_load_report_refuses_invalid_json(tmp_path: Path) -> None:
    path = tmp_path / "report.json"
    path.write_text("{not json", encoding="utf-8")

    with pytest.raises(ReportError, match="json_invalid"):
        load_report(path)


def test_load_report_refuses_a_file_that_is_not_utf8(tmp_path: Path) -> None:
    path = tmp_path / "report.json"
    path.write_bytes(b'{"workload": "\xff\xfe"}')

    with pytest.raises(ReportError, match="UTF-8"):
        load_report(path)


def test_load_report_refuses_a_directory(tmp_path: Path) -> None:
    with pytest.raises(ReportError):
        load_report(tmp_path)


def test_a_validation_error_names_the_field_and_not_the_planted_value(
    tmp_path: Path,
) -> None:
    data = report_data(workload=MARKER)
    data["cases"][0]["grades"]["alpha"] = MARKER
    data["cases"][0]["observed"] = {"alpha": [MARKER]}
    data["surprise"] = MARKER
    path = write_data(tmp_path / "report.json", data)

    with pytest.raises(ReportError) as raised:
        load_report(path)

    message = str(raised.value)
    assert MARKER not in message
    assert "workload" in message
    assert "string_pattern_mismatch" in message


def test_a_validator_error_carries_only_its_own_fixed_sentence(
    tmp_path: Path,
) -> None:
    data = reversed_cases()
    data["cases"][0]["observed"] = {"alpha": MARKER}
    path = write_data(tmp_path / "report.json", data)

    with pytest.raises(ReportError) as raised:
        load_report(path)

    assert "sorted" in str(raised.value)
    assert MARKER not in str(raised.value)


def test_a_file_full_of_errors_gets_a_short_message(tmp_path: Path) -> None:
    data = report_data()
    data["cases"] = [
        {"case": f"c-{n:03}", "grades": {"alpha": MARKER}, "observed": {}}
        for n in range(200)
    ]
    path = write_data(tmp_path / "report.json", data)

    with pytest.raises(ReportError) as raised:
        load_report(path)

    assert len(str(raised.value)) < 1000
    assert MARKER not in str(raised.value)
