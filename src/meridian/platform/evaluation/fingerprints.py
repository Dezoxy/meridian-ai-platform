"""Hashes that say when a baseline's inputs changed: prompt, tools, golden set."""

import hashlib
import json
from pathlib import Path

from pydantic import ValidationError

from meridian.platform.evaluation.report import (
    GoldenSet,
    ReportError,
    describe_validation_error,
    read_text_file,
)
from meridian.platform.registry.models import Registry

GOLDEN_SET_KEYS = ("generator_version", "seed", "files")


def canonical_sha256(value: object) -> str:
    """The SHA-256 of ``value`` as compact JSON with sorted keys."""
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def golden_set_of(manifest_path: Path) -> GoldenSet:
    """The version, seed and file hashes of a golden set's ``manifest.json``."""
    text = read_text_file(manifest_path)
    try:
        manifest = json.loads(text)
    except json.JSONDecodeError:
        raise ReportError("the manifest is not valid JSON") from None
    if not isinstance(manifest, dict):
        raise ReportError("the manifest is not a JSON object")
    try:
        return GoldenSet.model_validate(
            {key: manifest[key] for key in GOLDEN_SET_KEYS if key in manifest}
        )
    except ValidationError as exc:
        raise ReportError(describe_validation_error(exc)) from None


def tools_fingerprint(registry: Registry, agent_id: str) -> str:
    """A hash of an agent's registry entry and of every tool it may call."""
    agent = registry.agent(agent_id)
    if agent is None:
        raise ReportError(f"unknown agent {agent_id!r}")
    tools = []
    for tool_id in sorted(agent.tools):
        tool = registry.tool(tool_id)
        if tool is None:
            raise ReportError(f"agent {agent_id!r} names an unknown tool {tool_id!r}")
        tools.append(tool.model_dump(mode="json"))
    return canonical_sha256({"agent": agent.model_dump(mode="json"), "tools": tools})
