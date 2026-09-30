"""Facts about one framework that the shared observation tests cannot express."""

import asyncio
import json
import logging
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Never
from uuid import UUID

import pytest
from agent_framework import (
    Case,
    Default,
    FileCheckpointStorage,
    InMemoryCheckpointStorage,
    WorkflowBuilder,
    WorkflowContext,
    executor,
)
from agent_framework.exceptions import WorkflowCheckpointException
from agent_framework.observability import OBSERVABILITY_SETTINGS
from claimflow import langgraph_flow, maf_flow, rules
from langchain_core.callbacks import BaseCallbackHandler
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.errors import GraphInterrupt
from langgraph.types import Command
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from opentelemetry.trace import Status, StatusCode
from pydantic import ValidationError
from support import OVER_THRESHOLD_CLAIM


@pytest.fixture(scope="module")
def _tracer_provider() -> InMemorySpanExporter:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)  # process-wide, and settable only once
    return exporter


@pytest.fixture
def spans(_tracer_provider: InMemorySpanExporter) -> InMemorySpanExporter:
    _tracer_provider.clear()
    return _tracer_provider


GOOD = {"decision": "approve", "adjuster_id": "ADJ-0001"}
OTHER = {"decision": "reject", "adjuster_id": "ADJ-0002"}


def _personal_data() -> dict[str, str]:
    claim = rules.validate(OVER_THRESHOLD_CLAIM).claim
    return {
        "name": claim["claimant"]["name"],
        "email": claim["claimant"]["email"],
        "description": claim["description"][:40],
    }


def _maf_latest(store_dir: Path, run_ref: str) -> Any:
    """The run's latest checkpoint: the pause checkpoint, right after a pause."""
    storage = maf_flow.open_storage(store_dir)
    name = maf_flow.workflow_name(run_ref)
    return asyncio.run(storage.get_latest(workflow_name=name))


def _maf_records(store_dir: Path) -> list[tuple[str, str, str]]:
    """(checkpoint id, raw file text, decoded checkpoint repr) per checkpoint file."""
    storage = FileCheckpointStorage(
        store_dir, allowed_checkpoint_types=maf_flow.ALLOWED_CHECKPOINT_TYPES
    )
    records = []
    for path in sorted(store_dir.glob("*.json")):
        checkpoint = asyncio.run(storage.load(path.stem))
        records.append((path.stem, path.read_text(), repr(checkpoint)))
    return records


def _langgraph_rows(store_dir: Path) -> list[str]:
    """Raw bytes of every checkpoint and write row, decoded byte-for-byte."""
    with sqlite3.connect(store_dir / langgraph_flow.DB_FILE) as conn:
        blobs = [row[0] for row in conn.execute("SELECT checkpoint FROM checkpoints")]
        blobs += [row[0] for row in conn.execute("SELECT value FROM writes")]
    return [blob.decode("latin-1") for blob in blobs]


ATTEMPTS = 10  # a race may need a retry to overlap; see the concurrent tests


def _resume_concurrently(
    resume: Callable[..., Any], run_ref: str, store_dir: Path
) -> tuple[Any, Any]:
    """Two threads, lined up by a barrier, resume one run: (GOOD's result, OTHER's).

    Each `resume` call opens its own store connection, as two runtime replicas
    would. A result is the returned value or the exception that was raised.
    """
    barrier = threading.Barrier(2)
    results: dict[str, Any] = {}

    def worker(name: str, payload: dict[str, str]) -> None:
        barrier.wait()
        try:
            results[name] = resume(run_ref, payload, store_dir)
        except BaseException as error:  # the error is the observation
            results[name] = error

    threads = [
        threading.Thread(target=worker, args=("good", GOOD)),
        threading.Thread(target=worker, args=("other", OTHER)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return results["good"], results["other"]


# --- Microsoft Agent Framework --------------------------------------------------


def test_maf_checkpoint_files_keep_personal_data_in_pickles_not_in_clear(
    store_dir: Path,
) -> None:
    maf_flow.start(OVER_THRESHOLD_CLAIM, store_dir)

    records = _maf_records(store_dir)
    for value in _personal_data().values():
        assert not any(value in raw for _, raw, _ in records)  # base64 hides it
        assert any(value in decoded for _, _, decoded in records)  # but it is there


def test_maf_pause_checkpoint_holds_the_request_but_not_the_claim(
    store_dir: Path,
) -> None:
    # A consequence of this flow's shape (messages are consumed), not a framework
    # property: see the shared-state test below.
    paused = maf_flow.start(OVER_THRESHOLD_CLAIM, store_dir)

    pause_id = _maf_latest(store_dir, paused.run_ref).checkpoint_id
    decoded = {cid: text for cid, _, text in _maf_records(store_dir)}[pause_id]
    assert "ApprovalRequest" in decoded
    assert not any(value in decoded for value in _personal_data().values())


@executor(id="validate")
async def _validate_into_shared_state(
    claim_id: str, ctx: WorkflowContext[rules.ValidatedClaim]
) -> None:
    validated = rules.validate(claim_id)
    ctx.set_state("validated", validated)  # the untyped key/value state
    await ctx.send_message(validated)


def test_maf_shared_state_puts_the_claim_in_the_pause_checkpoint(
    store_dir: Path,
) -> None:
    storage = maf_flow.open_storage(store_dir)
    workflow = (
        WorkflowBuilder(
            name="shared-state",
            start_executor=_validate_into_shared_state,
            checkpoint_storage=storage,
        )
        .add_edge(_validate_into_shared_state, maf_flow.assess_step)
        .add_switch_case_edge_group(
            maf_flow.assess_step,
            [
                Case(
                    condition=lambda a: a.assessment.needs_approval,
                    target=maf_flow.ApprovalStep(),
                ),
                Default(target=maf_flow.auto_complete_step),
            ],
        )
        .build()
    )

    async def pause_checkpoint() -> Any:
        result = await workflow.run(OVER_THRESHOLD_CLAIM)
        ids = [event.request_id for event in result.get_request_info_events()]
        return await storage.load(await workflow.resolve_pause_checkpoint_id(ids))

    checkpoint = asyncio.run(pause_checkpoint())

    assert not any(checkpoint.messages.values())  # consumed, as in the spike's flow
    email = _personal_data()["email"]
    assert email in repr(checkpoint.state)  # but the state carries the claim


def test_maf_checkpoint_save_failure_does_not_fail_the_run(
    store_dir: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # With the application types off the allowlist, every save is refused.
    monkeypatch.setattr(maf_flow, "ALLOWED_CHECKPOINT_TYPES", [])

    async def run() -> None:
        workflow = maf_flow.build_workflow(store_dir, "run-1")
        result = await workflow.run(OVER_THRESHOLD_CLAIM)
        request_ids = [event.request_id for event in result.get_request_info_events()]
        assert request_ids  # the run reached the pause and reported it ...
        assert (
            await workflow.resolve_pause_checkpoint_id(request_ids) is None
        )  # ... unsaved

    with caplog.at_level(logging.WARNING, logger="agent_framework"):
        asyncio.run(run())

    assert any("Failed to create checkpoint" in m for m in caplog.messages)
    assert len(list(store_dir.glob("*.json"))) == 1  # only the entry checkpoint
    # The spike's own code is what turns the silent failure into an error.
    with pytest.raises(RuntimeError, match="pause was not checkpointed"):
        maf_flow.start(OVER_THRESHOLD_CLAIM, store_dir)


def test_maf_refuses_to_restore_a_checkpoint_into_a_changed_graph(
    store_dir: Path,
) -> None:
    paused = maf_flow.start(OVER_THRESHOLD_CLAIM, store_dir)
    changed = (
        WorkflowBuilder(
            name=maf_flow.workflow_name(paused.run_ref),
            start_executor=maf_flow.validate_step,
            checkpoint_storage=maf_flow.open_storage(store_dir),
        )
        .add_edge(maf_flow.validate_step, maf_flow.assess_step)
        .build()
    )
    pause_id = _maf_latest(store_dir, paused.run_ref).checkpoint_id

    with pytest.raises(WorkflowCheckpointException, match="graph has changed"):
        asyncio.run(changed.run(checkpoint_id=pause_id))


def test_maf_restore_checks_the_graph_hash_not_the_workflow_name(
    store_dir: Path,
) -> None:
    # A pause made under one workflow name resumes through a workflow with
    # another name, because only the graph signature is compared on restore.
    other = maf_flow.start(OVER_THRESHOLD_CLAIM, store_dir)
    pause = _maf_latest(store_dir, other.run_ref)
    workflow = maf_flow.build_workflow(store_dir, "a-different-run")

    result = asyncio.run(
        workflow.run(
            responses={rid: GOOD for rid in pause.pending_request_info_events},
            checkpoint_id=pause.checkpoint_id,
        )
    )

    assert result.get_outputs()[0].outcome == "approve"


def test_maf_replaying_the_pause_checkpoint_twice_lands_two_different_decisions(
    store_dir: Path,
) -> None:
    # Addressing a run by an explicit checkpoint id (instead of its workflow name)
    # replays from an immutable checkpoint: nothing marks the pause as answered.
    paused = maf_flow.start(OVER_THRESHOLD_CLAIM, store_dir)
    pause = _maf_latest(store_dir, paused.run_ref)

    async def replay(decision: dict[str, str]) -> Any:
        workflow = maf_flow.build_workflow(store_dir, paused.run_ref)
        result = await workflow.run(
            responses={rid: decision for rid in pause.pending_request_info_events},
            checkpoint_id=pause.checkpoint_id,
        )
        return result.get_outputs()[0]

    first = asyncio.run(replay(GOOD))
    second = asyncio.run(replay(OTHER))

    assert (first.outcome, first.adjuster_id) == ("approve", "ADJ-0001")
    assert (second.outcome, second.adjuster_id) == ("reject", "ADJ-0002")


def test_maf_two_concurrent_resumes_of_one_pause_both_complete_with_different_outcomes(
    store_dir: Path,
) -> None:
    """Observed 40 of 40 times in a scratch loop; here up to ATTEMPTS tries.

    Nothing serializes the two: each restores the same immutable pause
    checkpoint and runs to completion, so the run ends up with two completions
    branching from one pause. Should a thread ever start late and find the run
    already decided, the only error allowed is the "No pending requests" one and
    the attempt is retried on a fresh pause.
    """
    for attempt in range(ATTEMPTS):
        store = store_dir / f"attempt-{attempt}"
        paused = maf_flow.start(OVER_THRESHOLD_CLAIM, store)
        pause_id = _maf_latest(store, paused.run_ref).checkpoint_id

        good, other = _resume_concurrently(maf_flow.resume, paused.run_ref, store)

        errors = [r for r in (good, other) if isinstance(r, BaseException)]
        assert all(
            isinstance(e, RuntimeError) and "No pending requests" in str(e)
            for e in errors
        )
        if not errors:
            break
    else:
        pytest.fail("the two resumes never overlapped")

    assert (good.status, good.outcome, good.adjuster_id) == (
        "completed", "approve", "ADJ-0001",
    )  # fmt: skip
    assert (other.status, other.outcome, other.adjuster_id) == (
        "completed", "reject", "ADJ-0002",
    )  # fmt: skip
    storage = maf_flow.open_storage(store)
    name = maf_flow.workflow_name(paused.run_ref)
    lineage = asyncio.run(storage.list_checkpoints(workflow_name=name))
    assert sum(c.previous_checkpoint_id == pause_id for c in lineage) == 2  # a fork


def test_maf_resume_with_an_empty_object_is_refused_like_any_malformed_payload(
    store_dir: Path,
) -> None:
    paused = maf_flow.start(OVER_THRESHOLD_CLAIM, store_dir)

    with pytest.raises(ValueError, match="Response type mismatch"):
        maf_flow.resume(paused.run_ref, {}, store_dir)

    assert maf_flow.resume(paused.run_ref, GOOD, store_dir).status == "completed"


def test_maf_function_executor_can_pause_but_its_response_is_dropped(
    caplog: pytest.LogCaptureFixture,
) -> None:
    @dataclass
    class Ask:
        claim_id: str

    @dataclass
    class Answer:
        decision: str

    @executor(id="approval")
    async def approval(claim: str, ctx: WorkflowContext[Never, str]) -> None:
        await ctx.request_info(Ask(claim), Answer)

    workflow = WorkflowBuilder(
        name="function-pause",
        start_executor=approval,
        checkpoint_storage=InMemoryCheckpointStorage(),
    ).build()

    async def run() -> Any:
        paused = await workflow.run("CLM-1")
        request_id = paused.get_request_info_events()[0].request_id
        return await workflow.run(responses={request_id: Answer("approve")})

    with caplog.at_level(logging.WARNING, logger="agent_framework"):
        resumed = asyncio.run(run())

    assert any("no matching response handler" in m for m in caplog.messages)
    assert resumed.get_outputs() == []  # the response reached nobody


def test_maf_emits_opentelemetry_spans_once_a_tracer_provider_exists(
    spans: InMemorySpanExporter, store_dir: Path
) -> None:
    maf_flow.start(OVER_THRESHOLD_CLAIM, store_dir)

    names = [span.name for span in spans.get_finished_spans()]
    for prefix in (
        "workflow.build",
        "workflow.run",
        "executor.process",
        "edge_group.process",
        "message.send",
    ):
        assert any(name.startswith(prefix) for name in names), prefix


@pytest.mark.parametrize("sensitive", [False, True], ids=["off", "on"])
def test_maf_workflow_spans_carry_no_payload_whatever_the_sensitive_switch(
    spans: InMemorySpanExporter,
    store_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    sensitive: bool,
) -> None:
    # The switch governs model-call content; nothing under _workflows reads it.
    monkeypatch.setattr(OBSERVABILITY_SETTINGS, "enable_sensitive_data", sensitive)

    paused = maf_flow.start(OVER_THRESHOLD_CLAIM, store_dir)
    maf_flow.resume(paused.run_ref, GOOD, store_dir)

    blob = json.dumps(
        [
            {"name": span.name, "attributes": dict(span.attributes)}
            for span in spans.get_finished_spans()
        ],
        default=str,
    )
    probes = [*_personal_data().values(), OVER_THRESHOLD_CLAIM, "ADJ-0001"]
    assert "executor.process" in blob  # the spans exist ...
    assert not any(probe in blob for probe in probes)  # ... and hold no payload


# --- LangGraph ---------------------------------------------------------------


def test_langgraph_checkpoint_rows_hold_personal_data_in_clear(store_dir: Path) -> None:
    langgraph_flow.start(OVER_THRESHOLD_CLAIM, store_dir)

    rows = _langgraph_rows(store_dir)
    for value in _personal_data().values():
        assert any(value in row for row in rows)  # msgpack keeps strings readable


def test_langgraph_unknown_run_ref_leaves_a_new_thread_in_the_store(
    store_dir: Path,
) -> None:
    with pytest.raises(KeyError):
        langgraph_flow.resume("no-such-run", GOOD, store_dir)

    with sqlite3.connect(store_dir / langgraph_flow.DB_FILE) as conn:
        threads = {row[0] for row in conn.execute("SELECT thread_id FROM checkpoints")}
    assert threads == {"no-such-run"}


def test_langgraph_resume_with_none_fails_inside_the_framework(
    store_dir: Path,
) -> None:
    paused = langgraph_flow.start(OVER_THRESHOLD_CLAIM, store_dir)

    with pytest.raises(UnboundLocalError, match="resume_is_map"):
        langgraph_flow.resume(paused.run_ref, None, store_dir)


def test_langgraph_emits_no_opentelemetry_spans_of_its_own(
    spans: InMemorySpanExporter, store_dir: Path
) -> None:
    langgraph_flow.start(OVER_THRESHOLD_CLAIM, store_dir)

    assert spans.get_finished_spans() == ()


def test_langgraph_resume_with_an_empty_object_re_pauses_silently(
    store_dir: Path,
) -> None:
    # Upstream issue #8693: `{}` is read as "a map of interrupt ids to values"
    # with no entries, so nothing is delivered and the node interrupts again.
    paused = langgraph_flow.start(OVER_THRESHOLD_CLAIM, store_dir)

    again = langgraph_flow.resume(paused.run_ref, {}, store_dir)

    assert again == paused  # no error, no decision, the same pause
    assert langgraph_flow.resume(paused.run_ref, GOOD, store_dir).status == "completed"


def _resume_writes(store_dir: Path) -> list[bytes]:
    with sqlite3.connect(store_dir / langgraph_flow.DB_FILE) as conn:
        rows = conn.execute("SELECT value FROM writes WHERE channel = '__resume__'")
        return [bytes(row[0]) for row in rows]


def test_langgraph_a_refused_payload_stays_as_a_pending_resume_write(
    store_dir: Path,
) -> None:
    paused = langgraph_flow.start(OVER_THRESHOLD_CLAIM, store_dir)
    assert _resume_writes(store_dir) == []
    bad = {"decision": "maybe", "adjuster_id": "ADJ-0001"}

    with pytest.raises(ValidationError):
        langgraph_flow.resume(paused.run_ref, bad, store_dir)

    # Refused, yet persisted: the payload sits in the store as a pending write.
    assert any(b"maybe" in value for value in _resume_writes(store_dir))
    # The next resume overwrites it.
    langgraph_flow.resume(paused.run_ref, GOOD, store_dir)
    assert not any(b"maybe" in value for value in _resume_writes(store_dir))


def test_langgraph_a_plain_continue_replays_a_refused_payload(
    store_dir: Path,
) -> None:
    paused = langgraph_flow.start(OVER_THRESHOLD_CLAIM, store_dir)
    bad = {"decision": "maybe", "adjuster_id": "ADJ-0001"}
    with pytest.raises(ValidationError):
        langgraph_flow.resume(paused.run_ref, bad, store_dir)
    config = {"configurable": {"thread_id": paused.run_ref}}

    with SqliteSaver.from_conn_string(str(store_dir / langgraph_flow.DB_FILE)) as saver:
        graph = langgraph_flow.build_graph(saver)
        with pytest.raises(ValidationError):
            graph.invoke(None, config)  # the documented "continue" call


def test_langgraph_replaying_the_pause_checkpoint_lands_a_second_decision(
    store_dir: Path,
) -> None:
    # Time travel: invoking with a config pinned to the pause checkpoint forks the
    # thread from it and pauses again; a second resume then lands another decision.
    paused = langgraph_flow.start(OVER_THRESHOLD_CLAIM, store_dir)
    first = langgraph_flow.resume(paused.run_ref, GOOD, store_dir)
    thread = {"configurable": {"thread_id": paused.run_ref}}

    with SqliteSaver.from_conn_string(str(store_dir / langgraph_flow.DB_FILE)) as saver:
        graph = langgraph_flow.build_graph(saver)
        pause = next(s for s in graph.get_state_history(thread) if s.next)
        replayed = graph.invoke(None, pause.config)
        graph.invoke(Command(resume=OTHER), thread)
        head = graph.get_state(thread).values

    assert (first.outcome, first.adjuster_id) == ("approve", "ADJ-0001")
    assert "__interrupt__" in replayed  # the fork paused again
    assert (head["outcome"], head["adjuster_id"]) == ("reject", "ADJ-0002")


def test_langgraph_two_concurrent_resumes_are_never_refused_and_can_disagree(
    store_dir: Path,
) -> None:
    """Outcome varies between runs, so this asserts what holds every time.

    In a scratch loop of 30, roughly half the runs showed both callers
    "completed" with different decisions, and the rest showed both callers
    getting the same one (either decision won). Every time: no error, both
    callers get `completed`, and the stored decision is one of the two. The test
    retries up to ATTEMPTS fresh pauses until the two callers see different
    outcomes, which is the lost update: both were told their decision was
    recorded, and the thread holds one of them.
    """
    disagreed = False
    for attempt in range(ATTEMPTS):
        store = store_dir / f"attempt-{attempt}"
        paused = langgraph_flow.start(OVER_THRESHOLD_CLAIM, store)

        good, other = _resume_concurrently(langgraph_flow.resume, paused.run_ref, store)

        for result in (good, other):
            assert not isinstance(result, BaseException), result
            assert result.status == "completed"
            assert (result.outcome, result.adjuster_id) in {
                ("approve", "ADJ-0001"),
                ("reject", "ADJ-0002"),
            }
        config = {"configurable": {"thread_id": paused.run_ref}}
        with SqliteSaver.from_conn_string(str(store / langgraph_flow.DB_FILE)) as saver:
            stored = langgraph_flow.build_graph(saver).get_state(config).values
        assert (stored["outcome"], stored["adjuster_id"]) in {
            ("approve", "ADJ-0001"),
            ("reject", "ADJ-0002"),
        }
        if (good.outcome, good.adjuster_id) != (other.outcome, other.adjuster_id):
            disagreed = True
            break

    assert disagreed, "the two resumes never produced different outcomes"


def test_langgraph_a_resume_pinned_to_the_pause_checkpoint_does_not_fork(
    store_dir: Path,
) -> None:
    # Unlike MAF's one call, the pinned resume alone returns the first decision.
    paused = langgraph_flow.start(OVER_THRESHOLD_CLAIM, store_dir)
    langgraph_flow.resume(paused.run_ref, GOOD, store_dir)
    thread = {"configurable": {"thread_id": paused.run_ref}}

    with SqliteSaver.from_conn_string(str(store_dir / langgraph_flow.DB_FILE)) as saver:
        graph = langgraph_flow.build_graph(saver)
        pause = next(s for s in graph.get_state_history(thread) if s.next)
        result = graph.invoke(Command(resume=OTHER), pause.config)

    assert (result["outcome"], result["adjuster_id"]) == ("approve", "ADJ-0001")


class _NodeSpans(BaseCallbackHandler):
    """One OpenTelemetry span per graph node, from LangChain's callback surface.

    LangGraph tags each node run `graph:step:<n>`; its internal runnables (the
    graph itself, the router) carry other tags and are skipped.
    """

    def __init__(self) -> None:
        self._open: dict[UUID, trace.Span] = {}

    def on_chain_start(
        self,
        serialized: Any,
        inputs: Any,
        *,
        run_id: UUID,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        if not any(tag.startswith("graph:step:") for tag in tags or []):
            return
        node = (metadata or {}).get("langgraph_node", "unknown")
        tracer = trace.get_tracer("claimflow.langgraph")
        self._open[run_id] = tracer.start_span(f"langgraph.node {node}")

    def on_chain_end(self, outputs: Any, *, run_id: UUID, **kwargs: Any) -> None:
        if (span := self._open.pop(run_id, None)) is not None:
            span.end()

    def on_chain_error(
        self, error: BaseException, *, run_id: UUID, **kwargs: Any
    ) -> None:
        if (span := self._open.pop(run_id, None)) is None:
            return
        if not isinstance(error, GraphInterrupt):  # a pause is not a failure
            span.set_status(Status(StatusCode.ERROR, type(error).__name__))
        span.end()


def _traced_invoke(store_dir: Path, payload: Any) -> None:
    config = {
        "configurable": {"thread_id": "traced"},
        "callbacks": [_NodeSpans()],
    }
    store_dir.mkdir(parents=True, exist_ok=True)
    with SqliteSaver.from_conn_string(str(store_dir / langgraph_flow.DB_FILE)) as saver:
        langgraph_flow.build_graph(saver).invoke(payload, config)


def test_langgraph_callbacks_give_one_span_per_node_and_a_pause_is_not_an_error(
    spans: InMemorySpanExporter, store_dir: Path
) -> None:
    _traced_invoke(store_dir, {"claim_id": OVER_THRESHOLD_CLAIM})
    _traced_invoke(store_dir, Command(resume=GOOD))

    finished = spans.get_finished_spans()
    assert [span.name for span in finished] == [
        "langgraph.node validate",
        "langgraph.node assess",
        "langgraph.node approval",  # ended by the GraphInterrupt of the pause
        "langgraph.node approval",  # the resumed run
    ]
    assert all(span.status.status_code is StatusCode.UNSET for span in finished)


def test_langgraph_callbacks_mark_a_failing_node_span_as_an_error(
    spans: InMemorySpanExporter, store_dir: Path
) -> None:
    _traced_invoke(store_dir, {"claim_id": OVER_THRESHOLD_CLAIM})
    spans.clear()

    with pytest.raises(ValidationError):
        _traced_invoke(store_dir, Command(resume={"decision": "maybe"}))

    (failed,) = spans.get_finished_spans()
    assert failed.status.status_code is StatusCode.ERROR
