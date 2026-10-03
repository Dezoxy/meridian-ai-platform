"""The claims workload's plugin for ``meridian eval run`` (S050, T-78): what it
submits, where it reads the answers, how it grades them and who answered; the
platform's loader that finds it; and one run over the real services in process.
"""

import json
from collections.abc import Callable, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from dbsupport import DatabaseHandle
from servicesupport import REGISTRY_DIR, owner_rows
from stacksupport import WINDOW_SECONDS, ScriptedModel, build_stack, golden_answer
from toolsupport import SYNTHETIC_DIR
from typer.testing import CliRunner
from workloads.claims_triage.test_claims_app import DRAFTED_BY, OUTPUT

from meridian.platform.cli import app
from meridian.platform.cli import evaluation as cli
from meridian.platform.evaluation import workload
from meridian.platform.evaluation.report import (
    AnsweredBy,
    ReportError,
    load_report,
)
from meridian.platform.evaluation.run import run_cases
from meridian.platform.evaluation.workload import (
    EVALUATIONS_GROUP,
    Submission,
    WorkloadEvaluation,
    load_evaluation,
)
from meridian.platform.registry import load_registry
from meridian.workloads.claims_triage.evaluation import RULE_GRADERS
from meridian.workloads.claims_triage.evaluation_http import EVALUATION

CLAIMS = json.loads((SYNTHETIC_DIR / "claims.json").read_text(encoding="utf-8"))
REGISTRY = load_registry(REGISTRY_DIR)
MODES = {
    "replay": AnsweredBy(kind="replay", label="simulated"),
    "recorded": AnsweredBy(kind="recorded", label="real"),
    "live": AnsweredBy(kind="live", label="real"),
}
FIRST_THREE = ("CLM-0001", "CLM-0002", "CLM-0003")
runner = CliRunner()


def answer(claim_id: str, proposal: dict[str, Any] | None) -> dict[str, Any]:
    return {"claim_id": claim_id, "state": "awaiting_adjuster", "proposal": proposal}


def drafted(mode: str, prompt: str | None = "a" * 64) -> dict[str, Any]:
    return OUTPUT | {"drafted_by": DRAFTED_BY | {"mode": mode, "prompt": prompt}}


def report_of(answers: dict[str, Any]):
    return EVALUATION.report(answers, SYNTHETIC_DIR, REGISTRY)


# ── what it submits and reads ───────────────────────────────────────────────
def test_the_plugin_satisfies_the_protocol_and_names_its_workload() -> None:
    assert isinstance(EVALUATION, WorkloadEvaluation)
    assert EVALUATION.workload == "claims-triage"


def test_it_submits_every_golden_claim_in_claim_id_order_to_claims() -> None:
    submissions = EVALUATION.submissions(SYNTHETIC_DIR)

    assert len(submissions) == len(CLAIMS) == 40
    assert [s.case for s in submissions] == sorted(c["claim_id"] for c in CLAIMS)
    assert {s.path for s in submissions} == {"/claims"}
    by_id = {c["claim_id"]: c for c in CLAIMS}
    assert all(dict(s.body) == by_id[s.case] for s in submissions)
    assert all(isinstance(s, Submission) for s in submissions)


def test_the_answer_of_a_case_is_read_under_the_adjusters_path() -> None:
    assert EVALUATION.answer_path("CLM-0007") == "/adjuster/claims/CLM-0007/proposal"


def test_a_golden_set_that_cannot_be_read_is_a_report_error(tmp_path: Path) -> None:
    with pytest.raises(ReportError):
        EVALUATION.submissions(tmp_path)


def test_a_claims_file_that_is_not_a_list_of_claims_is_a_report_error(
    tmp_path: Path,
) -> None:
    (tmp_path / "claims.json").write_text('{"claim_id": "CLM-0001"}', encoding="utf-8")

    with pytest.raises(ReportError):
        EVALUATION.submissions(tmp_path)


# ── who answered ────────────────────────────────────────────────────────────
@pytest.mark.parametrize("mode", sorted(MODES))
def test_the_one_mode_the_proposals_name_says_who_answered(mode: str) -> None:
    answers = {
        "CLM-0001": answer("CLM-0001", drafted(mode)),
        "CLM-0002": answer("CLM-0002", drafted(mode)),
    }

    assert report_of(answers).answered_by == MODES[mode]


def test_when_no_proposal_names_a_mode_the_run_is_replay_and_simulated() -> None:
    plain = OUTPUT | {"drafted_by": None}
    answers = {"CLM-0001": answer("CLM-0001", plain)}

    assert report_of(answers).answered_by == MODES["replay"]


def test_a_proposal_with_no_model_beside_one_with_a_mode_is_that_mode() -> None:
    answers = {
        "CLM-0001": answer("CLM-0001", OUTPUT | {"drafted_by": None}),
        "CLM-0002": answer("CLM-0002", drafted("live")),
    }

    assert report_of(answers).answered_by == MODES["live"]


def test_proposals_that_name_different_modes_are_refused() -> None:
    answers = {
        "CLM-0001": answer("CLM-0001", drafted("replay")),
        "CLM-0002": answer("CLM-0002", drafted("live")),
    }

    with pytest.raises(ReportError, match="more than one mode"):
        report_of(answers)


def test_a_mode_that_is_not_known_is_refused_without_quoting_it() -> None:
    answers = {"CLM-0001": answer("CLM-0001", drafted("sneaky-mode"))}

    with pytest.raises(ReportError) as refused:
        report_of(answers)

    assert "sneaky-mode" not in str(refused.value)


def test_the_prompt_fingerprint_is_the_one_the_proposals_name() -> None:
    answers = {"CLM-0001": answer("CLM-0001", drafted("replay", "b" * 64))}

    assert report_of(answers).fingerprints.prompt == "b" * 64


def test_proposals_that_name_different_prompts_are_refused() -> None:
    answers = {
        "CLM-0001": answer("CLM-0001", drafted("replay", "b" * 64)),
        "CLM-0002": answer("CLM-0002", drafted("replay", "c" * 64)),
    }

    with pytest.raises(ReportError, match="more than one prompt"):
        report_of(answers)


# ── the grading ─────────────────────────────────────────────────────────────
def test_the_report_holds_only_the_cases_that_ran_and_the_ten_rule_graders() -> None:
    answers = {
        "CLM-0003": answer("CLM-0003", drafted("replay")),
        "CLM-0001": answer("CLM-0001", drafted("replay")),
    }

    report = report_of(answers)

    assert [case.case for case in report.cases] == ["CLM-0001", "CLM-0003"]
    assert all(tuple(case.grades) == RULE_GRADERS for case in report.cases)
    assert all(case.measured is None and case.tools is None for case in report.cases)
    assert report.workload == "claims-triage"


def test_an_answer_with_no_proposal_is_a_claim_with_no_proposal() -> None:
    answers = {
        "CLM-0001": answer("CLM-0001", None),
        "CLM-0002": {"claim_id": "CLM-0002", "state": "triage_failed"},
        "CLM-0003": answer("CLM-0003", drafted("replay")),
    }

    report = report_of(answers)

    by_case = {case.case: case for case in report.cases}
    assert not any(by_case["CLM-0001"].grades.values())
    assert not any(by_case["CLM-0002"].grades.values())
    assert by_case["CLM-0003"].grades["completed"] is True


def test_a_proposal_that_is_not_a_proposal_is_graded_as_none() -> None:
    answers = {"CLM-0001": answer("CLM-0001", {"route": "adjuster"})}

    (case,) = report_of(answers).cases

    assert not any(case.grades.values())


def test_an_answer_for_a_case_the_golden_set_does_not_hold_is_refused() -> None:
    answers = {"CLM-9999": answer("CLM-9999", drafted("replay"))}

    with pytest.raises(ReportError):
        report_of(answers)


def test_no_answer_at_all_is_refused_not_reported_empty() -> None:
    with pytest.raises(ReportError):
        report_of({})


# ── the loader ──────────────────────────────────────────────────────────────
def entry(
    name: str = "claims-triage",
    *,
    value: str = "meridian.workloads.claims_triage.evaluation_http:EVALUATION",
    dist: str | None = "meridian",
    loads: Callable[[], object] = lambda: EVALUATION,
) -> SimpleNamespace:
    return SimpleNamespace(
        name=name,
        value=value,
        module=value.split(":")[0],
        dist=None if dist is None else SimpleNamespace(name=dist),
        load=loads,
    )


def publish(monkeypatch: pytest.MonkeyPatch, *entries: SimpleNamespace) -> None:
    def entry_points(*, group: str) -> list[SimpleNamespace]:
        assert group == EVALUATIONS_GROUP
        return list(entries)

    monkeypatch.setattr(workload, "entry_points", entry_points)


def test_the_installed_plugin_is_found_by_its_workload() -> None:
    assert EVALUATIONS_GROUP == "meridian.evaluations"
    assert load_evaluation("claims-triage") is EVALUATION


def test_an_unknown_name_is_refused_and_the_known_ones_are_named() -> None:
    with pytest.raises(ReportError) as refused:
        load_evaluation("no-such-workload")

    assert "claims-triage" in str(refused.value)
    assert "no-such-workload" not in str(refused.value)


def test_a_published_entry_of_the_trusted_distribution_loads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publish(monkeypatch, entry())

    assert load_evaluation("claims-triage") is EVALUATION


@pytest.mark.parametrize(
    ("changes", "why"),
    [
        ({"dist": "meridian-evil"}, "another distribution"),
        ({"dist": None}, "no distribution"),
        (
            {"value": "evil.evaluation:EVALUATION", "loads": lambda: EVALUATION},
            "a module outside meridian.workloads",
        ),
        ({"loads": lambda: 3}, "an object that is not an evaluation"),
        ({"loads": lambda: SimpleNamespace(workload="x")}, "half a protocol"),
    ],
)
def test_an_entry_that_is_not_trusted_is_refused(
    monkeypatch: pytest.MonkeyPatch, changes: dict[str, Any], why: str
) -> None:
    publish(monkeypatch, entry(**changes))

    with pytest.raises(ReportError):
        load_evaluation("claims-triage")


def test_an_entry_that_fails_to_import_is_refused_without_its_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken() -> object:
        raise ImportError("secret-path-in-the-message")

    publish(monkeypatch, entry(loads=broken))

    with pytest.raises(ReportError) as refused:
        load_evaluation("claims-triage")

    assert "secret-path" not in str(refused.value)


def test_a_name_published_twice_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    publish(monkeypatch, entry(), entry())

    with pytest.raises(ReportError):
        load_evaluation("claims-triage")


def test_a_plugin_from_a_file_outside_the_package_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    publish(monkeypatch, entry())
    monkeypatch.setattr(workload, "TRUSTED_ROOT", tmp_path)

    with pytest.raises(ReportError):
        load_evaluation("claims-triage")


def test_an_untrusted_name_beside_the_trusted_one_is_not_listed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publish(monkeypatch, entry(), entry("planted", dist="someone-else"))

    with pytest.raises(ReportError) as refused:
        load_evaluation("other")

    assert "claims-triage" in str(refused.value)
    assert "planted" not in str(refused.value)


# ── a run over the real services, in process ────────────────────────────────
class FirstThree:
    """The claims plugin, restricted to the first three golden claims."""

    workload = EVALUATION.workload
    answer_path = staticmethod(EVALUATION.answer_path)
    report = staticmethod(EVALUATION.report)

    def submissions(self, golden_set: Path) -> Sequence[Submission]:
        return EVALUATION.submissions(golden_set)[:3]


def test_three_golden_claims_through_the_stack_grade_true_and_a_second_run_skips_them(
    fresh_database: DatabaseHandle,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = ScriptedModel(golden_answer)
    stack = build_stack(fresh_database, runtime_http=model.http())

    def advance(_: float) -> None:
        stack.clock.advance(WINDOW_SECONDS)

    outcome = run_cases(
        stack.client, EVALUATION, SYNTHETIC_DIR, limit=3, pace=10.0, sleep=advance
    )

    assert outcome.ran == FIRST_THREE
    assert outcome.skipped == outcome.failed == ()
    report = EVALUATION.report(outcome.answers, SYNTHETIC_DIR, REGISTRY)
    assert [case.case for case in report.cases] == list(FIRST_THREE)
    for case in report.cases:
        assert tuple(case.grades) == RULE_GRADERS
        assert all(case.grades.values()), (case.case, case.grades)
    (stored,) = owner_rows(
        fresh_database,
        "SELECT count(*) FROM claims.claims WHERE claim_id = ANY(%s)",
        (list(FIRST_THREE),),
    )
    assert stored == (3,)

    # The same three again: the stack has them, so nothing runs. (A limit alone
    # would move on to the next three: skipped cases do not count against it.)
    first_three = FirstThree()
    again = run_cases(
        stack.client, first_three, SYNTHETIC_DIR, limit=None, pace=0, sleep=advance
    )
    assert again.ran == ()
    assert again.skipped == FIRST_THREE

    # And through the command.
    monkeypatch.setattr(cli, "new_http_client", lambda base_url: stack.client)
    monkeypatch.setattr(cli, "load_evaluation", lambda name: first_three)
    out = tmp_path / "report.json"
    result = runner.invoke(
        app,
        [
            "eval", "run", "--base-url", "http://stack.test", "--report", str(out),
            "--golden-set", str(SYNTHETIC_DIR), "--registry", str(REGISTRY_DIR),
            "--pace", "0",
        ],
    )  # fmt: skip
    assert result.exit_code == 1, result.output
    assert "nothing ran: every case is already on the stack" in result.stderr
    assert "ran 0, skipped 3 (already on the stack), failed 0" in result.stdout
    assert not out.exists()


def test_the_report_of_a_run_is_written_and_reads_back(
    fresh_database: DatabaseHandle,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stack = build_stack(
        fresh_database, runtime_http=ScriptedModel(golden_answer).http()
    )
    monkeypatch.setattr(cli, "new_http_client", lambda base_url: stack.client)
    monkeypatch.setattr(cli, "sleep", lambda _: stack.clock.advance(WINDOW_SECONDS))
    out = tmp_path / "report.json"

    result = runner.invoke(
        app,
        [
            "eval", "run", "--base-url", "http://stack.test", "--report", str(out),
            "--golden-set", str(SYNTHETIC_DIR), "--registry", str(REGISTRY_DIR),
            "--limit", "2", "--pace", "10",
        ],
    )  # fmt: skip

    assert result.exit_code == 0, result.output
    assert (
        result.stdout.splitlines()[0]
        == "ran 2, skipped 0 (already on the stack), failed 0"
    )
    assert "answered by replay (simulated)" in result.stdout
    assert "completed: 2/2" in result.stdout.splitlines()
    assert "partial run" in result.stdout
    assert [case.case for case in load_report(out).cases] == ["CLM-0001", "CLM-0002"]
