"""Two evaluation reports side by side: the tables, byte for byte, and the cells."""

from typing import Any

import pytest

from meridian.platform.evaluation.diff import diff_reports, render_markdown
from meridian.platform.evaluation.report import Report, ReportError

PROMPT_A = "a1" * 32
PROMPT_B = "b2" * 32
JUDGE = "c3" * 32
RECORDING = "d4" * 32
TOOLS = "e5" * 32
HOSTILE = "\n::error::x"
FORGED = "| eval compare: passed"


def measured(calls: int, latency: int | None, **overrides: Any) -> dict[str, Any]:
    data = {
        "model_calls": calls,
        "input_tokens": calls * 100,
        "output_tokens": calls * 10,
        "cost_micro_eur": calls * 1234,
        "latency_ms": latency,
    }
    return data | overrides


def make_report(
    cases: list[dict[str, Any]] | None = None, **fingerprints: Any
) -> Report:
    if cases is None:
        cases = [
            {"case": "c-1", "grades": {"alpha": True, "beta": True}, "observed": {}},
            {"case": "c-2", "grades": {"alpha": True, "beta": False}, "observed": {}},
            {"case": "c-3", "grades": {"alpha": False, "beta": False}, "observed": {}},
        ]
    other = fingerprints.pop("other", {})
    data: dict[str, Any] = {
        "format": 2,
        "workload": "demo",
        "answered_by": fingerprints.pop(
            "answered_by", {"kind": "recorded", "label": "real"}
        ),
        "fingerprints": {
            "prompt": PROMPT_A,
            "judge": None,
            "recording": RECORDING,
            "tools": TOOLS,
            "golden_set": {
                "generator_version": "1",
                "seed": 7,
                "files": {"a.json": PROMPT_A},
                "manifest": PROMPT_A,
            },
        }
        | fingerprints,
        "absolute": [],
        "targets": {},
        "cases": cases,
    } | other
    return Report.model_validate(data)


def second_cases() -> list[dict[str, Any]]:
    return [
        {"case": "c-1", "grades": {"alpha": True, "beta": False}, "observed": {}},
        {"case": "c-2", "grades": {"alpha": True, "beta": False}, "observed": {}},
        {"case": "c-3", "grades": {"alpha": False, "beta": True}, "observed": {}},
    ]


def with_measured(
    cases: list[dict[str, Any]], values: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    return [
        case | {"measured": value} for case, value in zip(cases, values, strict=True)
    ]


def lines(first: Report, second: Report) -> list[str]:
    return render_markdown(diff_reports(first, second)).splitlines()


def test_two_small_reports_render_byte_for_byte() -> None:
    first_cases = [
        c | {"observed": {"alpha": "x", "gamma": 3}}
        for c in make_report().model_dump(mode="json")["cases"]
    ]
    second = [c | {"observed": {"alpha": "y", "gamma": 3}} for c in second_cases()]
    first_cases = with_measured(
        first_cases, [measured(2, 400), measured(0, None), measured(3, 900)]
    )
    second = with_measured(
        second, [measured(1, 300), measured(1, 500), measured(5, 700)]
    )
    first_report = make_report(first_cases)
    second_report = make_report(second, prompt=PROMPT_B, judge=JUDGE)

    text = render_markdown(diff_reports(first_report, second_report))

    assert text == (
        "# Evaluation: A and B\n"
        "\n"
        "## Reports\n"
        "\n"
        "| report | answered by | prompt | judge | recording | tools | golden set |\n"
        "| --- | --- | --- | --- | --- | --- | --- |\n"
        "| A | recorded (real) | a1a1a1a1a1a1 | none | d4d4d4d4d4d4 | same | same |\n"
        "| B | recorded (real) | b2b2b2b2b2b2 | c3c3c3c3c3c3 | d4d4d4d4d4d4 | same"
        " | same |\n"
        "\n"
        "## Pass rates\n"
        "\n"
        "| grader | A passed | B passed | cases |\n"
        "| --- | --- | --- | --- |\n"
        "| alpha | 2 | 2 | 3 |\n"
        "| beta | 1 | 1 | 3 |\n"
        "\n"
        "## Differences\n"
        "\n"
        "| case | what | A | B |\n"
        "| --- | --- | --- | --- |\n"
        "| c-1 | grade beta | passed | failed |\n"
        "| c-1 | observed alpha | `x` | `y` |\n"
        "| c-2 | observed alpha | `x` | `y` |\n"
        "| c-3 | grade beta | failed | passed |\n"
        "| c-3 | observed alpha | `x` | `y` |\n"
        "\n"
        "## Cost and latency\n"
        "\n"
        "| report | model calls | input tokens | output tokens | cost EUR"
        " | median latency ms | max latency ms |\n"
        "| --- | --- | --- | --- | --- | --- | --- |\n"
        "| A | 5 | 500 | 50 | 0.006170 | 650 | 900 |\n"
        "| B | 7 | 700 | 70 | 0.008638 | 500 | 700 |\n"
    )


def test_identical_reports_say_that_nothing_differs() -> None:
    text = render_markdown(diff_reports(make_report(), make_report()))

    assert "No grade or observed value differs." in text.splitlines()
    assert "| A | recorded (real) | a1a1a1a1a1a1 | none |" in text
    assert "| alpha | 2 | 2 | 3 |" in text
    assert "## Cost and latency" not in text


def test_a_different_tools_or_golden_set_fingerprint_reads_differs() -> None:
    golden_set = make_report().model_dump(mode="json")["fingerprints"]["golden_set"]
    other_golden = golden_set | {"seed": 8}

    rows = lines(
        make_report(),
        make_report(tools=PROMPT_B, golden_set=other_golden),
    )

    reports = [row for row in rows if row.startswith(("| A |", "| B |"))][:2]
    assert all(row.endswith("| differs | differs |") for row in reports)


def test_the_answering_kind_and_label_are_shown_per_report() -> None:
    live = make_report(answered_by={"kind": "live", "label": "real"})
    scripted = make_report(answered_by={"kind": "scripted", "label": "simulated"})

    rows = lines(live, scripted)

    assert any(row.startswith("| A | live (real) |") for row in rows)
    assert any(row.startswith("| B | scripted (simulated) |") for row in rows)


def test_pass_rates_are_sorted_by_grader_name() -> None:
    cases = [
        {"case": "c-1", "grades": {"zeta": True, "alpha": False}, "observed": {}},
    ]

    rows = lines(make_report(cases), make_report(cases))

    assert rows.index("| alpha | 0 | 0 | 1 |") < rows.index("| zeta | 1 | 1 | 1 |")


def test_a_value_absent_on_one_side_is_shown_as_absent_and_null_as_none() -> None:
    first = [{"case": "c-1", "grades": {"a": True}, "observed": {"k": None}}]
    second = [{"case": "c-1", "grades": {"a": True}, "observed": {"z": 5}}]

    rows = lines(make_report(first), make_report(second))

    assert "| c-1 | observed k | none | absent |" in rows
    assert "| c-1 | observed z | absent | `5` |" in rows


@pytest.mark.parametrize(
    ("value", "shown"),
    [
        pytest.param(HOSTILE, "???error??x", id="a-workflow-command-after-a-newline"),
        pytest.param(FORGED, "? eval compare? passed", id="a-forged-result-line"),
        pytest.param("a`b<c>d|e:f", "a?b?c?d?e?f", id="markdown-and-html"),
        pytest.param("tab\there\r\x1b[31m", "tab?here??[31m", id="control-characters"),
        pytest.param("café ☃", "caf? ?", id="non-ascii"),
        pytest.param("x" * 81, "x" * 80, id="cut-at-80"),
        pytest.param("x" * 80, "x" * 80, id="exactly-80"),
        pytest.param("\n" + "y" * 100, "?" + "y" * 79, id="cut-before-replacing"),
    ],
)
def test_an_observed_value_is_printed_harmless_inside_a_table_cell(
    value: str, shown: str
) -> None:
    first = [{"case": "c-1", "grades": {"a": True}, "observed": {"k": value}}]
    second = [{"case": "c-1", "grades": {"a": True}, "observed": {"k": "plain"}}]

    text = render_markdown(diff_reports(make_report(first), make_report(second)))

    assert f"| c-1 | observed k | `{shown}` | `plain` |" in text.splitlines()
    for line in text.splitlines():
        assert line == "" or line.startswith(("#", "|", "No "))
        assert "::" not in line
    assert "\x1b" not in text
    assert "\r" not in text


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("[click](https://evil.example/x)", id="a-link"),
        pytest.param("![pixel](https://evil.example/p.png)", id="an-image"),
        pytest.param("**bold** and _italic_ and ~~strike~~", id="emphasis"),
        pytest.param("# a heading", id="a-heading"),
        pytest.param("https://evil.example/autolink", id="an-autolink"),
    ],
)
def test_an_observed_value_that_is_markdown_is_printed_inside_a_code_span(
    value: str,
) -> None:
    first = [{"case": "c-1", "grades": {"a": True}, "observed": {"k": value}}]
    second = [{"case": "c-1", "grades": {"a": True}, "observed": {"k": "plain"}}]

    rows = lines(make_report(first), make_report(second))

    # The text, but for the colon (the sanitiser's), is the whole cell between
    # backticks, and it holds none of its own, so the span cannot end early.
    shown = value.replace(":", "?")
    assert f"| c-1 | observed k | `{shown}` | `plain` |" in rows
    assert "`" not in shown


@pytest.mark.parametrize(
    ("value", "shown"),
    [
        pytest.param("", "empty", id="empty"),
        pytest.param("   ", "blank", id="spaces-only"),
        pytest.param("\n\t", "`??`", id="control-characters-only"),
        pytest.param(" x ", "` x `", id="spaces-kept-around-text"),
        pytest.param(7, "`7`", id="an-integer"),
        pytest.param(0, "`0`", id="zero"),
        pytest.param("absent", "absent", id="the-word-absent"),
    ],
)
def test_an_empty_or_blank_observed_value_stays_readable(
    value: str | int, shown: str
) -> None:
    first = [{"case": "c-1", "grades": {"a": True}, "observed": {"k": value}}]
    second = [{"case": "c-1", "grades": {"a": True}, "observed": {"k": "plain"}}]

    rows = lines(make_report(first), make_report(second))

    assert f"| c-1 | observed k | {shown} | `plain` |" in rows
    assert "| `` |" not in " ".join(rows)


def test_a_grade_and_the_differs_own_words_are_not_in_a_code_span() -> None:
    first = [{"case": "c-1", "grades": {"a": True}, "observed": {"k": None}}]
    second = [{"case": "c-1", "grades": {"a": False}, "observed": {}}]

    rows = lines(make_report(first), make_report(second))

    assert "| c-1 | grade a | passed | failed |" in rows
    assert "| c-1 | observed k | none | absent |" in rows


def test_a_hostile_value_in_b_is_just_as_harmless() -> None:
    first = [{"case": "c-1", "grades": {"a": True}, "observed": {"k": "plain"}}]
    second = [{"case": "c-1", "grades": {"a": True}, "observed": {"k": HOSTILE}}]

    rows = lines(make_report(first), make_report(second))

    assert "| c-1 | observed k | `plain` | `???error??x` |" in rows
    assert not any(row.startswith("::") for row in rows)
    assert not any("::" in row for row in rows)


def test_equal_observed_values_do_not_make_a_row() -> None:
    cases = [{"case": "c-1", "grades": {"a": True}, "observed": {"k": HOSTILE}}]

    text = render_markdown(diff_reports(make_report(cases), make_report(cases)))

    assert "No grade or observed value differs." in text
    assert "error" not in text


def test_latency_is_left_out_unless_every_model_calling_case_has_it() -> None:
    cases = second_cases()
    first = with_measured(
        cases, [measured(1, 100), measured(0, None), measured(1, None)]
    )
    second = with_measured(
        cases, [measured(1, 100), measured(0, None), measured(1, 300)]
    )

    rows = lines(make_report(first), make_report(second))

    assert "| A | 2 | 200 | 20 | 0.002468 | n/a | n/a |" in rows
    assert "| B | 2 | 200 | 20 | 0.002468 | 200 | 300 |" in rows


def test_a_report_without_a_model_call_has_no_latency_to_report() -> None:
    cases = with_measured(second_cases(), [measured(0, None)] * 3)

    rows = lines(make_report(cases), make_report(cases))

    assert "| A | 0 | 0 | 0 | 0.000000 | n/a | n/a |" in rows


def test_the_median_of_an_even_number_of_cases_may_end_in_a_half() -> None:
    cases = with_measured(
        second_cases(), [measured(1, 100), measured(1, 201), measured(0, None)]
    )

    rows = lines(make_report(cases), make_report(cases))

    assert "| A | 2 | 200 | 20 | 0.002468 | 150.5 | 201 |" in rows


def test_the_cost_is_exact_in_micro_euro() -> None:
    cases = with_measured(
        second_cases(),
        [
            measured(1, 1, cost_micro_eur=1_000_000),
            measured(1, 1, cost_micro_eur=2),
            measured(1, 1, cost_micro_eur=0),
        ],
    )

    rows = lines(make_report(cases), make_report(cases))

    assert any("| 1.000002 |" in row for row in rows)


def test_totals_are_left_out_when_only_one_report_is_measured() -> None:
    measured_cases = with_measured(second_cases(), [measured(1, 1)] * 3)

    text = render_markdown(diff_reports(make_report(measured_cases), make_report()))

    assert "## Cost and latency" not in text


def test_reports_of_different_workloads_cannot_be_read_side_by_side() -> None:
    with pytest.raises(ReportError, match="different workloads"):
        diff_reports(make_report(), make_report(other={"workload": "other"}))


def test_reports_with_different_cases_cannot_be_read_side_by_side() -> None:
    other = second_cases()[:2]

    with pytest.raises(ReportError, match="different cases"):
        diff_reports(make_report(), make_report(other))


def test_reports_with_different_graders_cannot_be_read_side_by_side() -> None:
    other = [
        {"case": c["case"], "grades": {"alpha": True, "gamma": True}, "observed": {}}
        for c in second_cases()
    ]

    with pytest.raises(ReportError, match="different graders"):
        diff_reports(make_report(), make_report(other))


def test_the_diff_is_a_value_and_rendering_it_twice_gives_the_same_text() -> None:
    diff = diff_reports(make_report(), make_report(second_cases()))

    assert render_markdown(diff) == render_markdown(diff)
    assert diff == diff_reports(make_report(), make_report(second_cases()))
    with pytest.raises(AttributeError):
        diff.totals = None  # type: ignore[misc]
