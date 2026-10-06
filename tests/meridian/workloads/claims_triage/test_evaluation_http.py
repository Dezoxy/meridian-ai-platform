"""The claims workload's plugin for ``meridian eval run`` (S050, T-80): what it
submits, where it reads the answers, how it grades them and who answered; the
platform's loader that finds it; and one run over the real services in process.
"""

import hashlib
import json
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
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
from meridian.workloads.claims_triage.evaluation_http import (
    CLAIM_ID,
    EVALUATION,
    FILES_DISAGREE,
    NOT_A_CLAIM_ID,
)
from meridian.workloads.claims_triage.models import ClaimFacts

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

    assert len(submissions) == len(CLAIMS) == 47
    assert [s.case for s in submissions] == sorted(c["claim_id"] for c in CLAIMS)
    assert {s.path for s in submissions} == {"/claims"}
    by_id = {c["claim_id"]: c for c in CLAIMS}
    assert all(dict(s.body) == by_id[s.case] for s in submissions)
    assert all(isinstance(s, Submission) for s in submissions)


def test_the_answer_of_a_case_is_read_under_the_adjusters_path() -> None:
    assert EVALUATION.answer_path("CLM-0007") == "/adjuster/claims/CLM-0007/proposal"


def test_an_answer_names_its_case_in_the_claim_id_field() -> None:
    assert EVALUATION.case_field == "claim_id"


# ── a claim ID goes into a path, a log line and a CI annotation (T-80) ──────
CRAFTED_IDS = [
    pytest.param("CLM-0001/../../claimant/claims/CLM-0001", id="a-path-climb"),
    pytest.param("CLM-0001?x=1", id="a-query"),
    pytest.param("CLM-0001#frag", id="a-fragment"),
    pytest.param("CLM-0001\n::error::x", id="a-workflow-command"),
    pytest.param("CLM-0001\n", id="a-trailing-newline"),
    pytest.param("CLM-0001 ", id="a-trailing-space"),
    pytest.param("clm-0001", id="lower-case"),
    pytest.param("CLM-00001", id="five-digits"),
    pytest.param("CLM-001", id="three-digits"),
    pytest.param("CLM-١٢٣٤", id="arabic-indic-digits"),
    pytest.param("", id="empty"),
]


def claims_file(directory: Path, *claim_ids: str) -> None:
    records = [{"claim_id": claim_id} for claim_id in claim_ids]
    (directory / "claims.json").write_text(json.dumps(records), encoding="utf-8")


def test_the_claim_id_shape_is_the_one_the_claim_model_takes() -> None:
    # evaluation_http repeats the pattern of ClaimFacts.claim_id.
    patterns = [
        part.pattern
        for part in ClaimFacts.model_fields["claim_id"].metadata
        if hasattr(part, "pattern")
    ]

    assert patterns == [f"^{CLAIM_ID.pattern}$"]


@pytest.mark.parametrize("claim_id", ["CLM-0000", "CLM-0001", "CLM-9999"])
def test_a_claim_id_of_the_shape_is_accepted(tmp_path: Path, claim_id: str) -> None:
    claims_file(tmp_path, claim_id)

    (submission,) = EVALUATION.submissions(tmp_path)

    assert submission.case == claim_id
    assert EVALUATION.answer_path(claim_id) == f"/adjuster/claims/{claim_id}/proposal"


def test_every_claim_id_of_the_golden_set_has_the_shape() -> None:
    assert all(CLAIM_ID.fullmatch(c["claim_id"]) for c in CLAIMS)


@pytest.mark.parametrize("claim_id", CRAFTED_IDS)
def test_a_claim_with_an_id_of_another_shape_is_refused_without_quoting_it(
    tmp_path: Path, claim_id: str
) -> None:
    claims_file(tmp_path, "CLM-0001", claim_id)

    with pytest.raises(ReportError) as refused:
        EVALUATION.submissions(tmp_path)

    # The message is one fixed sentence: it can quote nothing of the ID.
    assert str(refused.value) == NOT_A_CLAIM_ID


@pytest.mark.parametrize("claim_id", CRAFTED_IDS)
def test_the_path_of_an_answer_is_never_built_from_an_id_of_another_shape(
    claim_id: str,
) -> None:
    with pytest.raises(ReportError) as refused:
        EVALUATION.answer_path(claim_id)

    assert str(refused.value) == NOT_A_CLAIM_ID


@pytest.mark.parametrize("claim_id", CRAFTED_IDS)
def test_the_command_sends_nothing_and_prints_nothing_of_a_crafted_claim_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, claim_id: str
) -> None:
    claims_file(tmp_path, "CLM-0001", claim_id)
    manifest = {
        "workload": "claims-triage",
        "generator_version": "1",
        "seed": 7,
        "files": {"claims.json": sha256_of(tmp_path / "claims.json")},
    }
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    requests: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(f"{request.method} {request.url}")
        return httpx.Response(201)

    monkeypatch.setattr(
        cli,
        "new_http_client",
        lambda base_url: httpx.Client(
            base_url=base_url, transport=httpx.MockTransport(respond)
        ),
    )
    monkeypatch.setattr(cli, "load_evaluation", lambda name: EVALUATION)

    result = runner.invoke(
        app,
        [
            "eval", "run", "--base-url", "http://stack.test",
            "--report", str(tmp_path / "report.json"),
            "--golden-set", str(tmp_path), "--registry", str(REGISTRY_DIR),
            "--pace", "0",
        ],
    )  # fmt: skip

    assert result.exit_code == 2, result.output
    assert requests == []
    assert result.stdout == ""
    assert result.stderr == f"ERROR the golden set: {NOT_A_CLAIM_ID}\n"
    assert not (tmp_path / "report.json").exists()


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


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


def golden_copy_without_policy_of(directory: Path, claim_id: str) -> None:
    """The golden set's three files, with the policy of ``claim_id`` left out."""
    number = next(c for c in CLAIMS if c["claim_id"] == claim_id)["policy_number"]
    for name in ("claims.json", "expected-outcomes.json"):
        (directory / name).write_bytes((SYNTHETIC_DIR / name).read_bytes())
    policies = json.loads((SYNTHETIC_DIR / "policies.json").read_text("utf-8"))
    kept = [p for p in policies if p["policy_number"] != number]
    assert len(kept) == len(policies) - 1
    (directory / "policies.json").write_text(json.dumps(kept), encoding="utf-8")


def test_a_claim_on_no_policy_is_graded_when_the_label_says_so() -> None:
    unknown = next(
        c["claim_id"]
        for c in CLAIMS
        if c["policy_number"]
        not in {
            p["policy_number"]
            for p in json.loads((SYNTHETIC_DIR / "policies.json").read_text("utf-8"))
        }
    )

    (case,) = report_of({unknown: answer(unknown, drafted("replay"))}).cases

    assert case.case == unknown
    assert case.grades["completed"] is True
    assert case.grades["reason"] is False  # the stand-in proposal is not this claim's


def test_a_claim_with_no_policy_and_another_reason_is_files_disagree_not_a_key_error(
    tmp_path: Path,
) -> None:
    golden_copy_without_policy_of(tmp_path, "CLM-0001")
    answers = {"CLM-0001": answer("CLM-0001", drafted("replay"))}

    with pytest.raises(ReportError) as raised:
        EVALUATION.report(answers, tmp_path, REGISTRY)

    assert str(raised.value) == FILES_DISAGREE


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


def test_the_import_error_is_the_cause_of_the_refusal_though_not_its_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failure = ImportError("secret-path-in-the-message")

    def broken() -> object:
        raise failure

    publish(monkeypatch, entry(loads=broken))

    with pytest.raises(ReportError) as refused:
        load_evaluation("claims-triage")

    assert str(refused.value) == workload.UNLOADABLE
    assert refused.value.__cause__ is failure


def test_a_refusal_the_wording_does_not_know_is_an_error_not_silence() -> None:
    unknown = SimpleNamespace(reason="a reason added later", known=())

    with pytest.raises(AssertionError):
        workload._fixed_text(unknown)


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


class Loads:
    """An entry point's ``load`` that says whether it was called."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> object:
        self.calls += 1
        return EVALUATION


def test_a_plugin_from_a_file_outside_the_package_is_refused_before_it_runs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    loads = Loads()
    publish(monkeypatch, entry(loads=loads))
    monkeypatch.setattr(workload, "TRUSTED_ROOT", tmp_path)

    with pytest.raises(ReportError) as refused:
        load_evaluation("claims-triage")

    assert str(refused.value) == workload.UNTRUSTED
    assert loads.calls == 0


@pytest.mark.parametrize(
    "value",
    [
        # find_spec finds no module of this name.
        "meridian.workloads.no_such_module:EVALUATION",
        # ... and cannot even import the parent of this one.
        "meridian.workloads.no_such_package.module:EVALUATION",
        # A name that is not a module name at all.
        "meridian.workloads..module:EVALUATION",
    ],
)
def test_a_plugin_whose_module_cannot_be_located_is_refused_before_it_runs(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    loads = Loads()
    publish(monkeypatch, entry(value=value, loads=loads))

    with pytest.raises(ReportError):
        load_evaluation("claims-triage")

    assert loads.calls == 0


def test_a_plugin_whose_module_has_no_file_is_refused_before_it_runs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A namespace package (a directory with no __init__.py) has no origin file.
    package = tmp_path / "namespace_plugin"
    package.mkdir()
    monkeypatch.syspath_prepend(str(tmp_path))
    loads = Loads()
    publish(monkeypatch, entry(value="namespace_plugin:EVALUATION", loads=loads))
    monkeypatch.setattr(workload, "TRUSTED_VALUE_PREFIX", "namespace_plugin")

    with pytest.raises(ReportError) as refused:
        load_evaluation("claims-triage")

    assert str(refused.value) == workload.UNTRUSTED
    assert loads.calls == 0


def test_a_plugin_inside_the_package_loads_after_the_location_is_checked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loads = Loads()
    publish(monkeypatch, entry(loads=loads))

    assert load_evaluation("claims-triage") is EVALUATION
    assert loads.calls == 1


def test_the_location_is_checked_again_after_the_plugin_loaded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The first check reads the module's spec; the second reads what loaded. A
    # module that is somewhere else once loaded is refused.
    name = "meridian.workloads.claims_triage.evaluation_http"
    checked: list[str] = []

    def moved() -> object:
        checked.append("after-load")
        monkeypatch.setitem(
            sys.modules, name, SimpleNamespace(__file__=str(tmp_path / "moved.py"))
        )
        return EVALUATION

    publish(monkeypatch, entry(loads=moved))

    with pytest.raises(ReportError) as refused:
        load_evaluation("claims-triage")

    assert str(refused.value) == workload.UNTRUSTED
    assert checked == ["after-load"]


def test_loading_the_claims_evaluation_brings_in_no_agent_framework() -> None:
    # The plugin seam runs workload and runtime code inside the platform's CLI
    # process. That is its purpose; the agent framework must not ride along.
    code = (
        "import sys\n"
        "from meridian.platform.evaluation.workload import load_evaluation\n"
        "load_evaluation('claims-triage')\n"
        "framework = sorted(\n"
        "    name for name in sys.modules\n"
        "    if name in ('langgraph', 'langgraph_sdk', 'langchain')\n"
        "    or name.startswith(('langgraph.', 'langchain'))\n"
        ")\n"
        "print('framework:' + ','.join(framework))\n"
    )

    completed = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "framework:", completed.stdout


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
    case_field = EVALUATION.case_field
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
