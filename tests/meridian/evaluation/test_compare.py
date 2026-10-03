"""The comparison of a new evaluation report with the committed baseline."""

from typing import Any

from meridian.platform.evaluation.compare import Comparison, compare
from meridian.platform.evaluation.report import Report

DIGEST = "ab" * 32
OTHER_DIGEST = "cd" * 32


def make_report(
    grades: dict[str, dict[str, bool]] | None = None,
    **overrides: Any,
) -> Report:
    """A report from ``{case: {grader: passed}}``; graders alpha and beta."""
    if grades is None:
        grades = {
            "c-1": {"alpha": True, "beta": True},
            "c-2": {"alpha": True, "beta": True},
            "c-3": {"alpha": True, "beta": False},
        }
    data: dict[str, Any] = {
        "format": 2,
        "workload": "demo",
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
            {"case": case, "grades": grader_grades, "observed": {}}
            for case, grader_grades in sorted(grades.items())
        ],
    }
    data.update(overrides)
    return Report.model_validate(data)


def with_fingerprint(name: str, value: Any) -> Report:
    base = make_report()
    fingerprints = base.model_dump(mode="json")["fingerprints"]
    fingerprints[name] = value
    return make_report(fingerprints=fingerprints)


def with_fingerprints(values: dict[str, Any]) -> dict[str, Any]:
    fingerprints = make_report().model_dump(mode="json")["fingerprints"]
    return fingerprints | values


def only_problem(comparison: Comparison, fragment: str) -> None:
    assert len(comparison.problems) == 1, comparison.problems
    assert fragment in comparison.problems[0]
    assert not comparison.passed


def test_identical_reports_pass_with_nothing_to_say() -> None:
    comparison = compare(make_report(), make_report())

    assert comparison.passed
    assert comparison.problems == ()
    assert comparison.regressions == ()
    assert comparison.improvements == ()
    assert comparison.rates == (("alpha", 3, 3, 3), ("beta", 2, 2, 3))


def test_a_different_workload_is_a_problem() -> None:
    comparison = compare(make_report(), make_report(workload="other"))

    only_problem(comparison, "workload")


def test_a_changed_prompt_asks_for_a_new_baseline() -> None:
    comparison = compare(make_report(), with_fingerprint("prompt", OTHER_DIGEST))

    only_problem(comparison, "the prompt changed")
    assert "make eval-baseline" in comparison.problems[0]


def test_changed_tools_ask_for_a_new_baseline() -> None:
    comparison = compare(make_report(), with_fingerprint("tools", OTHER_DIGEST))

    only_problem(comparison, "the tools' contracts changed")
    assert "make eval-baseline" in comparison.problems[0]


def test_a_changed_judge_prompt_asks_for_a_new_baseline() -> None:
    comparison = compare(
        with_fingerprint("judge", DIGEST), with_fingerprint("judge", OTHER_DIGEST)
    )

    only_problem(comparison, "the judge's prompt changed")
    assert "make eval-baseline" in comparison.problems[0]


def test_a_changed_recording_asks_for_a_new_baseline() -> None:
    comparison = compare(
        with_fingerprint("recording", DIGEST),
        with_fingerprint("recording", OTHER_DIGEST),
    )

    only_problem(comparison, "the recording changed")
    assert "make eval-baseline" in comparison.problems[0]


def test_a_judge_or_recording_on_one_side_only_is_a_difference() -> None:
    only_problem(
        compare(make_report(), with_fingerprint("judge", DIGEST)),
        "the judge's prompt changed",
    )
    only_problem(
        compare(with_fingerprint("recording", DIGEST), make_report()),
        "the recording changed",
    )


def test_an_equal_judge_and_recording_pass() -> None:
    both = {"judge": DIGEST, "recording": OTHER_DIGEST}

    comparison = compare(
        make_report(fingerprints=with_fingerprints(both)),
        make_report(fingerprints=with_fingerprints(both)),
    )

    assert comparison.passed
    assert comparison.problems == ()


def test_tools_and_measures_that_differ_are_never_compared() -> None:
    def with_extras(calls: int, tool: str) -> Report:
        data = make_report().model_dump(mode="json")
        for case in data["cases"]:
            case["tools"] = [{"tool": tool, "arguments": {"n": calls}}]
            case["measured"] = {
                "model_calls": calls,
                "input_tokens": calls * 10,
                "output_tokens": calls,
                "cost_micro_eur": calls * 100,
                "latency_ms": calls * 7,
            }
        return Report.model_validate(data)

    comparison = compare(with_extras(1, "lookup"), with_extras(9, "decide"))

    assert comparison.passed
    assert comparison.problems == ()
    assert comparison == compare(make_report(), make_report())


def test_a_changed_golden_set_asks_for_a_new_baseline() -> None:
    golden_set = {
        "generator_version": "1",
        "seed": 8,
        "files": {"a.json": DIGEST},
        "manifest": DIGEST,
    }

    comparison = compare(make_report(), with_fingerprint("golden_set", golden_set))

    only_problem(comparison, "the golden set changed")
    assert "make eval-baseline" in comparison.problems[0]


def test_a_changed_manifest_alone_asks_for_a_new_baseline() -> None:
    golden_set = {
        "generator_version": "1",
        "seed": 7,
        "files": {"a.json": DIGEST},
        "manifest": OTHER_DIGEST,
    }

    comparison = compare(make_report(), with_fingerprint("golden_set", golden_set))

    only_problem(comparison, "the golden set changed")


def test_a_different_set_of_cases_names_the_missing_and_the_extra() -> None:
    new = make_report(
        {
            "c-1": {"alpha": True, "beta": True},
            "c-2": {"alpha": True, "beta": True},
            "c-9": {"alpha": True, "beta": True},
        }
    )

    comparison = compare(make_report(), new)

    only_problem(comparison, "c-3")
    assert "c-9" in comparison.problems[0]


def test_the_case_problem_names_at_most_ten_ids_each_way() -> None:
    baseline = make_report(
        {f"b-{n:02}": {"alpha": True, "beta": True} for n in range(15)}
    )
    new = make_report({f"n-{n:02}": {"alpha": True, "beta": True} for n in range(15)})

    comparison = compare(baseline, new)

    problem = comparison.problems[0]
    assert "b-09" in problem
    assert "b-10" not in problem
    assert "n-09" in problem
    assert "n-10" not in problem


def test_different_graders_are_a_problem() -> None:
    grades = {c: {"alpha": True, "gamma": True} for c in ("c-1", "c-2", "c-3")}
    new = make_report(grades, targets={})

    comparison = compare(make_report(), new)

    assert any("graders differ" in p for p in comparison.problems)
    assert not comparison.passed


def test_different_absolute_graders_are_a_problem() -> None:
    comparison = compare(make_report(absolute=[]), make_report())

    only_problem(comparison, "absolute")


def test_equal_absolute_graders_are_not_a_difference() -> None:
    baseline = make_report(absolute=["alpha", "beta"], targets={})
    new = make_report(absolute=["alpha", "beta"], targets={})

    comparison = compare(baseline, new)

    assert not any("absolute graders differ" in p for p in comparison.problems)


def test_different_targets_are_a_problem() -> None:
    comparison = compare(make_report(), make_report(targets={"beta": 0.4}))

    only_problem(comparison, "targets")


def test_an_absolute_failure_in_the_new_report_fails_the_gate() -> None:
    new = make_report(
        {
            "c-1": {"alpha": False, "beta": True},
            "c-2": {"alpha": True, "beta": True},
            "c-3": {"alpha": True, "beta": False},
        }
    )

    comparison = compare(make_report(), new)

    assert "c-1 alpha: an absolute grader failed" in comparison.problems
    assert not comparison.passed


def test_an_absolute_failure_fails_even_when_the_baseline_has_it_too() -> None:
    grades = {
        "c-1": {"alpha": False, "beta": True},
        "c-2": {"alpha": True, "beta": True},
        "c-3": {"alpha": True, "beta": False},
    }

    comparison = compare(make_report(grades), make_report(grades))

    only_problem(comparison, "c-1 alpha: an absolute grader failed")
    assert comparison.regressions == ()


def test_a_target_shortfall_fails_the_gate() -> None:
    new = make_report(
        {
            "c-1": {"alpha": True, "beta": False},
            "c-2": {"alpha": True, "beta": False},
            "c-3": {"alpha": True, "beta": True},
        },
        targets={"beta": 0.9},
    )

    comparison = compare(make_report(targets={"beta": 0.9}), new)

    assert "beta: 1/3 passed, below the target 0.90" in comparison.problems
    assert not comparison.passed


def test_a_target_shortfall_fails_even_when_the_baseline_has_it_too() -> None:
    grades = {
        "c-1": {"alpha": True, "beta": False},
        "c-2": {"alpha": True, "beta": True},
        "c-3": {"alpha": True, "beta": False},
    }

    comparison = compare(make_report(grades), make_report(grades))

    only_problem(comparison, "beta: 1/3 passed, below the target 0.50")
    assert comparison.regressions == ()


def test_a_pass_rate_exactly_at_the_target_passes() -> None:
    grades = {f"c-{n}": {"alpha": True, "beta": n <= 9} for n in range(1, 11)}

    comparison = compare(
        make_report(grades, targets={"beta": 0.9}),
        make_report(grades, targets={"beta": 0.9}),
    )

    assert comparison.passed


def test_a_target_between_two_decimals_is_printed_as_it_is() -> None:
    grades = {"c-1": {"alpha": True, "beta": False}}

    comparison = compare(
        make_report(grades, targets={"beta": 0.875}),
        make_report(grades, targets={"beta": 0.875}),
    )

    assert "below the target 0.875" in comparison.problems[0]


def test_a_different_answering_model_is_a_problem() -> None:
    new = make_report(answered_by={"kind": "live", "label": "real"})

    comparison = compare(make_report(), new)

    only_problem(comparison, "the model that answered changed")
    assert "scripted" in comparison.problems[0]
    assert "live" in comparison.problems[0]


def test_a_regression_and_an_improvement_are_reported() -> None:
    new = make_report(
        {
            "c-1": {"alpha": True, "beta": False},
            "c-2": {"alpha": True, "beta": True},
            "c-3": {"alpha": True, "beta": True},
        },
        targets={"beta": 0.5},
    )

    comparison = compare(make_report(), new)

    assert comparison.regressions == ("c-1 beta: passed -> failed",)
    assert comparison.improvements == ("c-3 beta: failed -> passed",)
    assert not comparison.passed
    assert comparison.problems == ()


def test_an_improvement_alone_passes() -> None:
    new = make_report(
        {
            "c-1": {"alpha": True, "beta": True},
            "c-2": {"alpha": True, "beta": True},
            "c-3": {"alpha": True, "beta": True},
        }
    )

    comparison = compare(make_report(), new)

    assert comparison.passed
    assert comparison.improvements == ("c-3 beta: failed -> passed",)


def test_regressions_are_still_found_over_the_cases_both_reports_have() -> None:
    new = make_report(
        {
            "c-1": {"alpha": True, "beta": False},
            "c-2": {"alpha": True, "beta": True},
            "c-9": {"alpha": True, "beta": True},
        }
    )

    comparison = compare(make_report(), new)

    assert comparison.regressions == ("c-1 beta: passed -> failed",)
    assert any("c-9" in p for p in comparison.problems)
    assert comparison.rates == (("alpha", 2, 2, 2), ("beta", 2, 1, 2))


def test_regressions_are_still_found_over_the_graders_both_reports_have() -> None:
    grades = {
        "c-1": {"alpha": False, "gamma": True},
        "c-2": {"alpha": True, "gamma": True},
        "c-3": {"alpha": True, "gamma": True},
    }
    new = make_report(grades, targets={})

    comparison = compare(make_report(), new)

    assert "c-1 alpha: passed -> failed" in comparison.regressions
    assert [rate[0] for rate in comparison.rates] == ["alpha"]


def test_the_output_is_ordered_by_case_then_grader() -> None:
    baseline = make_report(
        {
            "c-1": {"alpha": True, "beta": True},
            "c-2": {"alpha": True, "beta": True},
            "c-3": {"alpha": True, "beta": True},
        },
        absolute=[],
        targets={},
    )
    new = make_report(
        {
            "c-1": {"alpha": False, "beta": False},
            "c-2": {"alpha": True, "beta": True},
            "c-3": {"alpha": False, "beta": True},
        },
        absolute=[],
        targets={},
    )

    comparison = compare(baseline, new)

    assert comparison.regressions == (
        "c-1 alpha: passed -> failed",
        "c-1 beta: passed -> failed",
        "c-3 alpha: passed -> failed",
    )
    assert comparison == compare(baseline, new)


def test_problems_come_in_the_documented_order() -> None:
    new = make_report(
        {
            "c-1": {"alpha": False, "beta": False},
            "c-2": {"alpha": True, "beta": False},
            "c-3": {"alpha": True, "beta": False},
        },
        workload="other",
        answered_by={"kind": "live", "label": "real"},
        targets={"beta": 0.9},
    )

    problems = compare(make_report(), new).problems

    assert len(problems) == 5
    assert "workload" in problems[0]
    assert "targets" in problems[1]
    assert "absolute grader failed" in problems[2]
    assert "below the target" in problems[3]
    assert "the model that answered changed" in problems[4]


def test_a_baseline_that_breaks_a_rule_is_not_a_problem_by_itself() -> None:
    baseline = make_report(
        {
            "c-1": {"alpha": False, "beta": False},
            "c-2": {"alpha": True, "beta": False},
            "c-3": {"alpha": True, "beta": False},
        }
    )
    new = make_report()

    comparison = compare(baseline, new)

    assert comparison.passed
    assert comparison.improvements == (
        "c-1 alpha: failed -> passed",
        "c-1 beta: failed -> passed",
        "c-2 beta: failed -> passed",
    )
