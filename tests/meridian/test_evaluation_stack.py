"""S050's done-when: the golden set is answered by a real model's recorded
answers, through the Model Gateway in recorded mode, and graded by the claims
workload (rule graders, an LLM judge's groundedness, the cost).

CI replays ``data/evaluation/recordings/claims-triage.json`` and compares the
report with the committed baseline. The recording is made on a laptop, with an
``az login``, by the two opt-in tests at the end (``make eval-record``): they
spend about EUR 0.50 and rewrite the files under ``data/evaluation/``. Nothing
else here reaches Azure, and CI never sets the variable that enables them.

The files those tests write do not exist until someone records. The tests that
read them fail with the instruction to record, not with an error from a parser.
"""

import json
import os
import re
from pathlib import Path

import pytest
from dbsupport import DatabaseHandle
from evalsupport import (
    BASELINE_PATH,
    COMPARISON_PATH,
    LIVE,
    LIVE_REPORT_PATH,
    RECORD_COMMAND,
    RECORDED,
    RECORDING_PATH,
    VARIANT_REPORT_PATH,
    VARIANT_SYSTEM_MESSAGE,
    another_database,
    call_lines,
    chat_tokens,
    live_recording_gateway,
    measure_claims,
    record_run,
    recorded_for,
    recorded_gateway,
    recording_problems,
    recording_sha256,
    report_of,
    run_evaluation,
    totals_line,
)
from servicesupport import FakeClock, owner_rows
from stacksupport import (
    CLAIMS,
    WINDOW_SECONDS,
    build_stack,
    circumstance_clause,
    claims_that_ask_the_model,
    golden_answer,
    stored_proposals,
)

from meridian.platform.evaluation.compare import compare
from meridian.platform.evaluation.diff import diff_reports, render_markdown
from meridian.platform.evaluation.judge import JUDGE_PROMPT_VERSION
from meridian.platform.evaluation.report import ReportError, load_report, write_report
from meridian.platform.gateway.models import ChatRequest
from meridian.platform.gateway.providers.base import (
    EmbeddingReply,
    ProviderError,
    ProviderReply,
)
from meridian.platform.gateway.providers.recorded import (
    RecordedProvider,
    Recording,
    RecordingError,
    load_recording,
    write_recording,
)
from meridian.platform.guardrails import holds_special_category, screen_fingerprint
from meridian.workloads.claims_triage import assessment
from meridian.workloads.claims_triage.evaluation import RULE_GRADERS

PROMPT_LABEL = "claims-triage"
JUDGE_LABEL = "evaluation-judge"
DIGEST_CHARS = 12
FAKE_INPUT_TOKENS = 700
FAKE_OUTPUT_TOKENS = 90
RECORD_ENV = "MERIDIAN_EVAL_RECORD"
OPT_IN = pytest.mark.skipif(
    os.environ.get(RECORD_ENV) != "1",
    reason=f"opt-in: set {RECORD_ENV}=1 (make eval-record)",
)


def committed_recording() -> Recording:
    """The committed recording, or a failure that says how to make one."""
    if not RECORDING_PATH.is_file():
        pytest.fail(f"no recording: {RECORD_COMMAND}")
    try:
        return load_recording(RECORDING_PATH)
    except RecordingError as error:
        pytest.fail(f"the recording cannot be read ({error}): {RECORD_COMMAND}")


def short(digest: str | None) -> str:
    return (digest or "none")[:DIGEST_CHARS]


# ── 1. the CI evaluation ────────────────────────────────────────────────────
def test_the_recorded_model_answers_the_golden_set_through_the_gateway(
    fresh_database: DatabaseHandle, tmp_path: Path
) -> None:
    recording = committed_recording()
    provider = RecordedProvider(recording)
    second = recorded_gateway(fresh_database, provider)
    stack = build_stack(fresh_database, runtime_http=second.http)

    evaluation = run_evaluation(
        stack, second, kind="recorded", pace=second.clock.advance
    )

    # The report is written first, so a failing run leaves one to read.
    report = report_of(
        evaluation,
        second.registry,
        answered_by=RECORDED,
        recording_fingerprint=recording_sha256(),
    )
    named = os.environ.get("MERIDIAN_EVAL_REPORT")
    destination = Path(named) if named else tmp_path / "claims-triage-report.json"
    write_report(report, destination)
    missed = set(provider.missed)
    assert not missed, (
        f"{len(missed)} requests have no recording. It was recorded for prompt "
        f"{short(recording.recorded_for.get(PROMPT_LABEL))} and judge "
        f"{short(recording.recorded_for.get(JUDGE_LABEL))}; the tree has "
        f"{short(assessment.PROMPT_VERSION)} and {short(JUDGE_PROMPT_VERSION)}: "
        "run make eval-record"
    )
    unused = provider.unused()
    assert not unused, (
        f"{len(unused)} recorded answers were not asked for: run make eval-record"
    )
    assert all(evaluation.proposals.values()), sorted(
        c for c, p in evaluation.proposals.items() if p is None
    )
    # The claims the model is not asked about equal the oracle: if one does not,
    # the pipeline changed, and no recording can excuse it.
    cases = {case.case: case for case in report.cases}
    for claim_id in sorted(set(CLAIMS) - claims_that_ask_the_model()):
        failed = [n for n in RULE_GRADERS if not cases[claim_id].grades[n]]
        assert not failed, (
            f"{claim_id} does not equal the oracle's proposal ({failed}): the "
            "pipeline changed, not the model"
        )
    assert evaluation.dropped == frozenset(), sorted(evaluation.dropped)
    if destination.resolve() != BASELINE_PATH.resolve():
        try:
            baseline = load_report(BASELINE_PATH)
        except ReportError as error:
            pytest.fail(
                f"the baseline cannot be read ({error}): run make eval-baseline"
            )
        outcome = compare(baseline, report)
        assert outcome.passed, (
            f"the report differs from the baseline: {outcome.problems} "
            f"{outcome.regressions}; if the change is intended, review it and "
            "run make eval-baseline"
        )


# ── 2. a recording replays ──────────────────────────────────────────────────
class FakeModel:
    """The provider behind a recording gateway in the replay test: it finds the
    claim from the description in the request and answers as the oracle's model
    does, with fixed token counts."""

    def __init__(self) -> None:
        self.asked: list[str] = []
        self.judged = 0

    def chat(self, deployment, request: ChatRequest, *, timeout_seconds: float):
        document = json.loads(request.messages[1].content)
        if "statement" in document:  # the judge's question
            self.judged += 1
            return self._reply(
                json.dumps({"grounded": True, "reason": "The source states it."})
            )
        (claim_id,) = [
            c for c, v in CLAIMS.items() if v["description"] == document["description"]
        ]
        self.asked.append(claim_id)
        return self._reply(golden_answer(claim_id))

    @staticmethod
    def _reply(text: str) -> ProviderReply:
        return ProviderReply(
            text=text,
            finish_reason="stop",
            model="fake-model",
            input_tokens=FAKE_INPUT_TOKENS,
            output_tokens=FAKE_OUTPUT_TOKENS,
        )

    def embed(self, deployment, request, *, timeout_seconds: float) -> EmbeddingReply:
        raise ProviderError("unavailable")


def three_claims_that_ask_the_model() -> list[str]:
    """One claim the oracle excludes by a circumstance and two it does not, all
    of which the model is asked about (not the one whose description holds
    special-category data: no call is made for it)."""
    asking = sorted(
        c
        for c in claims_that_ask_the_model()
        if not holds_special_category(CLAIMS[c]["description"])
    )
    excluded = [c for c in asking if circumstance_clause(c)]
    others = [c for c in asking if not circumstance_clause(c)]
    return [excluded[0], *others[:2]]


def post_claims(stack, claim_ids: list[str], advance) -> list[int]:
    statuses = []
    for claim_id in claim_ids:
        advance(WINDOW_SECONDS)
        statuses.append(stack.post(CLAIMS[claim_id]).status_code)
    return statuses


def test_a_recording_replays_to_the_same_proposals(
    fresh_database: DatabaseHandle, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    claim_ids = three_claims_that_ask_the_model()
    path = tmp_path / "recording.json"
    fake = FakeModel()
    gateway = live_recording_gateway(fresh_database, inner=fake)
    try:
        stack = build_stack(fresh_database, runtime_http=gateway.http)
        statuses = post_claims(stack, claim_ids, lambda seconds: None)
    finally:
        gateway.close()
    assert statuses == [201, 201, 201]
    assert fake.asked == claim_ids
    recorded = stored_proposals(fresh_database)
    write_recording(gateway.recording.recording(recorded_for()), path)

    with another_database(fresh_database) as replay_database:
        provider = RecordedProvider(load_recording(path))
        second = recorded_gateway(replay_database, provider)
        stack = build_stack(replay_database, runtime_http=second.http)
        statuses = post_claims(stack, claim_ids, second.clock.advance)
        replayed = stored_proposals(replay_database)
        measured = measure_claims(replay_database, second.registry, kind="recorded")
        tokens = chat_tokens(replay_database, second.registry)

    assert statuses == [201, 201, 201]
    assert set(replayed) == set(recorded) == set(claim_ids)
    for claim_id in claim_ids:
        was, now = recorded[claim_id], replayed[claim_id]
        assert was.drafted_by is not None
        assert (was.drafted_by.mode, was.drafted_by.deployment) == (
            "live",
            "aoai-sdc-gpt-4o",
        )
        assert now.drafted_by is not None
        assert (now.drafted_by.mode, now.drafted_by.deployment) == (
            "recorded",
            "recorded-chat",
        )
        assert now.model_copy(update={"drafted_by": None}) == was.model_copy(
            update={"drafted_by": None}
        )
        assert measured[claim_id].model_calls == 1
        assert measured[claim_id].latency_ms is None
    assert provider.missed == ()
    assert provider.unused() == ()
    # The ledger holds the tokens the recording says, for the chat call. The
    # claim's own tokens also hold its wording searches' embeddings, which the
    # replay provider counts.
    assert tokens == {c: (FAKE_INPUT_TOKENS, FAKE_OUTPUT_TOKENS) for c in claim_ids}
    for claim_id in claim_ids:
        assert measured[claim_id].output_tokens == FAKE_OUTPUT_TOKENS
        assert measured[claim_id].input_tokens > FAKE_INPUT_TOKENS

    changed = assessment.SYSTEM_MESSAGE.replace(
        "check one thing", "verify one thing", 1
    )
    assert changed != assessment.SYSTEM_MESSAGE
    monkeypatch.setattr(assessment, "SYSTEM_MESSAGE", changed)
    with another_database(fresh_database) as changed_database:
        provider = RecordedProvider(load_recording(path))
        second = recorded_gateway(changed_database, provider)
        stack = build_stack(changed_database, runtime_http=second.http)
        statuses = post_claims(stack, claim_ids, second.clock.advance)
        answered = stored_proposals(changed_database)
        run_statuses = owner_rows(
            changed_database, "SELECT reference, status FROM runtime.runs"
        )

    # No recording answers the changed request: the gateway answers 502, the
    # run fails, and no claim has a proposal. Every key asked is missed, and the
    # recording's own three answers go unasked.
    assert statuses == [502, 502, 502]
    assert sorted(run_statuses) == [(c, "Failed") for c in sorted(claim_ids)]
    assert not answered
    assert len(set(provider.missed)) == 3
    assert len(provider.unused()) == 3


CALL_LINE = re.compile(r"CLM-\d{4}( judge)?: \d+ in, \d+ out, \d+ ms, \d+\.\d tokens/s")


def test_a_fake_model_records_and_replays_the_whole_golden_set(
    fresh_database: DatabaseHandle,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The recording run and the CI run end to end, with a fake behind the
    recording instead of Azure: every golden claim, the judge, the ledger and the
    tool calls, once recorded and once replayed on a fresh database; then the
    variant prompt's run and its comparison."""
    clock = FakeClock()
    fake = FakeModel()
    run = record_run(fresh_database, inner=fake, pace=clock.advance, clock=clock)

    assert recording_problems(run) == []
    assert sorted(fake.asked) == sorted(
        claims_that_ask_the_model() - {c for c in CLAIMS if withheld(c)}
    )
    assert fake.judged == len(run.evaluation.judgements) > 0
    assert len(run.recording.entries) == run.chat_calls == len(fake.asked) + fake.judged
    # What the recording run prints: one line per chat call and the totals.
    print_run(fresh_database, run)
    *calls, total = [line for line in capsys.readouterr().out.splitlines() if line]
    assert len(calls) == run.chat_calls
    assert all(CALL_LINE.fullmatch(line) for line in calls), calls
    assert re.fullmatch(
        r"total: \d+ chat calls, \d+ tokens in, \d+ out, EUR 0\.\d+", total
    )
    path, live_path = tmp_path / "recording.json", tmp_path / "live.json"
    save_golden_set(run, path, live_path)
    live = load_report(live_path)

    with another_database(fresh_database) as replay_database:
        provider = RecordedProvider(load_recording(path))
        second = recorded_gateway(replay_database, provider)
        stack = build_stack(replay_database, runtime_http=second.http)
        evaluation = run_evaluation(
            stack, second, kind="recorded", pace=second.clock.advance
        )
    recorded = report_of(
        evaluation,
        second.registry,
        answered_by=RECORDED,
        recording_fingerprint=recording_sha256(path),
    )

    assert provider.missed == ()
    assert provider.unused() == ()
    assert evaluation.dropped == run.evaluation.dropped == frozenset()
    assert evaluation.judgements == run.evaluation.judgements
    assert evaluation.tools == run.evaluation.tools
    assert all(len(calls) >= 2 for calls in evaluation.tools.values())
    assert recorded.fingerprints.recording == recording_sha256(path)
    assert live.fingerprints.recording is None
    assert recorded.fingerprints.judge == JUDGE_PROMPT_VERSION
    assert live.fingerprints.judge == JUDGE_PROMPT_VERSION
    for was, now in zip(live.cases, recorded.cases, strict=True):
        assert was.case == now.case
        assert was.tools == now.tools
        assert was.observed == now.observed
        # The live run also grades latency, and passes it.
        assert was.grades["latency"] is True
        assert {n: ok for n, ok in was.grades.items() if n != "latency"} == now.grades
        assert was.measured is not None
        assert now.measured == was.measured.model_copy(update={"latency_ms": None})
    # The fake answers as the oracle's model: every grade passes, but the
    # recommendation of a claim the model is due to be asked about and never
    # is (S047, ``withheld``).
    failing = {
        case.case: sorted(name for name, ok in case.grades.items() if not ok)
        for case in recorded.cases
        if not all(case.grades.values())
    }
    assert failing == {c: ["recommendation"] for c in CLAIMS if withheld(c)}

    # The variant prompt, recorded on another database, and its comparison.
    with another_database(fresh_database) as variant_database:
        variant_run = record_variant(
            variant_database, monkeypatch, inner=fake, pace=clock.advance, clock=clock
        )
        assert variant_run.evaluation.proposals.keys() == CLAIMS.keys()
        variant_path = tmp_path / "variant.json"
        comparison = tmp_path / "comparison.md"
        save_variant(variant_run, live_path, variant_path, comparison)
    check_comparison(live_path, variant_path, comparison)
    variant = load_report(variant_path)
    assert variant.fingerprints.prompt != live.fingerprints.prompt
    assert (
        variant.fingerprints.prompt == variant_run.recording.recorded_for[PROMPT_LABEL]
    )
    assert live.fingerprints.prompt == run.recording.recorded_for[PROMPT_LABEL]


def withheld(claim_id: str) -> bool:
    """Whether the claim is one the run would ask the model about (the rules do
    not decide it: ``claims_that_ask_the_model``) and the model is never asked,
    because its description holds special-category data (S047). A claim the
    rules decide is not withheld whatever its description holds: no call was
    due, and its recommendation is the rules' own."""
    return claim_id in claims_that_ask_the_model() and holds_special_category(
        CLAIMS[claim_id]["description"]
    )


# ── 3. the committed recording holds only answers ───────────────────────────
ENTRY_FIELDS = {
    "text",
    "finish_reason",
    "model",
    "input_tokens",
    "output_tokens",
    "latency_ms",
}
# T-78: nothing in the file but what a model answered. The first group is
# matched whatever the case; the scheme "Bearer " is matched as it is written.
FORBIDDEN = {
    "an Azure OpenAI host": re.compile(r"\.openai\.azure\.com", re.IGNORECASE),
    "an account name": re.compile(r"oai-meridian-", re.IGNORECASE),
    "an email address": re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    "a GUID": re.compile(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
        re.IGNORECASE,
    ),
    "a bearer token": re.compile(r"Bearer "),
    "an api-key": re.compile(r"api-key", re.IGNORECASE),
    "an x-ms- header": re.compile(r"x-ms-", re.IGNORECASE),
    "an apim- header": re.compile(r"apim-", re.IGNORECASE),
    "a subscription": re.compile(r"subscription", re.IGNORECASE),
}


def test_the_committed_recording_holds_only_answers() -> None:
    if not RECORDING_PATH.is_file():
        pytest.fail(f"no recording: {RECORD_COMMAND}")
    text = RECORDING_PATH.read_text(encoding="utf-8")
    document = json.loads(text)

    assert set(document) == {"format", "recorded_for", "entries"}
    assert document["entries"], "the recording holds no entry"
    for key, entry in document["entries"].items():
        assert set(entry) == ENTRY_FIELDS, key
    for what, pattern in FORBIDDEN.items():
        assert pattern.search(text) is None, f"the recording holds {what}"


BASELINE_COMMAND = "run make eval-baseline"


@pytest.mark.parametrize(
    ("path", "command"),
    [
        pytest.param(BASELINE_PATH, BASELINE_COMMAND, id="baseline"),
        pytest.param(LIVE_REPORT_PATH, RECORD_COMMAND, id="live-report"),
        pytest.param(VARIANT_REPORT_PATH, RECORD_COMMAND, id="variant-report"),
        pytest.param(COMPARISON_PATH, RECORD_COMMAND, id="prompt-comparison"),
    ],
)
def test_a_committed_report_and_the_comparison_hold_nothing_of_the_service(
    path: Path, command: str
) -> None:
    """The reports hold tool arguments and the comparison holds observed values:
    the same text a recording may not hold is kept out of them too (T-78)."""
    if not path.is_file():
        pytest.fail(f"no {path.name}: {command}")
    text = path.read_text(encoding="utf-8")

    assert text, path.name
    for what, pattern in FORBIDDEN.items():
        assert pattern.search(text) is None, f"{path.name} holds {what}"


def test_the_committed_baseline_carries_the_fingerprint_of_the_screens() -> None:
    if not BASELINE_PATH.is_file():
        pytest.fail(f"no {BASELINE_PATH.name}: {BASELINE_COMMAND}")

    baseline = load_report(BASELINE_PATH)

    assert baseline.fingerprints.screen == screen_fingerprint(), BASELINE_COMMAND


def test_the_forbidden_patterns_find_what_they_name() -> None:
    # A pattern that cannot match proves nothing: each one finds its own sample.
    samples = {
        "an Azure OpenAI host": "https://x.OpenAI.Azure.com/openai",
        "an account name": "OAI-MERIDIAN-dev",
        "an email address": "a.b+c@example.org",
        "a GUID": "12345678-1234-1234-1234-1234567890AB",
        "a bearer token": "Authorization: Bearer abc",
        "an api-key": "API-Key: abc",
        "an x-ms- header": "X-MS-request-id",
        "an apim- header": "Apim-Request-Id",
        "a subscription": "the Subscription id",
    }

    assert set(samples) == set(FORBIDDEN)
    for what, sample in samples.items():
        assert FORBIDDEN[what].search(sample) is not None, what


# ── 4. the prompt comparison is the diff of the two live reports ────────────
def check_comparison(live_path: Path, variant_path: Path, comparison: Path) -> None:
    """The comparison file is the diff of the two live reports, which differ in
    the prompt alone. A missing file is a failure that says how to record."""
    for path in (live_path, variant_path, comparison):
        if not path.is_file():
            pytest.fail(f"no {path.name}: {RECORD_COMMAND}")
    live, variant = load_report(live_path), load_report(variant_path)

    assert live.fingerprints.prompt != variant.fingerprints.prompt
    assert live.fingerprints.golden_set == variant.fingerprints.golden_set
    assert live.fingerprints.tools == variant.fingerprints.tools
    for report in (live, variant):
        assert (report.answered_by.kind, report.answered_by.label) == ("live", "real")
    assert comparison.read_text(encoding="utf-8") == render_markdown(
        diff_reports(live, variant)
    )


def test_the_prompt_comparison_is_the_diff_of_the_two_live_reports() -> None:
    check_comparison(LIVE_REPORT_PATH, VARIANT_REPORT_PATH, COMPARISON_PATH)


# ── 5. what the recording runs write ────────────────────────────────────────
# The two opt-in tests at the end call these with the repository's paths; the
# fake-model test above calls them with its own, so they run in CI.
def print_run(db: DatabaseHandle, run) -> None:
    """Per model call the claim, the tokens, the latency and the rate; then the
    totals and the cost. Nothing else."""
    print()
    for line in call_lines(db, run):
        print(line)
    print(totals_line(db, run))


def refuse_to_write(run) -> None:
    problems = recording_problems(run)
    assert not problems, f"nothing was written: {'; '.join(problems)}"


def save_golden_set(run, recording_path: Path, live_report_path: Path) -> None:
    """The recording and the live report, only if the run may be written."""
    refuse_to_write(run)
    recording_path.parent.mkdir(parents=True, exist_ok=True)
    write_recording(run.recording, recording_path)
    live = report_of(
        run.evaluation, run.registry, answered_by=LIVE, recording_fingerprint=None
    )
    write_report(live, live_report_path)


def record_variant(db: DatabaseHandle, patch: pytest.MonkeyPatch, **options):
    """The recording run under the variant prompt: the system message is swapped
    and the prompt version recomputed from it, for the duration of ``patch``."""
    patch.setattr(assessment, "SYSTEM_MESSAGE", VARIANT_SYSTEM_MESSAGE)
    patch.setattr(assessment, "PROMPT_VERSION", assessment._prompt_version())
    return record_run(db, **options)


def save_variant(run, live_path: Path, variant_path: Path, comparison: Path) -> None:
    """The variant's live report and the comparison with the golden set's, only
    if the run may be written. The recording is not kept."""
    refuse_to_write(run)
    variant = report_of(
        run.evaluation, run.registry, answered_by=LIVE, recording_fingerprint=None
    )
    write_report(variant, variant_path)
    comparison.write_text(
        render_markdown(diff_reports(load_report(live_path), variant)),
        encoding="utf-8",
    )


@OPT_IN
def test_record_the_golden_set_with_the_live_model(
    fresh_database: DatabaseHandle,
) -> None:
    run = record_run(fresh_database)

    print_run(fresh_database, run)
    save_golden_set(run, RECORDING_PATH, LIVE_REPORT_PATH)


@OPT_IN
def test_record_the_variant_prompt_with_the_live_model(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    if not LIVE_REPORT_PATH.is_file():
        pytest.fail(f"no {LIVE_REPORT_PATH.name} to compare with: {RECORD_COMMAND}")
    run = record_variant(fresh_database, monkeypatch)

    print_run(fresh_database, run)
    save_variant(run, LIVE_REPORT_PATH, VARIANT_REPORT_PATH, COMPARISON_PATH)
