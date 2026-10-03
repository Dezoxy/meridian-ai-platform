"""The evaluation report: its shape, its canonical file and its loader."""

import json
import os
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
    read_json_file,
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
                "manifest": DIGEST,
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

    with pytest.raises(ReportError, match="not valid JSON"):
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


# ── duplicate keys ──────────────────────────────────────────────────────────
def with_a_duplicate(old: str, new: str) -> str:
    text = json.dumps(report_data())
    assert old in text
    return text.replace(old, new, 1)


DUPLICATES = [
    pytest.param('{"format": 1,', '{"format": 1, "format": 1,', id="top-level"),
    pytest.param(
        '"grades": {"alpha": true,',
        '"grades": {"alpha": true, "alpha": false,',
        id="in-a-cases-grades",
    ),
    pytest.param(
        '"targets": {"beta": 0.5}',
        '"targets": {"beta": 0.5, "beta": 0.6}',
        id="in-targets",
    ),
    pytest.param('"seed": 7,', '"seed": 7, "seed": 8,', id="in-the-fingerprints"),
]


@pytest.mark.parametrize(("old", "new"), DUPLICATES)
def test_load_report_refuses_a_duplicate_key_at_any_depth(
    old: str, new: str, tmp_path: Path
) -> None:
    path = tmp_path / "report.json"
    path.write_text(with_a_duplicate(old, new), encoding="utf-8")

    with pytest.raises(ReportError) as raised:
        load_report(path)

    assert str(raised.value) == "duplicate key"


def test_read_json_file_refuses_a_duplicate_key_without_naming_it(
    tmp_path: Path,
) -> None:
    path = tmp_path / "any.json"
    path.write_text('{"a": {"SECRET-KEY": 1, "SECRET-KEY": 2}}', encoding="utf-8")

    with pytest.raises(ReportError) as raised:
        read_json_file(path)

    assert str(raised.value) == "duplicate key"


def test_read_json_file_parses_a_valid_document(tmp_path: Path) -> None:
    path = tmp_path / "any.json"
    path.write_text('{"a": [1, {"b": null}]}', encoding="utf-8")

    assert read_json_file(path) == {"a": [1, {"b": None}]}


@pytest.mark.parametrize(
    "text", ["{not json", "", '{"a": ' + "[" * 100_000, "1" * 5000]
)
def test_read_json_file_maps_every_parse_failure_to_a_report_error(
    text: str, tmp_path: Path
) -> None:
    path = tmp_path / "any.json"
    path.write_text(text, encoding="utf-8")

    with pytest.raises(ReportError) as raised:
        read_json_file(path)

    assert "{not" not in str(raised.value)
    assert len(str(raised.value)) < 100


def test_read_json_file_names_a_missing_file_and_a_non_utf8_file(
    tmp_path: Path,
) -> None:
    bad = tmp_path / "bad.json"
    bad.write_bytes(b'{"a": "\xff"}')

    with pytest.raises(ReportError, match="not found"):
        read_json_file(tmp_path / "nope.json")
    with pytest.raises(ReportError, match="UTF-8"):
        read_json_file(bad)


# ── error text never echoes the file's own keys ─────────────────────────────
EVIL = "EVIL\n::error::pwned\x1b[31m"


def assert_clean(message: str) -> None:
    assert "\n" not in message
    assert "\r" not in message
    assert "::" not in message
    assert "\x1b" not in message
    assert "EVIL" not in message


def an_evil_top_level_key() -> dict[str, Any]:
    return report_data(**{EVIL: 1})


def an_evil_key_in_grades() -> dict[str, Any]:
    data = report_data()
    data["cases"][0]["grades"] = {EVIL: True}
    return data


def an_evil_key_in_the_golden_files() -> dict[str, Any]:
    data = report_data()
    data["fingerprints"]["golden_set"]["files"] = {EVIL: "xyz"}
    return data


def an_evil_key_in_targets() -> dict[str, Any]:
    return report_data(targets={EVIL: 0.5})


@pytest.mark.parametrize(
    "make",
    [
        pytest.param(an_evil_top_level_key, id="top-level-extra-key"),
        pytest.param(an_evil_key_in_grades, id="grades-key"),
        pytest.param(an_evil_key_in_the_golden_files, id="golden-set-files-key"),
        pytest.param(an_evil_key_in_targets, id="targets-key"),
    ],
)
def test_the_error_text_carries_none_of_the_files_keys(
    make: Any, tmp_path: Path
) -> None:
    path = write_data(tmp_path / "report.json", make())

    with pytest.raises(ReportError) as raised:
        load_report(path)

    assert_clean(str(raised.value))
    assert str(raised.value)


def test_the_error_text_still_names_this_packages_own_fields(tmp_path: Path) -> None:
    path = write_data(tmp_path / "report.json", report_data(workload="Bad Workload"))

    with pytest.raises(ReportError) as raised:
        load_report(path)

    assert str(raised.value) == "workload: string_pattern_mismatch"


def test_a_key_part_is_kept_up_to_forty_characters_and_hidden_beyond(
    tmp_path: Path,
) -> None:
    kept = "k" * 40
    hidden = "h" * 41
    path = write_data(tmp_path / "report.json", report_data(**{kept: 1, hidden: 1}))

    with pytest.raises(ReportError) as raised:
        load_report(path)

    assert f"{kept}: extra_forbidden" in str(raised.value)
    assert hidden not in str(raised.value)
    assert "?: extra_forbidden" in str(raised.value)


def test_a_list_position_stays_a_number(tmp_path: Path) -> None:
    data = report_data()
    data["cases"][1]["case"] = "-c-2"
    path = write_data(tmp_path / "report.json", data)

    with pytest.raises(ReportError, match=r"^cases\.1\.case: "):
        load_report(path)


# ── a canonical absolute ────────────────────────────────────────────────────
def test_absolute_must_be_sorted() -> None:
    data = report_data(absolute=["beta", "alpha"])

    with pytest.raises(ValidationError, match="sorted and unique"):
        Report.model_validate(data)


def test_a_duplicate_absolute_says_it_is_not_unique() -> None:
    with pytest.raises(ValidationError, match="sorted and unique"):
        Report.model_validate(duplicate_absolute())


def test_a_sorted_absolute_is_accepted_and_dumped_as_it_is() -> None:
    report = Report.model_validate(report_data(absolute=["alpha", "beta"]))

    assert report.absolute == ("alpha", "beta")
    assert json.loads(dump_report(report))["absolute"] == ["alpha", "beta"]


# ── strict numbers ──────────────────────────────────────────────────────────
@pytest.mark.parametrize("value", [True, "0.5", None, [0.5]])
def test_a_target_must_be_a_number(value: Any, tmp_path: Path) -> None:
    path = write_data(tmp_path / "report.json", report_data(targets={"beta": value}))

    with pytest.raises(ReportError):
        load_report(path)


def test_an_integer_target_of_one_is_read_as_the_float_one(tmp_path: Path) -> None:
    path = write_data(tmp_path / "report.json", report_data(targets={"beta": 1}))

    report = load_report(path)

    assert report.targets == {"beta": 1.0}
    assert isinstance(report.targets["beta"], float)
    assert '"beta": 1.0' in dump_report(report)


@pytest.mark.parametrize("value", [True, 1.0, "1"])
def test_the_format_must_be_the_integer_one(value: Any, tmp_path: Path) -> None:
    path = write_data(tmp_path / "report.json", report_data(format=value))

    with pytest.raises(ReportError):
        load_report(path)


# ── the bounded read ────────────────────────────────────────────────────────
@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs named pipes")
def test_load_report_refuses_a_named_pipe_instead_of_reading_it(
    tmp_path: Path,
) -> None:
    pipe = tmp_path / "report.json"
    os.mkfifo(pipe)

    with pytest.raises(ReportError, match="not a regular file"):
        load_report(pipe)


def test_the_limit_holds_without_trusting_the_reported_size(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = write_data(tmp_path / "report.json", report_data())
    monkeypatch.setattr(report_module, "MAX_REPORT_BYTES", 100)

    def stat_claiming_an_empty_regular_file(self: Path, **_: Any) -> os.stat_result:
        return os.stat_result((0o100644,) + (0,) * 9)

    monkeypatch.setattr(Path, "stat", stat_claiming_an_empty_regular_file)

    with pytest.raises(ReportError, match="too large"):
        load_report(path)


# ── an atomic write ─────────────────────────────────────────────────────────
def test_write_report_replaces_a_temporary_file_from_the_same_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[Path, Path]] = []
    real_replace = os.replace

    def spy(source: Any, target: Any) -> None:
        calls.append((Path(source), Path(target)))
        real_replace(source, target)

    monkeypatch.setattr(os, "replace", spy)
    path = tmp_path / "report.json"

    write_report(Report.model_validate(report_data()), path)

    assert len(calls) == 1
    source, target = calls[0]
    assert target == path
    assert source.parent == path.parent
    assert source != path
    assert [p.name for p in tmp_path.iterdir()] == ["report.json"]


def test_a_failed_write_leaves_the_old_report_and_no_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "report.json"
    path.write_text("old report\n", encoding="utf-8")

    def refuse(source: Any, target: Any) -> None:
        raise OSError("disk says no")

    monkeypatch.setattr(os, "replace", refuse)

    with pytest.raises(OSError, match="disk says no"):
        write_report(Report.model_validate(report_data()), path)

    assert path.read_text(encoding="utf-8") == "old report\n"
    assert [p.name for p in tmp_path.iterdir()] == ["report.json"]


def test_write_report_writes_utf8_with_unix_newlines(tmp_path: Path) -> None:
    data = report_data()
    data["cases"][0]["observed"] = {"alpha": "café"}
    path = tmp_path / "report.json"

    write_report(Report.model_validate(data), path)

    raw = path.read_bytes()
    assert "café".encode() in raw
    assert b"\r" not in raw
    assert raw.endswith(b"}\n")
