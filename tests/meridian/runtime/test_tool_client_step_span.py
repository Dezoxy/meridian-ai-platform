"""A tool call's span names the step it was made from (S069).

The step is the graph's own word for a call site, checked against
``STEP_PATTERN`` before anything is sent, never a value from a request.
"""

import uuid
from typing import Any

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from servicesupport import REGISTRY_DIR
from toolsupport import AGENT, CLAIM, POLICY, Routed, StandIn, tracer_of

from meridian.platform.common.telemetry import SPAN_ATTRIBUTE_KEYS
from meridian.platform.registry import load_registry
from meridian.runtime.tool_client import SPAN_NAME, STEP_PATTERN, ToolClient

NOTE = {"claim_id": CLAIM, "note": "A synthetic note."}


@pytest.fixture
def exporter() -> InMemorySpanExporter:
    return InMemorySpanExporter()


def client_for(exporter: InMemorySpanExporter) -> Routed:
    """A client called as the graph calls it: through its worker's view."""
    stand_in = StandIn()
    registry = load_registry(REGISTRY_DIR)
    client = ToolClient(
        {"claims-mcp": stand_in.server, "policy-mcp": stand_in.server},
        registry=registry,
        agent=AGENT,
        run_id=uuid.uuid4(),
        tracer=tracer_of(exporter),
        on_refusal=lambda tool: None,
        on_worker_refusal=lambda tool, reason, worker: None,
        max_calls=10,
    )
    return Routed(client, registry, AGENT)


def tool_spans(exporter: InMemorySpanExporter) -> list[Any]:
    return [s for s in exporter.get_finished_spans() if s.name == SPAN_NAME]


def test_the_step_key_is_one_the_allowlist_holds_beside_the_nodes() -> None:
    assert "meridian.step" in SPAN_ATTRIBUTE_KEYS
    assert "meridian.node" in SPAN_ATTRIBUTE_KEYS


def test_a_write_calls_span_carries_the_step_it_was_made_from(
    exporter: InMemorySpanExporter,
) -> None:
    client = client_for(exporter)

    client.call("add_claim_note", NOTE, step="file-note")

    (span,) = tool_spans(exporter)
    assert span.attributes["meridian.step"] == "file-note"
    assert span.attributes["meridian.tool"] == "add_claim_note"


def test_a_read_calls_span_has_no_step_key(exporter: InMemorySpanExporter) -> None:
    client = client_for(exporter)

    client.call("policy_lookup", {"policy_number": POLICY})

    (span,) = tool_spans(exporter)
    assert "meridian.step" not in span.attributes


def test_the_step_a_span_carries_always_fits_the_pattern(
    exporter: InMemorySpanExporter,
) -> None:
    client = client_for(exporter)

    with pytest.raises(ValueError, match="step"):
        client.call("add_claim_note", NOTE, step=f"{POLICY} {CLAIM}")
    client.call("add_claim_note", NOTE, step="plan.step-2_x")

    (span,) = tool_spans(exporter)  # the refused step left no span at all
    assert STEP_PATTERN.fullmatch(span.attributes["meridian.step"])
