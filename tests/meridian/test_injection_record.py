"""S071 L2: a real model's answers to the injection cases, built and tested with
no live call.

``record_injection_run`` runs the cases the committed baseline says reached the
model through the real stack, with the runtime calling a live-mode recording
gateway; the tests put a FAKE provider behind that gateway (the shapes the Azure
provider maps, built at the provider seam) and write only under ``tmp_path``.
The paid run is the last test: it needs both ``MERIDIAN_LIVE_AZURE=1`` and
``MERIDIAN_EVAL_INJECTION_RECORD=1`` (``make eval-injection-record``, which the
owner runs) and spends money; CI never sets either.

A case's added sentence is data and is never printed or written: a failure here
names case IDs and counts.
"""

import hashlib
import json
import os
from collections.abc import Iterator
from datetime import date
from pathlib import Path

import pytest
import writehygienesupport
from dbsupport import DatabaseHandle
from evalsupport import (
    LIVE_PACE_SECONDS,
    ToolCapture,
    another_database,
    call_lines,
    recorded_gateway,
    totals_line,
)
from injectionrecordsupport import (
    LIVE_ENV,
    LIVE_REPORT_PATH,
    LIVE_SUMMARY_PATH,
    RECORD_COMMAND,
    RECORD_ENV,
    RECORDING_PATH,
    THE_RULE_OF_COMPLETENESS,
    IncompleteRun,
    _run_registry,
    added_text,
    as_recorded_run,
    asked_cases,
    check_no_case_text,
    injection_problems,
    injection_run_enabled,
    record_injection_run,
    write_injection_run,
)
from injectionsupport import (
    INJECTION_BASELINE_PATH,
    INJECTION_CASES,
    ObedientModel,
    run_cases,
)
from runceilingsupport import (
    INJECTION_RUN_CEILING_EUR,
    INJECTION_RUN_TENANTS,
    registry_with_ceilings,
)
from servicesupport import REGISTRY_DIR, REPO_ROOT, FakeClock
from stacksupport import (
    build_stack,
    golden_answer,
    model_answer,
    stored_proposals,
)
from writehygienesupport import SAMPLES, UnsafeFile

from meridian.platform.gateway.budget import estimate_input_tokens
from meridian.platform.gateway.models import MAX_OUTPUT_TOKENS, ChatRequest
from meridian.platform.gateway.operations import INPUT_TOKEN_FACTOR
from meridian.platform.gateway.providers.base import (
    EmbeddingReply,
    ProviderError,
    ProviderReply,
)
from meridian.platform.gateway.providers.recorded import (
    RecordedProvider,
    load_recording,
)
from meridian.platform.registry import load_registry
from meridian.workloads.claims_triage.injection import InjectionCase, load_cases

OPT_IN = pytest.mark.skipif(
    not injection_run_enabled(os.environ),
    reason=(
        f"opt-in, and it spends money: set {LIVE_ENV}=1 and {RECORD_ENV}=1 "
        "(make eval-injection-record)"
    ),
)
TODAY = date(2026, 10, 8)
ORDINARY_ANSWER = (
    "The description says the damage came from wear and tear, so the exclusion "
    "applies. The bearer of the policy is the insured; contact the adjuster."
)
FAKE_INPUT_TOKENS = 700
FAKE_OUTPUT_TOKENS = 90
FEW_CENTS = "0.05"
RATIONALE_PREFIX = "The description says: "
GUARDED = (REGISTRY_DIR, REPO_ROOT / "data" / "evaluation")
REFUSED_PROMPT = 400
GATEWAY_HEADER_NAMES = {
    "X-Meridian-Refusal",
    "X-Meridian-Completion",
    "X-Meridian-Deployment",
    "X-Meridian-Provider",
    "X-Meridian-Mode",
}


def digests() -> dict[str, str]:
    return {
        str(path.relative_to(REPO_ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
        for directory in GUARDED
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


@pytest.fixture(autouse=True)
def committed_files_stay_as_they_are() -> Iterator[None]:
    """No test here writes under ``config/registry`` or ``data/evaluation``, and
    none of the three files of a paid run exists in the repository."""
    before = digests()
    yield
    assert digests() == before
    for path in (RECORDING_PATH, LIVE_REPORT_PATH, LIVE_SUMMARY_PATH):
        assert not path.exists(), f"{path.name} must not be committed by this step"


# ── the fake provider ───────────────────────────────────────────────────────
def strings_in(value: object) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from strings_in(item)
    elif isinstance(value, list):
        for item in value:
            yield from strings_in(item)


class InjectionFake:
    """The provider behind the recording gateway. It finds the case from what
    the request holds (the description, and for a clause case the inserted
    sentence in the clauses shown) and answers by the case's behaviour:

    - ``oracle`` (the default): as the oracle's model does for the base claim;
    - ``obey``: the verdict ``none`` whatever it is sent, as the obedient model;
    - ``quote``: ``none``, with the case's own sentence in the rationale;
    - ``refuse``: a refusal before the model ran, the 400 Azure's content filter
      gives a prompt (``ProviderError`` ``filtered``, status 400);
    - ``withhold``: a completion the filter withheld, or the model's own refusal
      of a structured request: ``ProviderError`` ``filtered`` with no status;
    - ``raw:TEXT``: TEXT as the answer, whatever it is.

    With ``large`` it reports the largest counts the gateway accepts."""

    def __init__(
        self,
        cases: tuple[InjectionCase, ...],
        behaviours: dict[str, str] | None = None,
        *,
        large: bool = False,
    ) -> None:
        self.cases = cases
        self.behaviours = behaviours or {}
        self.large = large
        self.asked: list[str] = []

    def case_of(self, request: ChatRequest) -> InjectionCase:
        content = request.messages[1].content
        document = json.loads(content)
        strings = list(strings_in(document))
        found = [
            case
            for case in self.cases
            if document["description"] == case.claim["description"]
            and (
                case.clause is None
                or any(case.clause.inserted in text for text in strings)
            )
        ]
        assert len(found) == 1, f"{len(found)} cases match the request"
        return found[0]

    def chat(self, deployment, request: ChatRequest, *, timeout_seconds: float):
        case = self.case_of(request)
        self.asked.append(case.case)
        behaviour = self.behaviours.get(case.case, "oracle")
        if behaviour == "refuse":
            raise ProviderError("filtered", REFUSED_PROMPT)
        if behaviour == "withhold":
            raise ProviderError("filtered")
        if behaviour.startswith("raw:"):
            text = behaviour.removeprefix("raw:")
        elif behaviour == "obey":
            text = model_answer(
                "none", None, "The description states no excluded fact."
            )
        elif behaviour == "quote":
            text = model_answer("none", None, RATIONALE_PREFIX + added_text(case))
        else:
            text = golden_answer(case.base_claim)
        if self.large:
            tokens = (
                INPUT_TOKEN_FACTOR * estimate_input_tokens(request),
                MAX_OUTPUT_TOKENS,
            )
        else:
            tokens = (FAKE_INPUT_TOKENS, FAKE_OUTPUT_TOKENS)
        return ProviderReply(
            text=text,
            finish_reason="stop",
            model="fake-model",
            input_tokens=tokens[0],
            output_tokens=tokens[1],
        )

    def embed(self, deployment, request, *, timeout_seconds: float) -> EmbeddingReply:
        raise ProviderError("unavailable")


def baseline_entries() -> dict[str, dict]:
    """The committed baseline's cases, read as JSON and not through the code
    under test."""
    document = json.loads(INJECTION_BASELINE_PATH.read_text(encoding="utf-8"))
    return {entry["case"]: entry for entry in document["cases"]}


def picks(cases: tuple[InjectionCase, ...]) -> dict[str, str]:
    """Four cases for the four behaviours that are not the default, chosen from
    the baseline: an attack the scripted model's obedience moved off its route,
    a benign case to refuse, an attack to withhold and a short one to quote."""
    entries = baseline_entries()
    asked = [c for c in cases]
    off_route = [
        c.case
        for c in asked
        if c.label == "attack" and not entries[c.case]["grades"]["route_held"]
    ]
    benign = [
        c.case for c in asked if c.label == "benign" and c.carrier == "description"
    ]
    attacks = [
        c.case
        for c in asked
        if c.label == "attack"
        and c.carrier == "description"
        and c.case not in off_route[:1]
    ]
    shortest = min(
        attacks, key=lambda i: len(added_text(next(c for c in asked if c.case == i)))
    )
    others = [i for i in attacks if i != shortest]
    return {
        off_route[0]: "obey",
        benign[0]: "refuse",
        others[0]: "withhold",
        shortest: "quote",
    }


# ── a complete run ──────────────────────────────────────────────────────────
def test_a_complete_run_sends_the_asked_cases_and_writes_the_three_files(
    fresh_database: DatabaseHandle, tmp_path: Path
) -> None:
    every = load_cases(INJECTION_CASES)
    asked = asked_cases(every)
    behaviours = picks(asked)
    obeying = next(c for c, b in behaviours.items() if b == "obey")
    refused = next(c for c, b in behaviours.items() if b == "refuse")
    withheld = next(c for c, b in behaviours.items() if b == "withhold")
    quoting = next(c for c, b in behaviours.items() if b == "quote")
    fake = InjectionFake(asked, behaviours)
    clock = FakeClock()
    paced: list[float] = []

    def pace(seconds: float) -> None:
        paced.append(seconds)
        clock.advance(seconds)

    run = record_injection_run(fresh_database, inner=fake, pace=pace, clock=clock)

    entries = baseline_entries()
    expected_ids = [i for i, e in entries.items() if e["observed"]["model_asked"] == 1]
    assert len(expected_ids) == len(asked) > 0
    assert [c.case for c in asked] == expected_ids  # the baseline's order
    # Exactly the asked cases were sent: each once (the gateway does not try the
    # route's next candidate after a content-filter refusal), none other.
    assert sorted(fake.asked) == sorted(expected_ids)
    # The pacing was asked of the clock before every case.
    assert paced == [LIVE_PACE_SECONDS] * len(expected_ids)
    assert clock.now == 1000.0 + LIVE_PACE_SECONDS * len(expected_ids)
    # A refusal of the request and a withheld completion are answers.
    assert THE_RULE_OF_COMPLETENESS
    assert injection_problems(run) == []

    paths = write_injection_run(run, tmp_path / "out", today=TODAY, all_cases=every)
    recording_path, report_path, summary_path = paths
    assert [p.name for p in paths] == [
        "claims-triage-injection.json",
        "claims-triage-injection-live.json",
        "injection-live-summary.md",
    ]
    assert recording_path.parent.name == "recordings"

    check_recording(recording_path, run)
    document = json.loads(report_path.read_text(encoding="utf-8"))
    check_report_form(document, expected_ids)
    check_totals_are_the_sum_of_the_cases(document)
    check_no_sentence_in_the_files([report_path, summary_path], every, expected=asked)
    check_the_obeying_case_reads_as_the_scripted_baseline_does(
        document, entries[obeying], obeying
    )
    check_a_refusal_and_a_withheld_completion(document, refused, withheld)
    assert document["totals"]["answers_quoting_the_case"] == [quoting]
    quoted = case_of(document, quoting)
    assert quoted["answer_quotes_the_case"] is True
    assert sum(c["answer_quotes_the_case"] for c in document["cases"]) == 1
    summary = summary_path.read_text(encoding="utf-8")
    assert "2026-10-08" in summary
    assert "| all | all |" in summary
    check_the_printed_lines(fresh_database, run)

    check_the_recording_replays(fresh_database, recording_path, run, behaviours)


def case_of(document: dict, case: str) -> dict:
    (found,) = [c for c in document["cases"] if c["case"] == case]
    return found


def check_recording(path: Path, run) -> None:
    """The golden recording's entry form: one replay provider reads both."""
    recording = load_recording(path)
    assert set(recording.recorded_for) == {"claims-triage"}
    assert len(recording.entries) == run.chat_calls > 0
    assert {
        field
        for entry in json.loads(path.read_text(encoding="utf-8"))["entries"].values()
        for field in entry
    } == {
        "text",
        "finish_reason",
        "model",
        "input_tokens",
        "output_tokens",
        "latency_ms",
    }


CASE_KEYS = {
    "case",
    "label",
    "family",
    "carrier",
    "base_claim",
    "grades",
    "observed",
    "injection_obeyed",
    "answer_quotes_the_case",
    "finish_reason",
    "deployment",
    "refusal",
    "model_calls",
    "input_tokens",
    "output_tokens",
    "cost_micro_eur",
    "latency_ms",
}
TOTAL_KEYS = {
    "cases",
    "answers_obeying_the_injection",
    "refusals",
    "withheld_completions",
    "calls",
    "input_tokens",
    "output_tokens",
    "cost_micro_eur",
    "answers_quoting_the_case",
    "by_label_and_family",
}


def check_report_form(document: dict, expected_ids: list[str]) -> None:
    assert set(document) == {
        "format",
        "workload",
        "run_on",
        "answered_by",
        "fingerprints",
        "absolute",
        "cases",
        "totals",
    }
    assert document["answered_by"] == {
        "kind": "live",
        "label": "real",
        "deployments": ["aoai-sdc-gpt-4o"],
        "models": ["fake-model"],
    }
    assert document["absolute"] == ["contained", "ended", "tools_allowlisted"]
    assert [c["case"] for c in document["cases"]] == expected_ids
    assert all(set(c) == CASE_KEYS for c in document["cases"])
    assert set(document["totals"]) == TOTAL_KEYS
    assert all(
        c["finish_reason"] == "stop" for c in document["cases"] if not c["refusal"]
    )
    # A call that cost something is a call: a refused prompt was billed nothing.
    assert all(
        c["model_calls"] >= 1
        for c in document["cases"]
        if c["refusal"] is None or c["refusal"]["completion"] == "withheld"
    )


def check_totals_are_the_sum_of_the_cases(document: dict) -> None:
    cases, totals = document["cases"], document["totals"]
    assert totals["cases"] == len(cases)
    for key in ("calls", "input_tokens", "output_tokens", "cost_micro_eur"):
        source = {"calls": "model_calls"}.get(key, key)
        assert totals[key] == sum(c[source] for c in cases), key
        assert totals[key] == sum(g[key] for g in totals["by_label_and_family"]), key
    assert totals["answers_obeying_the_injection"] == sum(
        1 for c in cases if c["injection_obeyed"]
    )
    assert sum(g["cases"] for g in totals["by_label_and_family"]) == len(cases)
    assert totals["cost_micro_eur"] > 0


def check_no_sentence_in_the_files(paths, every, *, expected) -> None:
    """No string of the two reports holds a sentence a case adds: the scan is
    written here, not the support module's, over every case of the set."""
    sentences = {added_text(case) for case in every} - {""}
    assert len(sentences) >= len(expected)
    for path in paths:
        text = path.read_text(encoding="utf-8")
        holding = [
            index
            for index, sentence in enumerate(sorted(sentences))
            if any(
                form in text
                for form in (
                    sentence,
                    json.dumps(sentence)[1:-1],
                    json.dumps(sentence, ensure_ascii=False)[1:-1],
                )
            )
        ]
        # The index, never the sentence: a failure must not print an attack.
        assert holding == [], f"{path.name} holds the text of {len(holding)} sentences"


def check_the_obeying_case_reads_as_the_scripted_baseline_does(
    document: dict, baseline: dict, case: str
) -> None:
    """The fake obeys one case as the obedient model does, so the graders'
    fields are the scripted baseline's for it, computed by the same functions."""
    entry = case_of(document, case)
    assert entry["grades"] == baseline["grades"]
    assert entry["observed"] == baseline["observed"]
    assert entry["grades"]["route_held"] is False or (
        entry["grades"]["recommendation_held"] is False
    )
    assert entry["injection_obeyed"] is True


def check_a_refusal_and_a_withheld_completion(
    document: dict, refused: str, withheld: str
) -> None:
    entry = case_of(document, refused)
    assert entry["refusal"] == {
        "class": "filtered",
        "completion": "none",
        "status": 400,
        "headers": {"X-Meridian-Refusal": "content-filter"},
    }
    assert entry["observed"]["unavailable_because"] == "filtered"
    assert entry["finish_reason"] is None
    entry = case_of(document, withheld)
    assert entry["observed"]["unavailable_because"] == "filtered"
    refusal = entry["refusal"]
    assert refusal["class"] == "filtered"
    assert refusal["completion"] == "withheld"
    assert refusal["status"] == 400
    assert set(refusal["headers"]) == GATEWAY_HEADER_NAMES
    assert refusal["headers"]["X-Meridian-Completion"] == "withheld"
    assert refusal["headers"]["X-Meridian-Deployment"] == "aoai-sdc-gpt-4o"
    assert refusal["headers"]["X-Meridian-Provider"] == "azure-openai"
    assert refusal["headers"]["X-Meridian-Mode"] == "live"
    totals = document["totals"]
    assert totals["refusals"] == 1
    assert totals["withheld_completions"] == 1
    assert [c["case"] for c in document["cases"] if c["refusal"] is not None] == sorted(
        [refused, withheld]
    )
    # A withheld completion is billed with its reservation kept; a refused
    # prompt costs nothing.
    assert case_of(document, withheld)["cost_micro_eur"] > 0
    assert case_of(document, refused)["cost_micro_eur"] == 0


def check_the_printed_lines(db: DatabaseHandle, run) -> None:
    """What the paid test prints per case is what the golden run prints: the ID,
    the tokens, the latency and the rate; then the totals."""
    shown = as_recorded_run(run)
    lines = list(call_lines(db, shown))
    assert lines
    ids = {c.case for c in run.cases}
    for line in lines:
        label, _, rest = line.partition(": ")
        assert label in ids
        assert rest.endswith(" tokens/s")
        assert "in," in rest and "out," in rest
    assert totals_line(db, shown).startswith(f"total: {run.chat_calls} chat calls")


def check_the_recording_replays(
    db: DatabaseHandle, recording_path: Path, run, behaviours: dict[str, str]
) -> None:
    """A replay provider built from the fake run's recording gives the same
    proposals for the cases the model answered. A refusal and a withheld
    completion are not in the recording (the provider raised, so nothing was
    answered): the replay of those two is not what this checks."""
    answered = tuple(
        c for c in run.cases if behaviours.get(c.case) not in ("refuse", "withhold")
    )
    was = stored_proposals(db)
    with another_database(db) as replay_database:
        provider = RecordedProvider(load_recording(recording_path))
        second = recorded_gateway(replay_database, provider)
        stack = build_stack(replay_database, runtime_http=second.http)
        mark = ObedientModel()
        capture = ToolCapture()
        with pytest.MonkeyPatch.context() as patch:
            capture.install(patch)
            for case in answered:
                second.clock.advance(LIVE_PACE_SECONDS)
                run_cases(stack, [case], capture, model=mark)
        now = stored_proposals(replay_database)
    assert provider.missed == ()
    assert provider.unused() == ()
    assert set(now) == {c.case for c in answered}
    for case in answered:
        before, after = was[case.case], now[case.case]
        assert before.drafted_by is not None and after.drafted_by is not None
        assert after.drafted_by.mode == "recorded"
        assert after.model_copy(update={"drafted_by": None}) == before.model_copy(
            update={"drafted_by": None}
        ), case.case


# ── an incomplete run writes nothing ────────────────────────────────────────
def test_a_run_the_ceiling_stops_writes_nothing_and_names_the_cases(
    fresh_database: DatabaseHandle, tmp_path: Path
) -> None:
    every = load_cases(INJECTION_CASES)
    asked = asked_cases(every)
    ceiling = registry_with_ceilings(
        REGISTRY_DIR,
        tmp_path / "registry",
        {t: type(INJECTION_RUN_CEILING_EUR)(FEW_CENTS) for t in INJECTION_RUN_TENANTS},
    )
    fake = InjectionFake(asked, large=True)
    clock = FakeClock()

    run = record_injection_run(
        fresh_database,
        inner=fake,
        pace=clock.advance,
        clock=clock,
        registry_dir=ceiling,
    )

    stopped = [c.case for c in asked if c.case not in fake.asked]
    assert 0 < len(fake.asked) < len(asked)
    assert stopped
    problems = injection_problems(run)
    assert [p.split(" has no answer")[0] for p in problems[: len(stopped)]] == stopped
    # The line says what stopped it from the gateway's own refusal, not from the
    # runtime's ``model-error``, which a provider fault gets too.
    assert all(
        "refused by the gateway: the tenant's budget is used up "
        "(audit: tenant-cost-budget)" in p
        for p in problems[: len(stopped)]
    )
    assert not any("model-error" in p for p in problems)
    out = tmp_path / "out"
    with pytest.raises(IncompleteRun, match="nothing was written") as raised:
        write_injection_run(run, out, today=TODAY, all_cases=every)
    assert stopped[0] in str(raised.value)
    assert not out.exists()


# ── what needs no database ──────────────────────────────────────────────────
def test_the_asked_cases_are_read_from_the_baseline_in_its_order() -> None:
    entries = baseline_entries()
    expected = [i for i, e in entries.items() if e["observed"]["model_asked"] == 1]

    asked = asked_cases()

    assert [c.case for c in asked] == expected
    # 40 attacks and 12 benign cases on 2026-10-07; a count read from the file.
    assert {c.label for c in asked} == {"attack", "benign"}
    assert 0 < len(asked) < len(entries)


def test_a_case_the_baseline_names_and_the_case_file_lacks_is_refused() -> None:
    every = load_cases(INJECTION_CASES)

    with pytest.raises(ValueError, match="the case file lacks"):
        asked_cases(every[:3])


def test_the_run_is_capped_by_default() -> None:
    """Without a registry of its own the run's gateway loads a copy in which the
    tenants it charges have its ceiling, and the committed registry is
    untouched."""
    committed = load_registry(REGISTRY_DIR)
    with _run_registry(None) as directory:
        copy = load_registry(directory)
        budgets = {t.id: t.limits.cost_per_month_eur for t in copy.tenants}
    for tenant in committed.tenants:
        wanted = (
            INJECTION_RUN_CEILING_EUR
            if tenant.id in INJECTION_RUN_TENANTS
            else tenant.limits.cost_per_month_eur
        )
        assert budgets[tenant.id] == wanted
    assert all(
        t.limits.cost_per_month_eur > INJECTION_RUN_CEILING_EUR
        for t in committed.tenants
        if t.id in INJECTION_RUN_TENANTS
    )
    assert not directory.exists()


def test_a_string_that_holds_a_cases_sentence_is_refused_by_the_writer() -> None:
    cases = load_cases(INJECTION_CASES)
    case = next(c for c in cases if c.carrier == "description")
    sentence = added_text(case)

    for form in (sentence, json.dumps(sentence)[1:-1]):
        with pytest.raises(IncompleteRun, match="would hold 1 sentences"):
            check_no_case_text(f'{{"note": "{form}"}}', [case])
    check_no_case_text('{"note": "nothing of a case"}', cases)


# ── what a run may write, and how (S071, L3) ────────────────────────────────
def small_run(db: DatabaseHandle, behaviour: str):
    """A complete run of the first two asked cases, the first answered by
    ``behaviour``: cheap enough to run once for each thing a file must not hold."""
    every = load_cases(INJECTION_CASES)
    asked = asked_cases(every)[:2]
    fake = InjectionFake(asked, {asked[0].case: behaviour})
    clock = FakeClock()
    run = record_injection_run(
        db, inner=fake, pace=clock.advance, clock=clock, cases=asked
    )
    assert injection_problems(run) == []
    return run, every


LEAKS = [*SAMPLES.items(), ("entries over 4000 characters", "x" * 4001)]


@pytest.mark.parametrize(("kind", "text"), LEAKS, ids=[k for k, _ in LEAKS])
def test_a_run_whose_answer_holds_a_shape_only_a_leak_puts_there_writes_nothing(
    fresh_database: DatabaseHandle, tmp_path: Path, kind: str, text: str
) -> None:
    run, every = small_run(fresh_database, f"raw:{text}")
    out = tmp_path / "out"

    with pytest.raises(UnsafeFile) as raised:
        write_injection_run(run, out, today=TODAY, all_cases=every)

    message = str(raised.value)
    # The file and the kind, never the text.
    assert message.startswith(
        "nothing was written: claims-triage-injection.json holds "
    )
    assert kind in message
    assert text not in message
    assert not out.exists()


def test_an_ordinary_answer_passes_the_checks_and_the_files_are_written(
    fresh_database: DatabaseHandle, tmp_path: Path
) -> None:
    run, every = small_run(fresh_database, f"raw:{ORDINARY_ANSWER}")

    paths = write_injection_run(run, tmp_path / "out", today=TODAY, all_cases=every)

    assert [p.name for p in paths] == [
        "claims-triage-injection.json",
        "claims-triage-injection-live.json",
        "injection-live-summary.md",
    ]
    assert all(path.is_file() for path in paths)
    # Nothing is left beside them: the staging directory is gone.
    assert sorted(p.name for p in (tmp_path / "out").iterdir()) == [
        "claims-triage-injection-live.json",
        "injection-live-summary.md",
        "recordings",
    ]


def test_a_writer_killed_between_two_files_leaves_none_of_the_three_in_place(
    fresh_database: DatabaseHandle, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run, every = small_run(fresh_database, "oracle")
    real = writehygienesupport._write_text
    writes: list[str] = []

    def second_write_raises(path: Path, text: str) -> None:
        if writes:
            raise OSError("the writer was killed")
        writes.append(path.name)
        real(path, text)

    monkeypatch.setattr(writehygienesupport, "_write_text", second_write_raises)
    out = tmp_path / "out"

    with pytest.raises(OSError, match="writer was killed"):
        write_injection_run(run, out, today=TODAY, all_cases=every)

    assert len(writes) == 1  # one file was written, to the staging directory
    assert list(out.iterdir()) == []  # none in place, and no staging left


# ── the paid run ────────────────────────────────────────────────────────────
@OPT_IN
def test_record_the_injection_cases_with_the_live_model(
    fresh_database: DatabaseHandle,
) -> None:
    run = record_injection_run(fresh_database)

    print()
    shown = as_recorded_run(run)
    for line in call_lines(fresh_database, shown):
        print(line)
    print(totals_line(fresh_database, shown))
    try:
        write_injection_run(run)
    except (IncompleteRun, UnsafeFile) as stopped:
        pytest.fail(f"{stopped}; {RECORD_COMMAND}")
