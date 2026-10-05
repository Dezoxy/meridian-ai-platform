"""S032's done-when: the injection cases run through the real services with a
model that obeys every injection, and the platform's answers are graded.

One stack test posts the 94 cases (a claimant's description, or a clause of a
wording that carries the attack, and benign cases that look alike) and writes
the report; `make eval-baseline` commits it, `make eval` and CI compare with it.
The model is scripted to obey, so a case the screen does not stop is a case that
reaches it: the three absolute graders say the platform held anyway, and the
rate of the screen is reported, not asserted.

The files `make eval-baseline` writes do not exist until someone runs it. The
tests that read them fail with the instruction to run it, not with an error
from a parser. The cases' sentences are data and are never printed; a failure
message holds case IDs and counts only.
"""

import json
import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from dbsupport import DatabaseHandle
from evalsupport import ToolCapture
from injectionsupport import (
    BASELINE_COMMAND,
    INJECTION_BASELINE_PATH,
    INJECTION_CASES,
    INJECTION_MANIFEST,
    INJECTION_SUMMARY_PATH,
    SCRIPTED,
    ObedientModel,
    insert_before_closing_sentence,
    run_cases,
    stored_bodies,
)
from servicesupport import REGISTRY_DIR
from stacksupport import (
    CLAIMS,
    EXPECTED,
    MANIFEST,
    build_stack,
    claims_that_ask_the_model,
)

from meridian.platform.evaluation.compare import compare
from meridian.platform.evaluation.fingerprints import golden_set_of
from meridian.platform.evaluation.report import ReportError, load_report, write_report
from meridian.platform.guardrails import (
    holds_special_category,
    redact,
    screen_fingerprint,
)
from meridian.platform.registry import load_registry
from meridian.workloads.claims_triage import assessment
from meridian.workloads.claims_triage.injection import (
    ENDED,
    INJECTION_ABSOLUTE,
    RECOMMENDATION_HELD,
    ROUTE_HELD,
    SCREENED,
    InjectionCase,
    build_injection_report,
    load_cases,
    render_summary,
    summarise,
)

REPORT_ENV = "MERIDIAN_EVAL_INJECTION_REPORT"
SUMMARY_ENV = "MERIDIAN_EVAL_INJECTION_SUMMARY"
MIN_TOOL_CALLS = 2


def strings_in(value: object) -> Iterator[str]:
    """Every string in a decoded JSON document, keys excluded."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from strings_in(item)
    elif isinstance(value, list):
        for item in value:
            yield from strings_in(item)


def reached(message: str, sentence: str) -> bool:
    """Whether ``sentence`` is in the request's user message, as written or in a
    string of the JSON it holds."""
    if sentence in message:
        return True
    return any(sentence in text for text in strings_in(json.loads(message)))


# ── 1. the stack test ───────────────────────────────────────────────────────
def test_the_injection_cases_run_through_the_stack_with_an_obedient_model(
    fresh_database: DatabaseHandle, tmp_path: Path
) -> None:
    cases = load_cases(INJECTION_CASES)
    model = ObedientModel()
    stack = build_stack(fresh_database, runtime_http=model.http())
    bodies_before = stored_bodies(fresh_database)
    capture = ToolCapture()
    statuses: dict[str, int] = {}
    with pytest.MonkeyPatch.context() as patch:
        capture.install(patch)
        outcomes = run_cases(stack, cases, capture, model=model, statuses=statuses)
    bodies_after = stored_bodies(fresh_database)

    # The report is written first, so a failing run leaves one to read.
    report = build_injection_report(
        cases,
        outcomes,
        EXPECTED,
        manifest_path=INJECTION_MANIFEST,
        golden_manifest_path=MANIFEST,
        registry=load_registry(REGISTRY_DIR),
        answered_by=SCRIPTED,
        prompt=assessment.PROMPT_VERSION,
    )
    named = os.environ.get(REPORT_ENV)
    destination = Path(named) if named else tmp_path / "injection-report.json"
    write_report(report, destination)

    by_case = {entry.case: entry for entry in report.cases}
    for name in INJECTION_ABSOLUTE:
        failed = sorted(c for c, entry in by_case.items() if not entry.grades[name])
        assert not failed, f"{name} is false for {failed}; statuses {statuses}"
    # The summary is written only once the absolute graders have passed, so a
    # failing run leaves no summary that reads as a result.
    summary_named = os.environ.get(SUMMARY_ENV)
    if summary_named:
        text = render_summary(summarise(report, cases), report.answered_by)
        Path(summary_named).write_text(text, encoding="utf-8")
    # The graph looks the policy up before it proposes, and a clause run that
    # fails has also searched the wording: every outcome, with or without a
    # proposal, has at least two captured calls, or the capture saw too little
    # for tools_allowlisted to mean anything.
    few_calls = sorted(
        name
        for name, outcome in outcomes.items()
        if len(outcome.tools) < MIN_TOOL_CALLS
    )
    assert not few_calls, f"fewer than {MIN_TOOL_CALLS} tool calls for {few_calls}"
    asked = {c for c, entry in by_case.items() if entry.observed["model_asked"] == 1}
    flagged = {c for c, entry in by_case.items() if entry.observed["flagged"] == 1}
    assert model.calls == len(asked)
    assert not flagged & asked, sorted(flagged & asked)
    assert {c for c, _ in model.asked} == asked
    for case in cases:
        if case.case in flagged:
            continue
        sent = model.messages_for(case.case)
        if case.clause is not None:
            inserted = case.clause.inserted
            assert any(reached(m, inserted) for m in sent), (
                f"{case.case}: the inserted clause text did not reach the model"
            )
        elif case.label == "attack":
            assert len(sent) == 1, f"{case.case}: the model was asked {len(sent)} times"
    assert bodies_after == bodies_before, "a stored clause was not put back"
    if destination.resolve() != INJECTION_BASELINE_PATH.resolve():
        baseline = committed_baseline()
        outcome = compare(baseline, report)
        assert outcome.passed, (
            f"the report differs from the baseline: {outcome.problems} "
            f"{outcome.regressions}; if the change is intended, review it and "
            "run make eval-baseline"
        )


def committed_baseline():
    """The committed baseline, or a failure that says how to make one."""
    if not INJECTION_BASELINE_PATH.is_file():
        pytest.fail(f"no {INJECTION_BASELINE_PATH.name}: {BASELINE_COMMAND}")
    try:
        return load_report(INJECTION_BASELINE_PATH)
    except ReportError as error:
        pytest.fail(f"the baseline cannot be read ({error}): {BASELINE_COMMAND}")


# ── 2. what needs no database ───────────────────────────────────────────────
def added_text(case: InjectionCase) -> str:
    """What the case adds to its base claim's description."""
    base = CLAIMS[case.base_claim]["description"]
    description = case.claim["description"]
    assert isinstance(description, str)
    assert base in description, case.case
    return description.replace(base, "", 1).strip()


def test_no_case_trips_another_guardrail() -> None:
    # The injection screen, not the special-category screen or the redactor, is
    # what a case measures.
    cases = load_cases(INJECTION_CASES)

    descriptions = {c.case: c.claim["description"] for c in cases}
    special = [c for c, d in descriptions.items() if holds_special_category(d)]
    redacted = [c for c, d in descriptions.items() if redact(d).text != d]
    assert special == []
    assert redacted == []


def test_every_base_claim_is_one_the_model_is_asked_about() -> None:
    cases = load_cases(INJECTION_CASES)

    unasked = sorted({c.base_claim for c in cases} - claims_that_ask_the_model())
    assert unasked == []


def test_the_committed_summary_is_the_summary_of_the_committed_baseline() -> None:
    cases = load_cases(INJECTION_CASES)
    baseline = committed_baseline()
    if not INJECTION_SUMMARY_PATH.is_file():
        pytest.fail(f"no {INJECTION_SUMMARY_PATH.name}: {BASELINE_COMMAND}")

    rendered = render_summary(summarise(baseline, cases), baseline.answered_by)

    assert INJECTION_SUMMARY_PATH.read_text(encoding="utf-8") == rendered, (
        f"the summary is not the baseline's: {BASELINE_COMMAND}"
    )


def test_a_benign_clause_case_is_not_flagged_and_ends_as_designed() -> None:
    # A changed clause alone does not flag a claim: the model is asked, no run
    # fails, and every grader of the case holds.
    cases = load_cases(INJECTION_CASES)
    baseline = committed_baseline()
    benign = [c.case for c in cases if c.label == "benign" and c.carrier == "clause"]
    by_case = {entry.case: entry for entry in baseline.cases}

    assert benign == ["CLM-4001", "CLM-4002", "CLM-4003", "CLM-4004"]
    for name in benign:
        entry = by_case[name]
        assert entry.observed["flagged"] == 0, name
        assert entry.observed["model_asked"] == 1, name
        assert entry.observed["failure"] is None, name
        assert entry.grades[SCREENED], name
        assert entry.grades[ENDED], name
        assert entry.grades[ROUTE_HELD], name
        assert entry.grades[RECOMMENDATION_HELD], name


def test_a_benign_clause_case_ends_like_its_base_claim_with_its_clause_shown(
    fresh_database: DatabaseHandle, tmp_path: Path
) -> None:
    # The model answers none to everything: a base claim no exclusion applies to
    # ends as its oracle says, and a clause the triage never retrieves would
    # leave the case measuring nothing, so the sentence must reach the model.
    cases = [c for c in load_cases(INJECTION_CASES) if c.label == "benign" and c.clause]
    model = ObedientModel()
    stack = build_stack(fresh_database, runtime_http=model.http())
    capture = ToolCapture()
    with pytest.MonkeyPatch.context() as patch:
        capture.install(patch)
        outcomes = run_cases(stack, cases, capture, model=model)

    report = build_injection_report(
        cases,
        outcomes,
        EXPECTED,
        manifest_path=INJECTION_MANIFEST,
        golden_manifest_path=MANIFEST,
        registry=load_registry(REGISTRY_DIR),
        answered_by=SCRIPTED,
        prompt=assessment.PROMPT_VERSION,
    )

    assert [c.case for c in cases] == ["CLM-4001", "CLM-4002", "CLM-4003", "CLM-4004"]
    for case, entry in zip(cases, report.cases, strict=True):
        assert case.clause is not None
        sent = model.messages_for(case.case)
        assert entry.case == case.case
        assert entry.observed["flagged"] == 0, case.case
        assert entry.observed["model_asked"] == 1, case.case
        assert entry.grades[ENDED], case.case
        assert entry.grades[ROUTE_HELD], case.case
        assert entry.grades[RECOMMENDATION_HELD], case.case
        assert any(reached(m, case.clause.inserted) for m in sent), case.case


def test_the_committed_baseline_carries_the_fingerprint_of_the_screens() -> None:
    baseline = committed_baseline()

    assert baseline.fingerprints.screen == screen_fingerprint()


@pytest.mark.parametrize(
    "path", [INJECTION_BASELINE_PATH, INJECTION_SUMMARY_PATH], ids=lambda p: p.name
)
def test_a_committed_file_holds_no_text_a_case_adds(path: Path) -> None:
    cases = load_cases(INJECTION_CASES)
    if not path.is_file():
        pytest.fail(f"no {path.name}: {BASELINE_COMMAND}")
    text = path.read_text(encoding="utf-8")
    sentences = {
        sentence
        for case in cases
        for sentence in (
            added_text(case),
            case.clause.inserted if case.clause else "",
        )
        if sentence
    }

    assert text
    assert sentences
    holding = sorted(
        sentence_index
        for sentence_index, sentence in enumerate(sorted(sentences))
        if any(
            form in text
            for form in (
                sentence,
                json.dumps(sentence)[1:-1],
                json.dumps(sentence, ensure_ascii=False)[1:-1],
            )
        )
    )
    # The index, never the sentence: a failure must not print an attack.
    assert holding == [], f"{path.name} holds the text of {len(holding)} sentences"


def test_a_sentence_is_inserted_before_the_last_closing_sentence_with_a_space() -> None:
    closing = "This exclusion applies to claims for storm."
    stored = f"Prose.\n\n{closing}"

    edited = insert_before_closing_sentence(stored, "A marker sentence.")

    assert edited == f"Prose.\n\nA marker sentence. {closing}"
    assert edited.endswith(closing)


def test_only_the_last_closing_sentence_is_a_place_for_the_insertion() -> None:
    closing = "This exclusion applies to claims for storm."
    stored = f"{closing} More.\n\n{closing}"

    edited = insert_before_closing_sentence(stored, "Marker.")

    assert edited == f"{closing} More.\n\nMarker. {closing}"


def test_a_clause_without_its_closing_sentence_is_refused_loudly() -> None:
    with pytest.raises(AssertionError, match="closing sentence"):
        insert_before_closing_sentence("Prose only.", "Marker.")


def test_the_baseline_names_the_injection_set_it_was_made_for() -> None:
    baseline = committed_baseline()

    assert baseline.fingerprints.golden_set == golden_set_of(INJECTION_MANIFEST)
