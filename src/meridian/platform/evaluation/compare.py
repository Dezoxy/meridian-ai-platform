"""Compare a new evaluation report with the committed baseline.

``problems`` and ``regressions`` fail the gate; ``improvements`` are
information. Whatever the baseline says, the new report must keep every
absolute grader true and every target's pass rate: a baseline that already
breaks a rule does not excuse the new run.
"""

from dataclasses import dataclass

from meridian.platform.evaluation.report import Report

MAX_IDS_NAMED = 10
REGENERATE = "regenerate the baseline in this change (make eval-baseline)"


@dataclass(frozen=True, slots=True)
class Comparison:
    problems: tuple[str, ...]  # each fails the gate
    regressions: tuple[str, ...]  # "CASE grader: passed -> failed"; fail the gate
    improvements: tuple[str, ...]  # "CASE grader: failed -> passed"
    # (grader, baseline passed, new passed, cases), over the cases and graders
    # both reports have, sorted by grader.
    rates: tuple[tuple[str, int, int, int], ...]

    @property
    def passed(self) -> bool:
        return not self.problems and not self.regressions


def _grades(report: Report) -> dict[str, dict[str, bool]]:
    return {case.case: dict(case.grades) for case in report.cases}


def _graders(report: Report) -> set[str]:
    return set(report.cases[0].grades)


def _named(ids: set[str]) -> str:
    ordered = sorted(ids)
    text = ", ".join(ordered[:MAX_IDS_NAMED]) or "none"
    more = len(ordered) - MAX_IDS_NAMED
    return f"{text} (and {more} more)" if more > 0 else text


def _target(target: float) -> str:
    return f"{target:.2f}" if round(target, 2) == target else f"{target:g}"


def _fingerprint_problems(baseline: Report, new: Report) -> list[str]:
    before, after = baseline.fingerprints, new.fingerprints
    problems = []
    if before.prompt != after.prompt:
        problems.append(f"the prompt changed: {REGENERATE}")
    if before.tools != after.tools:
        problems.append(f"the tools' contracts changed: {REGENERATE}")
    if before.golden_set != after.golden_set:
        problems.append(f"the golden set changed: {REGENERATE}")
    if before.judge != after.judge:
        problems.append(f"the judge's prompt changed: {REGENERATE}")
    if before.recording != after.recording:
        problems.append(f"the recording changed: {REGENERATE}")
    return problems


def _shape_problems(baseline: Report, new: Report) -> list[str]:
    problems = []
    before_cases = {case.case for case in baseline.cases}
    after_cases = {case.case for case in new.cases}
    if before_cases != after_cases:
        problems.append(
            "the cases differ: missing from the report "
            f"{_named(before_cases - after_cases)}; "
            f"not in the baseline {_named(after_cases - before_cases)}"
        )
    if _graders(baseline) != _graders(new):
        problems.append(
            "the graders differ: missing from the report "
            f"{_named(_graders(baseline) - _graders(new))}; "
            f"not in the baseline {_named(_graders(new) - _graders(baseline))}"
        )
    if set(baseline.absolute) != set(new.absolute):
        problems.append("the absolute graders differ from the baseline's")
    if baseline.targets != new.targets:
        problems.append("the targets differ from the baseline's")
    return problems


def _rule_problems(new: Report) -> list[str]:
    """What the new report breaks on its own, whatever the baseline says."""
    problems = [
        f"{case.case} {grader}: an absolute grader failed"
        for case in new.cases
        for grader in sorted(new.absolute)
        if not case.grades[grader]
    ]
    for grader, target in sorted(new.targets.items()):
        passed = sum(case.grades[grader] for case in new.cases)
        total = len(new.cases)
        if passed / total < target:
            problems.append(
                f"{grader}: {passed}/{total} passed, below the target {_target(target)}"
            )
    return problems


def compare(baseline: Report, new: Report) -> Comparison:
    """Compare ``new`` with ``baseline``; see the module docstring."""
    problems: list[str] = []
    if baseline.workload != new.workload:
        problems.append(
            f"the workload differs: baseline {baseline.workload}, report {new.workload}"
        )
    problems += _fingerprint_problems(baseline, new)
    problems += _shape_problems(baseline, new)
    problems += _rule_problems(new)
    if baseline.answered_by != new.answered_by:
        before, after = baseline.answered_by, new.answered_by
        problems.append(
            "the model that answered changed: "
            f"{before.kind} ({before.label}) -> {after.kind} ({after.label})"
        )

    before_grades, after_grades = _grades(baseline), _grades(new)
    cases = sorted(before_grades.keys() & after_grades.keys())
    graders = sorted(_graders(baseline) & _graders(new))
    regressions = []
    improvements = []
    for case in cases:
        for grader in graders:
            was, is_now = before_grades[case][grader], after_grades[case][grader]
            if was and not is_now:
                regressions.append(f"{case} {grader}: passed -> failed")
            elif is_now and not was:
                improvements.append(f"{case} {grader}: failed -> passed")
    rates = tuple(
        (
            grader,
            sum(before_grades[case][grader] for case in cases),
            sum(after_grades[case][grader] for case in cases),
            len(cases),
        )
        for grader in graders
    )
    return Comparison(
        problems=tuple(problems),
        regressions=tuple(regressions),
        improvements=tuple(improvements),
        rates=rates,
    )
