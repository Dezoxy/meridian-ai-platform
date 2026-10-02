"""What each tool server answers to ``tools/list``, generated from the registry.

Free of the MCP SDK: the registry alone says what a server publishes, and the
snapshot files under ``api/mcp`` are rendered from it (the CLI writes and
checks them). A server is published once it has a tool and every tool has an
output schema; every server of the registry has them now, so each publishes a
contract.
"""

import copy
import json
from pathlib import Path
from typing import Any

from meridian.platform.registry.models import Registry, Tool

SUFFIX = ".json"


def _listing(tool: Tool) -> dict[str, Any]:
    return {
        "name": tool.id,
        "description": tool.description,
        "inputSchema": copy.deepcopy(tool.input_schema),
        "outputSchema": copy.deepcopy(tool.output_schema),
        "annotations": {
            "readOnlyHint": tool.effect == "read",
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
        # What enforcement depends on: a change to one of them in the registry
        # changes the published contract.
        "_meta": {
            "meridian/scope": tool.scope,
            "meridian/idempotency-key-required": tool.idempotency_key_required,
            "meridian/approval-required": tool.approval_required,
        },
    }


def tool_listing(registry: Registry, server_id: str) -> list[dict[str, Any]]:
    """The server's tools in registry order, as ``tools/list`` shows them.
    The schemas are copies: the caller may change them."""
    return [_listing(tool) for tool in registry.tools if tool.server == server_id]


def published_servers(registry: Registry) -> tuple[str, ...]:
    """The servers with at least one tool, every tool with an output schema."""
    published: list[str] = []
    for server in registry.servers:
        tools = [tool for tool in registry.tools if tool.server == server.id]
        if tools and all(tool.output_schema is not None for tool in tools):
            published.append(server.id)
    return tuple(published)


def render_contract(registry: Registry, server_id: str) -> str:
    server = next(s for s in registry.servers if s.id == server_id)
    document = {
        "server": server.id,
        "description": server.description,
        "tools": tool_listing(registry, server_id),
    }
    return json.dumps(document, indent=2, ensure_ascii=False) + "\n"


def contract_problems(registry: Registry, directory: Path) -> tuple[str, ...]:
    """One message per file that is missing or out of date, and per ``*.json``
    that no published server owns."""
    published = published_servers(registry)
    problems: list[str] = []
    for server_id in published:
        path = directory / f"{server_id}{SUFFIX}"
        if not path.is_file():
            problems.append(f"{path} is missing: run `meridian registry contracts`")
        elif path.read_text(encoding="utf-8") != render_contract(registry, server_id):
            problems.append(f"{path} is out of date: run `meridian registry contracts`")
    owned = {f"{server_id}{SUFFIX}" for server_id in published}
    if directory.is_dir():
        problems += [
            f"{path} belongs to no published server: delete it"
            for path in sorted(directory.glob(f"*{SUFFIX}"))
            if path.name not in owned
        ]
    return tuple(problems)


def write_contracts(registry: Registry, directory: Path) -> list[str]:
    """Write every published server's file; return the names that changed."""
    directory.mkdir(parents=True, exist_ok=True)
    changed: list[str] = []
    for server_id in published_servers(registry):
        path = directory / f"{server_id}{SUFFIX}"
        text = render_contract(registry, server_id)
        if not path.is_file() or path.read_text(encoding="utf-8") != text:
            path.write_bytes(text.encode("utf-8"))
            changed.append(path.name)
    return changed
