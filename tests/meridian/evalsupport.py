"""The evaluation run of the claims workload over a second gateway (S050).

``stacksupport.build_stack`` keeps the first gateway in replay mode for the
ingestion and the wording search, so the embeddings, the clauses found and so
each chat request are the same whether the answers are recorded or replayed. The
evaluation hands the runtime a second gateway over the same database
(``build_stack(db, runtime_http=...)``): in CI one in ``recorded`` mode, on a
laptop one in ``live`` mode whose Azure provider is wrapped in a
``RecordingProvider``. The judge calls the second gateway too.

``run_evaluation`` posts the 40 golden claims, judges every proposal that has a
rationale, and reads from the ledger and from the tool client what each case
cost and called. Nothing here prints a prompt, an endpoint, a tenant ID or a
header.
"""

import hashlib
import json
import os
import re
import threading
import time
import uuid
from collections import defaultdict
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Literal

import pytest
from dbsupport import DatabaseHandle, copy_database, drop_database, ensure_template
from fastapi.testclient import TestClient
from knowledgesupport import Gateway
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from servicesupport import REGISTRY_DIR, REPO_ROOT, FakeClock, owner_rows
from stacksupport import (
    CLAIMS,
    EXPECTED,
    MANIFEST,
    POLICIES,
    WINDOW_SECONDS,
    Stack,
    build_stack,
    claims_that_ask_the_model,
    stored_proposals,
    whole_wording,
)

from meridian.platform.common.metrics import make_meter_provider
from meridian.platform.common.telemetry import make_tracer_provider
from meridian.platform.evaluation.judge import (
    JUDGE_PROMPT_VERSION,
    Judgement,
    judge,
)
from meridian.platform.evaluation.report import (
    AnsweredBy,
    Measured,
    Report,
    ToolCall,
)
from meridian.platform.gateway.app import AZURE_KIND, RECORDED_KIND, REPLAY_KIND
from meridian.platform.gateway.app import create_app as create_gateway
from meridian.platform.gateway.providers.base import ModelProvider
from meridian.platform.gateway.providers.recorded import (
    RecordedProvider,
    Recording,
    RecordingProvider,
)
from meridian.platform.gateway.replay import ReplayProvider
from meridian.platform.gateway.settings import (
    ENDPOINTS_ENV,
    TENANT_ID_ENV,
    GatewaySettings,
)
from meridian.platform.registry import Registry, load_registry
from meridian.runtime.tool_client import ToolClient
from meridian.workloads.claims_triage import assessment
from meridian.workloads.claims_triage.evaluation import build_report, judge_inputs
from meridian.workloads.claims_triage.proposal import TriageProposal
from meridian.workloads.claims_triage.wording import Clause, select_terms

EVALUATION_DIR = REPO_ROOT / "data" / "evaluation"
RECORDING_PATH = EVALUATION_DIR / "recordings" / "claims-triage.json"
BASELINE_PATH = EVALUATION_DIR / "claims-triage-baseline.json"
LIVE_REPORT_PATH = EVALUATION_DIR / "claims-triage-live.json"
VARIANT_REPORT_PATH = EVALUATION_DIR / "claims-triage-live-variant.json"
COMPARISON_PATH = EVALUATION_DIR / "prompt-comparison.md"
RECORD_COMMAND = "run make eval-record (needs an Azure login)"

RECORDED = AnsweredBy(kind="recorded", label="real")
LIVE = AnsweredBy(kind="live", label="real")
Kind = Literal["recorded", "live"]

# Real seconds before each claim that asks the model, and before each judge call,
# in the recording run: the gateway's windows are real there (10 requests per
# 10 seconds and 10,000 tokens per minute for the claims-triage tenant, 6 and
# 6,000 for the evaluation tenant), and the judge does not retry a 429.
LIVE_PACE_SECONDS = 10
JUDGE_PACE_SECONDS = 12

PERSONAL = "personal"
CHAT = "chat"
MICRO_PER_EURO = 1_000_000
MILLISECONDS = timedelta(milliseconds=1)
# What an argument must not be, to be the same on every run of the same tree: a
# run ID or an idempotency key, or a time of day.
UNSTABLE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    r"|[0-9a-f]{64}"
    r"|\d{4}-\d\d-\d\d[T ]\d\d:\d\d",
    re.IGNORECASE,
)


# ── a second database ───────────────────────────────────────────────────────
@contextmanager
def another_database(like: DatabaseHandle) -> Iterator[DatabaseHandle]:
    """A new migrated database on the same server as ``like``, dropped
    afterwards: ``conftest.py`` makes one per fixture, and a test that runs the
    stack on a fresh database more than once needs more than one. A copy of the
    migrated template, as ``fresh_database`` is (``dbsupport.copy_database``)."""
    template = ensure_template(like.admin_dsn, like.passwords)
    handle = copy_database(like.admin_dsn, like.passwords, template)
    try:
        yield handle
    finally:
        drop_database(handle)


# ── the second gateway ──────────────────────────────────────────────────────
def _tracer():
    return make_tracer_provider("model-gateway", InMemorySpanExporter())


def recorded_gateway(db: DatabaseHandle, provider: RecordedProvider) -> Gateway:
    """The gateway app in recorded mode over ``db`` on a fake clock the caller
    moves; it holds ``provider``, so that a test reads its ``missed`` and
    ``unused()``. Embeddings are answered by the replay provider."""
    clock = FakeClock()
    app = create_gateway(
        GatewaySettings(
            registry_dir=REGISTRY_DIR,
            mode="recorded",
            environment="test",
            database_url=db.dsn("model_gateway"),
        ),
        tracer_provider=_tracer(),
        meter_provider=make_meter_provider("model-gateway", InMemoryMetricReader()),
        providers={RECORDED_KIND: provider, REPLAY_KIND: ReplayProvider()},
        clock=clock,
    )
    return Gateway(TestClient(app), clock, load_registry(REGISTRY_DIR), db)


@dataclass(slots=True)
class LiveGateway:
    """The gateway app in live mode on the real clock, whose provider records
    what it was answered."""

    http: TestClient
    registry: Registry
    recording: RecordingProvider
    close: Callable[[], None]


def live_recording_gateway(
    db: DatabaseHandle,
    *,
    inner: ModelProvider | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> LiveGateway:
    """A gateway in live mode (environment ``local``) over ``db`` whose one
    provider, under the Azure kind, is a ``RecordingProvider``. Without ``inner``
    it wraps the Azure OpenAI adapter, built as ``test_live_azure.py`` builds
    it: the endpoints and the Entra tenant come from the two variables
    ``make eval-record`` sets, the token from this ``az login``. A test brings
    its own ``inner``, and no Azure setting is read then; it may bring a clock
    to move too (the real one by default)."""
    endpoints = {} if inner is not None else json.loads(os.environ[ENDPOINTS_ENV])
    tenant_id = None if inner is not None else os.environ[TENANT_ID_ENV]
    settings = GatewaySettings(
        registry_dir=REGISTRY_DIR,
        mode="live",
        environment="local",
        database_url=db.dsn("model_gateway"),
        azure_openai_endpoints=endpoints,
        azure_credential="azure-cli",
        azure_tenant_id=tenant_id,
    )
    close = _nothing
    if inner is None:
        inner, close = _azure_provider(settings)
    recording = RecordingProvider(inner)
    app = create_gateway(
        settings,
        tracer_provider=_tracer(),
        providers={AZURE_KIND: recording},
        clock=clock,
    )
    return LiveGateway(TestClient(app), load_registry(REGISTRY_DIR), recording, close)


def _nothing() -> None:
    return None


def _azure_provider(settings: GatewaySettings) -> tuple[ModelProvider, Callable]:
    from meridian.platform.gateway.providers.azure_openai import (
        AzureOpenAIProvider,
        azure_cli_token_provider,
        refuse_sdk_environment,
    )

    refuse_sdk_environment()
    provider = AzureOpenAIProvider(
        settings.azure_openai_endpoints,
        azure_cli_token_provider(settings.azure_tenant_id),
    )
    return provider, provider.close


# ── the tool calls ──────────────────────────────────────────────────────────
class ToolCapture:
    """The name and the arguments of every call a run makes through the tool
    client, taken from the call itself. An argument that is not the same on every
    run of the same tree (a UUID, a 64-digit key, a time of day) is not stored;
    ``dropped`` names the tool and the argument."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._calls: dict[uuid.UUID, list[ToolCall]] = defaultdict(list)
        self.dropped: set[tuple[str, str]] = set()

    def install(self, patch: pytest.MonkeyPatch) -> None:
        original = ToolClient.call
        capture = self

        def call(client, tool, arguments, *, step=None):
            capture.record(client._run_id, tool, arguments)
            return original(client, tool, arguments, step=step)

        patch.setattr(ToolClient, "call", call)

    def record(self, run_id: uuid.UUID, tool: str, arguments: Mapping) -> None:
        stored = {}
        dropped = set()
        for name, value in json.loads(json.dumps(dict(arguments))).items():
            if UNSTABLE.search(json.dumps(value)):
                dropped.add((tool, name))
            else:
                stored[name] = value
        with self._lock:
            self._calls[run_id].append(ToolCall(tool=tool, arguments=stored))
            self.dropped |= dropped

    def by_claim(self, db: DatabaseHandle) -> dict[str, tuple[ToolCall, ...]]:
        """The calls of each claim's runs, oldest run first, in the order made."""
        runs = owner_rows(
            db, "SELECT run_id, reference FROM runtime.runs ORDER BY created_at"
        )
        found: dict[str, list[ToolCall]] = defaultdict(list)
        with self._lock:
            for run_id, reference in runs:
                found[reference].extend(self._calls.get(run_id, ()))
        return {claim_id: tuple(calls) for claim_id, calls in found.items()}


# ── the ledger ──────────────────────────────────────────────────────────────
def settled_rows(db: DatabaseHandle) -> list[tuple]:
    """Every settled row of the ledger, oldest first: the run's reference (None
    for a run the runtime did not make, the judge's), the deployment, the tokens
    and the cost, and the two times."""
    return owner_rows(
        db,
        "SELECT r.reference, u.run_id, u.deployment, u.input_tokens, "
        "u.output_tokens, u.charged_micro_eur, u.reserved_at, u.closed_at "
        "FROM gateway.usage u LEFT JOIN runtime.runs r ON r.run_id = u.run_id "
        "WHERE u.state = 'settled' ORDER BY u.reserved_at, u.attempt_id",
    )


def unsettled_count(db: DatabaseHandle) -> int:
    """The rows of the ledger no answer settled: a call that failed or was
    refused after it was reserved."""
    ((count,),) = owner_rows(
        db, "SELECT count(*) FROM gateway.usage WHERE state <> 'settled'"
    )
    return int(count)


def _is_chat(registry: Registry, deployment: str) -> bool:
    found = registry.deployment(deployment)
    return found is not None and found.purpose == CHAT


def chat_calls(db: DatabaseHandle, registry: Registry) -> int:
    """The settled chat calls of the whole ledger, the judge's included."""
    return sum(1 for row in settled_rows(db) if _is_chat(registry, row[2]))


def measure_claims(db: DatabaseHandle, registry: Registry, *, kind: Kind) -> dict:
    """What each claim's runs cost: the settled rows of ``gateway.usage`` joined
    to ``runtime.runs`` on ``run_id`` for the claim's reference. ``model_calls``
    counts the rows on a chat deployment; the tokens and the cost are sums over
    all the rows. ``latency_ms`` is, for a live run, the sum over the chat rows
    of ``closed_at - reserved_at`` in whole milliseconds, and None for a recorded
    run, whose ledger times a file read. A claim with no row measured nothing."""
    totals = {
        claim_id: {"calls": 0, "in": 0, "out": 0, "micro": 0, "ms": 0}
        for claim_id in CLAIMS
    }
    for (
        reference,
        _,
        deployment,
        tokens_in,
        tokens_out,
        micro,
        opened,
        closed,
    ) in settled_rows(db):
        if reference not in totals:
            continue
        total = totals[reference]
        total["in"] += tokens_in
        total["out"] += tokens_out
        total["micro"] += micro
        if _is_chat(registry, deployment):
            total["calls"] += 1
            total["ms"] += (closed - opened) // MILLISECONDS
    return {
        claim_id: Measured(
            model_calls=t["calls"],
            input_tokens=t["in"],
            output_tokens=t["out"],
            cost_micro_eur=t["micro"],
            latency_ms=t["ms"] if kind == "live" else None,
        )
        for claim_id, t in totals.items()
    }


def chat_tokens(db: DatabaseHandle, registry: Registry) -> dict[str, tuple[int, int]]:
    """For each claim, the input and output tokens of the settled rows on a chat
    deployment alone (``measure_claims`` sums all rows, the wording searches'
    embeddings included)."""
    found: dict[str, tuple[int, int]] = {}
    for reference, _, deployment, tokens_in, tokens_out, *_ in settled_rows(db):
        if reference in CLAIMS and _is_chat(registry, deployment):
            before = found.get(reference, (0, 0))
            found[reference] = (before[0] + tokens_in, before[1] + tokens_out)
    return found


# ── the run ─────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Evaluation:
    """What one run produced, for ``report_of``."""

    proposals: dict[str, TriageProposal | None]
    judgements: dict[str, Judgement]
    measured: dict[str, Measured]
    tools: dict[str, tuple[ToolCall, ...]]
    judge_runs: dict[uuid.UUID, str] = field(default_factory=dict)
    dropped: frozenset[tuple[str, str]] = frozenset()


def candidates_of(claim_id: str) -> tuple[Clause, ...]:
    """The clauses the model is shown for a claim: the candidate exclusions the
    rules pick from the whole wording of the policy's product, which is what a
    search that loses nothing finds."""
    claim = CLAIMS[claim_id]
    policy = POLICIES[claim["policy_number"]]
    terms = select_terms(
        claim["peril"],
        whole_wording(policy["product"]),
        product=policy["product"],
        wording_version=policy["wording_version"],
    )
    return terms.candidates


def judge_proposals(
    http,
    proposals: Mapping[str, TriageProposal | None],
    *,
    pause: float,
    pace: Callable[[float], None],
) -> tuple[dict[str, Judgement], dict[uuid.UUID, str]]:
    """Ask the judge about every proposal that has a rationale, one call each
    with a run ID of its own and the data class ``personal``; ``pace(pause)``
    runs before each call so that the evaluation tenant's windows are clear (the
    judge does not retry a 429)."""
    judgements: dict[str, Judgement] = {}
    runs: dict[uuid.UUID, str] = {}
    for claim_id in sorted(proposals):
        proposal = proposals[claim_id]
        shown = (
            None
            if proposal is None
            else judge_inputs(CLAIMS[claim_id], proposal, candidates_of(claim_id))
        )
        if shown is None:
            continue
        pace(pause)
        run_id = uuid.uuid4()
        runs[run_id] = claim_id
        judgements[claim_id] = judge(
            http,
            run_id=run_id,
            source=shown.source,
            statement=shown.statement,
            data_class=PERSONAL,
        )
    return judgements, runs


def run_evaluation(
    stack: Stack,
    second: Gateway | LiveGateway,
    *,
    kind: Kind,
    pace: Callable[[float], None],
) -> Evaluation:
    """Post the 40 golden claims to ``stack`` (whose runtime calls ``second``),
    judge the proposals through ``second``, and read the ledger and the tool
    calls.

    ``pace(seconds)`` clears the second gateway's windows: a recorded run moves
    its fake clock (``pace=second.clock.advance``) before every claim and every
    judge call, a live run sleeps (``pace=time.sleep``) only before a claim that
    asks the model and before every judge call."""
    capture = ToolCapture()
    asking = claims_that_ask_the_model()
    with pytest.MonkeyPatch.context() as patch:
        capture.install(patch)
        for claim_id, claim in CLAIMS.items():
            pause = WINDOW_SECONDS if kind == "recorded" else 0
            if kind == "live" and claim_id in asking:
                pause = LIVE_PACE_SECONDS
            if pause:
                pace(pause)
            stack.post(claim)
    proposals = {c: None for c in CLAIMS} | dict(stored_proposals(stack.db))
    judge_pause = WINDOW_SECONDS if kind == "recorded" else JUDGE_PACE_SECONDS
    judgements, runs = judge_proposals(
        second.http, proposals, pause=judge_pause, pace=pace
    )
    return Evaluation(
        proposals=proposals,
        judgements=judgements,
        measured=measure_claims(stack.db, second.registry, kind=kind),
        tools=capture.by_claim(stack.db),
        judge_runs=runs,
        dropped=frozenset(capture.dropped),
    )


def recording_sha256(path: Path = RECORDING_PATH) -> str:
    """The SHA-256 of the recording file's bytes: the report's recording
    fingerprint."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def report_of(
    evaluation: Evaluation,
    registry: Registry,
    *,
    answered_by: AnsweredBy,
    recording_fingerprint: str | None,
) -> Report:
    """The report of a run, graded by the claims workload. The prompt is the
    assessment prompt of the tree at the time of the call (a test that swaps the
    prompt swaps its version too)."""
    return build_report(
        evaluation.proposals,
        EXPECTED,
        CLAIMS,
        POLICIES,
        manifest_path=MANIFEST,
        registry=registry,
        answered_by=answered_by,
        prompt=assessment.PROMPT_VERSION,
        judgements=evaluation.judgements,
        measured=evaluation.measured,
        tools=evaluation.tools,
        judge_fingerprint=JUDGE_PROMPT_VERSION,
        recording_fingerprint=recording_fingerprint,
    )


# ── the recording run ───────────────────────────────────────────────────────
def recorded_for() -> dict[str, str]:
    """The prompts a recording made now answers, by label."""
    return {
        "claims-triage": assessment.PROMPT_VERSION,
        "evaluation-judge": JUDGE_PROMPT_VERSION,
    }


@dataclass(frozen=True)
class RecordedRun:
    evaluation: Evaluation
    recording: Recording
    registry: Registry
    chat_calls: int
    unsettled: int


def record_run(
    db: DatabaseHandle,
    *,
    inner: ModelProvider | None = None,
    pace: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> RecordedRun:
    """Run the evaluation through a live-mode gateway that records its answers,
    pacing in real seconds (a test brings a fake ``inner`` and a ``clock`` that
    ``pace`` moves)."""
    gateway = live_recording_gateway(db, inner=inner, clock=clock)
    try:
        stack = build_stack(db, runtime_http=gateway.http)
        evaluation = run_evaluation(stack, gateway, kind="live", pace=pace)
    finally:
        gateway.close()
    return RecordedRun(
        evaluation=evaluation,
        recording=gateway.recording.recording(recorded_for()),
        registry=gateway.registry,
        chat_calls=chat_calls(db, gateway.registry),
        unsettled=unsettled_count(db),
    )


def recording_problems(run: RecordedRun) -> list[str]:
    """Why a recording run must not be written: a claim without a proposal, a
    model call that failed, or a recording that does not hold one entry per chat
    call made. Empty when it may be."""
    problems = []
    missing = sorted(c for c, p in run.evaluation.proposals.items() if p is None)
    if missing:
        problems.append(f"{len(missing)} claims have no proposal: {', '.join(missing)}")
    if run.unsettled:
        problems.append(f"{run.unsettled} model calls did not settle")
    if len(run.recording.entries) != run.chat_calls:
        problems.append(
            f"{run.chat_calls} chat calls were made but the recording holds "
            f"{len(run.recording.entries)} entries"
        )
    if run.evaluation.dropped:
        problems.append(f"unstable tool arguments: {sorted(run.evaluation.dropped)}")
    return problems


# ── what a recording run prints ─────────────────────────────────────────────
def call_lines(db: DatabaseHandle, run: RecordedRun) -> Iterable[str]:
    """One line per chat call: the claim's ID (the judged claim's, marked
    ``judge``), the tokens, the latency in milliseconds and the tokens per
    second. Never an endpoint, a tenant, a header or a prompt."""
    judged = run.evaluation.judge_runs
    for (
        reference,
        run_id,
        deployment,
        tokens_in,
        tokens_out,
        _,
        opened,
        closed,
    ) in settled_rows(db):
        if not _is_chat(run.registry, deployment):
            continue
        label = reference or f"{judged.get(run_id, '?')} judge"
        milliseconds = (closed - opened) // MILLISECONDS
        rate = tokens_out / (milliseconds / 1000) if milliseconds else 0.0
        yield (
            f"{label}: {tokens_in} in, {tokens_out} out, {milliseconds} ms, "
            f"{rate:.1f} tokens/s"
        )


def totals_line(db: DatabaseHandle, run: RecordedRun) -> str:
    """The last line: the calls, the tokens and the cost in EUR, from the
    ledger (the judge's calls included)."""
    rows = settled_rows(db)
    micro = sum(row[5] for row in rows)
    return (
        f"total: {run.chat_calls} chat calls, "
        f"{sum(r[3] for r in rows)} tokens in, {sum(r[4] for r in rows)} out, "
        f"EUR {micro / MICRO_PER_EURO:.4f}"
    )


# ── the variant prompt ──────────────────────────────────────────────────────
OLD_LAST_SENTENCE = (
    "The rationale says in a sentence or two which fact in the description led "
    "to the verdict."
)
NEW_LAST_SENTENCE = (
    "The rationale quotes, in double quotation marks, the words of the "
    "description that led to the verdict, and says in one sentence why they do "
    "or do not meet the clause."
)
if OLD_LAST_SENTENCE not in assessment.SYSTEM_MESSAGE:
    raise RuntimeError("the assessment prompt no longer ends as the variant expects")
VARIANT_SYSTEM_MESSAGE = assessment.SYSTEM_MESSAGE.replace(
    OLD_LAST_SENTENCE, NEW_LAST_SENTENCE
)
