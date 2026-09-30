"""The claims-triage workload: the Claims API and its LangGraph graph."""

# LangGraph reads LANGGRAPH_STRICT_MSGPACK once, when it is first imported, and
# this package's graph module imports it. A package's __init__ runs before its
# submodules, so importing the runtime here (whose __init__ sets the variable)
# keeps the order right whatever the import sorter does to graph.py.
import meridian.runtime  # noqa: F401
