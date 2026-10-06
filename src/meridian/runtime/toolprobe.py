"""A probe of the tool servers through the runtime's own client (S044):
``python -m meridian.runtime.toolprobe``.

It makes one call per tool server of the registry, with a run ID that does not
exist. The servers check the run before anything else of the caller's, so the
expected answer of every server is the refusal ``unknown-run``. That answer
proves the runtime's address for the server, the name lookup, the server's Host
allowlist, the MCP handshake and the server's database role reading
``runtime.runs``; it needs no claim and changes nothing but, at most once per
throttle window, a refusal's audit row.
"""

import os
import ssl
import sys
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from opentelemetry.trace import NoOpTracer
from pydantic import ValidationError

from meridian.platform.common.env import SettingsError
from meridian.platform.common.tls import verify_of
from meridian.platform.registry import Registry, RegistryError, load_registry
from meridian.platform.registry.models import Tool
from meridian.runtime.settings import RuntimeSettings
from meridian.runtime.tool_client import (
    ToolClient,
    ToolError,
    ToolRefused,
    ToolTarget,
)

EXPECTED = "unknown-run"
# The answers that are not a refusal's reason word.
UNAVAILABLE = "unavailable"
NO_ADDRESS = "no-address"
NO_TOOL = "no-tool"
COMPLETED = "completed"
# The tool column of a server no agent may call a tool of.
NO_TOOL_NAME = "-"
# The graph's name for the call site of a write tool's idempotency key.
STEP = "probe"


@dataclass(frozen=True, slots=True)
class Answer:
    server: str
    tool: str
    answer: str


def first_tool(registry: Registry, server: str) -> tuple[str, Tool] | None:
    """The first pair of an agent and a tool of its allowlist that ``server``
    serves, agents and allowlists in registry order."""
    for agent in registry.agents:
        for tool_id in agent.tools:
            tool = registry.tool(tool_id)
            if tool is not None and tool.server == server:
                return agent.id, tool
    return None


def call_once(
    registry: Registry,
    target: ToolTarget,
    server: str,
    agent: str,
    tool: Tool,
    verify: ssl.SSLContext | bool = True,
) -> Answer:
    """One call with an empty argument object and a run ID nobody has."""
    client = ToolClient(
        {server: target},
        registry=registry,
        agent=agent,
        run_id=uuid.uuid4(),
        tracer=NoOpTracer(),
        # The probe picks the tool from the agent's own allowlist, so the
        # allowlist never refuses and there is nothing to audit here.
        on_refusal=lambda _tool: None,
        max_calls=1,  # the client makes one call
        verify=verify,
    )
    step = STEP if tool.idempotency_key_required else None
    # An agent with workers calls through the view of the worker that holds the
    # tool (the registry gives every tool of such an agent one), as a graph does
    # (S031); the client itself would refuse the call before it was sent.
    declared = registry.agent(agent)
    workers = declared.workers if declared is not None else ()
    holder = next((w.id for w in workers if tool.id in w.tools), None)
    caller = client if holder is None else client.for_worker(holder)
    try:
        caller.call(tool.id, {}, step=step)
    except ToolRefused as refusal:
        return Answer(server, tool.id, refusal.reason)
    except ToolError:
        # Never the exception's text: the client has logged its class name.
        return Answer(server, tool.id, UNAVAILABLE)
    return Answer(server, tool.id, COMPLETED)


def probe(
    registry: Registry,
    addresses: Mapping[str, ToolTarget],
    verify: ssl.SSLContext | bool = True,
) -> list[Answer]:
    """One answer per server of the registry, in registry order. ``verify`` is
    how the runtime's own client reaches an address over TLS."""
    answers: list[Answer] = []
    for server in registry.servers:
        chosen = first_tool(registry, server.id)
        if chosen is None:
            answers.append(Answer(server.id, NO_TOOL_NAME, NO_TOOL))
            continue
        agent, tool = chosen
        target = addresses.get(server.id)
        if target is None:
            answers.append(Answer(server.id, tool.id, NO_ADDRESS))
            continue
        answers.append(call_once(registry, target, server.id, agent, tool, verify))
    return answers


def succeeded(answers: Sequence[Answer]) -> bool:
    """At least one server, and every one answered ``unknown-run``."""
    return bool(answers) and all(a.answer == EXPECTED for a in answers)


def main(
    environ: Mapping[str, str] = os.environ,
    *,
    targets: Mapping[str, ToolTarget] | None = None,
) -> int:
    """Print one line per server and return the exit status. ``targets`` (tests)
    replaces the settings' addresses, as it does for ``create_app``."""
    try:
        settings = RuntimeSettings.from_env(environ)
        registry = load_registry(settings.registry_dir)
        # The runtime's own certificate and CA, as ``create_app`` builds them.
        verify = verify_of(settings.client_tls)
    except (SettingsError, RegistryError) as error:
        # The text names a variable or a rule, never a value.
        text = "; ".join(str(error).splitlines())
        sys.stderr.write(f"ERROR {text}\n")
        return 1
    except ValidationError as error:
        # Only the field names: pydantic's own text can carry a value.
        fields = dict.fromkeys(".".join(map(str, e["loc"])) for e in error.errors())
        sys.stderr.write(
            f"ERROR the runtime's settings are not valid: {', '.join(fields)}\n"
        )
        return 1
    servers = settings.tool_servers if targets is None else targets
    answers = probe(registry, servers, verify)
    for answer in answers:
        sys.stdout.write(f"{answer.server} {answer.tool} {answer.answer}\n")
    return 0 if succeeded(answers) else 1


if __name__ == "__main__":
    sys.exit(main())
