"""One span per graph node, from LangChain's callback surface.

Adapted from the S005 spike. LangGraph tags each node run ``graph:step:<n>``;
its internal runnables carry other tags and are skipped. The span is made
current while the node runs, so a call the node makes (the gateway call) is
its child.
"""

from typing import Any
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler
from langgraph.errors import GraphInterrupt
from opentelemetry import context, trace
from opentelemetry.trace import Span, Tracer

from meridian.platform.common.telemetry import mark_error, set_span_attributes
from meridian.platform.toolserver.wire import WORKER_PATTERN

NODE_TAG_PREFIX = "graph:step:"
# The key of a node's metadata that names the worker the node belongs to.
WORKER_KEY = "meridian.worker"


class NodeSpans(BaseCallbackHandler):
    def __init__(self, tracer: Tracer, *, run_id: UUID, agent: str) -> None:
        self._tracer = tracer
        self._run_id = str(run_id)
        self._agent = agent
        self._open: dict[UUID, tuple[Span, object]] = {}

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
        if not any(tag.startswith(NODE_TAG_PREFIX) for tag in tags or []):
            return
        node = str((metadata or {}).get("langgraph_node", "unknown"))
        span = self._tracer.start_span(f"langgraph.node {node}")
        attributes = {
            "meridian.node": node,
            "meridian.run_id": self._run_id,
            "meridian.agent": self._agent,
        }
        # A workload labels the nodes of a worker in their metadata (S031); a
        # node with no label belongs to no worker. Only a string in the form of
        # an ID reaches the span: anything else is left out, so nothing but an
        # identifier can be an attribute (T-03). fullmatch, and not $: $ also
        # matches before a trailing newline.
        worker = (metadata or {}).get(WORKER_KEY)
        if isinstance(worker, str) and WORKER_PATTERN.fullmatch(worker):
            attributes["meridian.worker"] = worker
        set_span_attributes(span, attributes)
        token = context.attach(trace.set_span_in_context(span))
        self._open[run_id] = (span, token)

    def on_chain_end(self, outputs: Any, *, run_id: UUID, **kwargs: Any) -> None:
        self._finish(run_id, None)

    def on_chain_error(
        self, error: BaseException, *, run_id: UUID, **kwargs: Any
    ) -> None:
        self._finish(run_id, error)

    def _finish(self, run_id: UUID, error: BaseException | None) -> None:
        opened = self._open.pop(run_id, None)
        if opened is None:
            return
        span, token = opened
        context.detach(token)
        # A pause for approval is not a failure. mark_error keeps the message
        # out: it could hold claimant text (T-03).
        if error is not None and not isinstance(error, GraphInterrupt):
            mark_error(span, error)
        span.end()
