"""Hashes that say when a baseline's inputs changed: prompt, tools, golden set."""

import hashlib
import json
import re
from collections.abc import Mapping
from pathlib import Path, PurePosixPath

from pydantic import ValidationError

from meridian.platform.evaluation.report import (
    GoldenSet,
    ReportError,
    describe_validation_error,
    read_json_file,
)
from meridian.platform.registry.models import Registry

GOLDEN_SET_KEYS = ("generator_version", "seed", "files")
FILES_DIFFER = "the golden set's files differ from its manifest"
# A path from the manifest is the file's own text: it is named in an error only
# when it is plain, and short.
NAMEABLE_PATH = re.compile(r"[A-Za-z0-9_./-]+")
MAX_NAMED_PATH_CHARS = 100
HASH_CHUNK_BYTES = 64 * 1024


def canonical_sha256(value: object) -> str:
    """The SHA-256 of ``value`` as compact JSON with sorted keys."""
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _file_sha256(root: Path, name: str) -> str | None:
    """The SHA-256 of the file ``name`` under ``root`` (already resolved), or
    None when the path is absolute, climbs out, escapes through a symlink, or is
    not a readable regular file."""
    relative = PurePosixPath(name)
    if relative.is_absolute() or ".." in relative.parts:
        return None
    try:
        target = (root / name).resolve()
        if not target.is_relative_to(root) or not target.is_file():
            return None
        digest = hashlib.sha256()
        with target.open("rb") as stream:
            while chunk := stream.read(HASH_CHUNK_BYTES):
                digest.update(chunk)
    except (OSError, RuntimeError):  # a symlink loop raises RuntimeError
        return None
    return digest.hexdigest()


def _verify_files(directory: Path, files: Mapping[str, str]) -> None:
    """Raise ``ReportError`` unless every listed file hashes as the manifest
    says; the first mismatch, in path order, is the one named."""
    root = directory.resolve()
    for name in sorted(files):
        if _file_sha256(root, name) != files[name]:
            if NAMEABLE_PATH.fullmatch(name) and len(name) <= MAX_NAMED_PATH_CHARS:
                raise ReportError(f"{FILES_DIFFER}: {name}")
            raise ReportError(FILES_DIFFER)


def golden_set_of(manifest_path: Path) -> GoldenSet:
    """The version, seed and file hashes of a golden set's ``manifest.json``,
    after checking the files themselves, and the hash of the whole manifest."""
    manifest = read_json_file(manifest_path)
    if not isinstance(manifest, dict):
        raise ReportError("the manifest is not a JSON object")
    try:
        golden_set = GoldenSet.model_validate(
            {key: manifest[key] for key in GOLDEN_SET_KEYS if key in manifest}
            | {"manifest": canonical_sha256(manifest)}
        )
    except ValidationError as exc:
        raise ReportError(describe_validation_error(exc)) from None
    _verify_files(manifest_path.parent, golden_set.files)
    return golden_set


def tools_fingerprint(registry: Registry, agent_id: str) -> str:
    """A hash of an agent's registry entry and of every tool entry it lists.

    It does not cover the ``Server`` entries those tools name: a change to a
    server's endpoint or scope does not change this fingerprint.
    """
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
