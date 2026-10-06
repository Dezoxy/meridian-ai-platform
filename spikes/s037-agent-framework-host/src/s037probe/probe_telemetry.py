"""A process of its own for probe 9: a global tracer provider can be set once
per process, so "none" and "global" cannot share one.

``python -m s037probe.probe_telemetry none|global`` runs the probe workflow over
the framework's in-memory store and prints one line of JSON.
"""

import json
import sys

from agent_framework import InMemoryCheckpointStorage
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from s037probe.flow import CLAIM, build_workflow
from s037probe.rig import leg, plain_deps

BRIEF = "a synthetic brief"


def main(mode: str) -> int:
    exporter = InMemorySpanExporter()
    if mode == "global":
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        trace.set_tracer_provider(provider)
    deps = plain_deps()
    workflow = build_workflow(deps, InMemoryCheckpointStorage())

    leg(lambda: workflow.run(CLAIM))

    spans = exporter.get_finished_spans()
    values = [str(v) for s in spans for v in (s.attributes or {}).values()]
    print(
        json.dumps(
            {
                "provider": type(trace.get_tracer_provider()).__name__,
                "valid_in_steps": deps.span_valid_in_step,
                "spans": sorted({s.name for s in spans}),
                "executor_ids": sorted(
                    {
                        str(s.attributes["executor.id"])
                        for s in spans
                        if s.attributes and "executor.id" in s.attributes
                    }
                ),
                "payload_in_attributes": any(BRIEF in v or CLAIM in v for v in values),
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
