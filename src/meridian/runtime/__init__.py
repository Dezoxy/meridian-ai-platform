"""The Agent Runtime: hosts LangGraph graphs behind the platform's run API."""

import os

SERVICE_NAME = "agent-runtime"

# LangGraph reads this once, when it is first imported, so it must be set
# before any module imports langgraph (ADR 2 appendix).
os.environ["LANGGRAPH_STRICT_MSGPACK"] = "true"

# LangSmith's default endpoint is outside the EU and its tracer would send the
# claim (name, email, description) there. Nothing reaches a model or a tracer
# but through the gateway and OTLP, so LangSmith is switched off before
# anything can read these variables. The names that were truthy are kept so
# that create_app can refuse to start instead of silently giving an operator
# who asked for LangSmith nothing.
LANGSMITH_TRACING_VARIABLES = (
    "LANGSMITH_TRACING",
    "LANGSMITH_TRACING_V2",
    "LANGCHAIN_TRACING_V2",
    "LANGCHAIN_TRACING",
)
_TRUTHY = frozenset({"true", "1", "yes"})
LANGSMITH_REQUESTED_BY: tuple[str, ...] = tuple(
    name
    for name in LANGSMITH_TRACING_VARIABLES
    if os.environ.get(name, "").strip().lower() in _TRUTHY
)
for _name in LANGSMITH_TRACING_VARIABLES:
    os.environ[_name] = "false"
