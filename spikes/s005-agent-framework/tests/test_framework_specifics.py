"""Facts about one framework that the shared observation tests cannot express."""

import asyncio
import sqlite3
from pathlib import Path

import pytest
from agent_framework import FileCheckpointStorage, WorkflowBuilder
from agent_framework.exceptions import WorkflowCheckpointException
from claimflow import langgraph_flow, maf_flow, rules
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
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


def _personal_data() -> dict[str, str]:
    claim = rules.validate(OVER_THRESHOLD_CLAIM).claim
    return {
        "name": claim["claimant"]["name"],
        "email": claim["claimant"]["email"],
        "description": claim["description"][:40],
    }


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
    paused = maf_flow.start(OVER_THRESHOLD_CLAIM, store_dir)

    decoded = {cid: text for cid, _, text in _maf_records(store_dir)}[paused.run_ref]
    assert "ApprovalRequest" in decoded
    assert not any(value in decoded for value in _personal_data().values())


def test_maf_checkpoint_save_failure_does_not_fail_the_run(
    store_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # With the application types off the allowlist, every save is refused.
    monkeypatch.setattr(maf_flow, "ALLOWED_CHECKPOINT_TYPES", [])

    async def run() -> None:
        workflow = maf_flow.build_workflow(store_dir)
        result = await workflow.run(OVER_THRESHOLD_CLAIM)
        request_ids = [event.request_id for event in result.get_request_info_events()]
        assert request_ids  # the run reached the pause and reported it ...
        assert (
            await workflow.resolve_pause_checkpoint_id(request_ids) is None
        )  # ... unsaved

    asyncio.run(run())
    assert len(list(store_dir.glob("*.json"))) == 1  # only the entry checkpoint


def test_maf_refuses_to_restore_a_checkpoint_into_a_changed_graph(
    store_dir: Path,
) -> None:
    paused = maf_flow.start(OVER_THRESHOLD_CLAIM, store_dir)
    changed = (
        WorkflowBuilder(
            name=maf_flow.WORKFLOW_NAME,
            start_executor=maf_flow.validate_step,
            checkpoint_storage=FileCheckpointStorage(
                store_dir, allowed_checkpoint_types=maf_flow.ALLOWED_CHECKPOINT_TYPES
            ),
        )
        .add_edge(maf_flow.validate_step, maf_flow.assess_step)
        .build()
    )

    with pytest.raises(WorkflowCheckpointException, match="graph has changed"):
        asyncio.run(changed.run(checkpoint_id=paused.run_ref))


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
