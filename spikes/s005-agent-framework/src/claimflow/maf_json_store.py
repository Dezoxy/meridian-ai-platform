"""A pickle-free `CheckpointStorage` for Microsoft Agent Framework: JSON on disk.

`FileCheckpointStorage` writes a pickle for every non-primitive value.
`CheckpointStorage` is a public protocol, so a flow can bring its own store
instead. This one uses only names exported by `agent_framework`, writes plain
JSON, and rebuilds an application type only if it is in the registry passed to
the constructor: a file naming any other type is refused, never imported.

Scope: it encodes what this spike's flow puts in a checkpoint (primitives,
lists, string-keyed dicts, workflow messages and request events, registered
dataclasses). Anything else fails the save, which MAF logs as a warning while
the run carries on (README section 7).
"""

import dataclasses
import json
import os
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any

from agent_framework import WorkflowCheckpoint, WorkflowEvent, WorkflowMessage
from agent_framework.exceptions import WorkflowCheckpointException

_PRIMITIVES = (bool, int, float, str)


def _registry_key(cls: type) -> str:
    return f"{cls.__module__}:{cls.__qualname__}"


class JsonCheckpointStorage:
    """One `<checkpoint_id>.json` per checkpoint; no pickle anywhere."""

    def __init__(self, directory: Path, types: Iterable[type]) -> None:
        self._directory = Path(directory)
        self._directory.mkdir(parents=True, exist_ok=True)
        registered = list(types)
        self._types = {_registry_key(cls): cls for cls in registered}
        # WorkflowEvent.from_dict wants "module.qualname" names for its own types.
        self._event_types = {f"{c.__module__}.{c.__qualname__}": c for c in registered}

    # --- encoding ------------------------------------------------------------

    def _encode(self, value: Any) -> Any:
        if value is None or isinstance(value, _PRIMITIVES):
            return value
        if isinstance(value, list):
            return [self._encode(item) for item in value]
        if isinstance(value, dict):
            # A "__" key would collide with the markers below, so it is refused.
            if not all(isinstance(k, str) and not k.startswith("__") for k in value):
                raise TypeError("only string keys without a '__' prefix are stored")
            return {key: self._encode(item) for key, item in value.items()}
        if isinstance(value, WorkflowEvent):
            fields = value.to_dict()
            return {"__event__": {**fields, "data": self._encode(fields["data"])}}
        if isinstance(value, WorkflowMessage):
            fields = value.to_dict()
            return {
                "__message__": {
                    **fields,
                    "data": self._encode(fields["data"]),
                    "original_request_info_event": self._encode(
                        fields["original_request_info_event"]
                    ),
                }
            }
        key = _registry_key(type(value))
        if dataclasses.is_dataclass(value) and key in self._types:
            return {
                "__dataclass__": key,
                "fields": {
                    field.name: self._encode(getattr(value, field.name))
                    for field in dataclasses.fields(value)
                },
            }
        raise TypeError(f"unregistered checkpoint type {key}")

    def _decode(self, value: Any) -> Any:
        if isinstance(value, list):
            return [self._decode(item) for item in value]
        if not isinstance(value, dict):
            return value
        if "__event__" in value:
            fields = value["__event__"]
            event = {**fields, "data": self._decode(fields["data"])}
            return WorkflowEvent.from_dict(event, allowed_types=self._event_types)
        if "__message__" in value:
            fields = value["__message__"]
            return WorkflowMessage.from_dict(
                {
                    **fields,
                    "data": self._decode(fields["data"]),
                    "original_request_info_event": self._decode(
                        fields["original_request_info_event"]
                    ),
                }
            )
        if "__dataclass__" in value:
            cls = self._types[value["__dataclass__"]]  # KeyError: not registered
            return cls(**{k: self._decode(v) for k, v in value["fields"].items()})
        return {key: self._decode(item) for key, item in value.items()}

    # --- the CheckpointStorage protocol ---------------------------------------

    def _path(self, checkpoint_id: str) -> Path:
        path = (self._directory / f"{checkpoint_id}.json").resolve()
        if not path.is_relative_to(self._directory.resolve()):
            raise WorkflowCheckpointException(f"Invalid checkpoint ID: {checkpoint_id}")
        return path

    def _read(self, path: Path) -> WorkflowCheckpoint:
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
            return WorkflowCheckpoint.from_dict(self._decode(document))
        except Exception as error:
            raise WorkflowCheckpointException(
                f"cannot read checkpoint {path.stem}: {error}"
            ) from error

    async def save(self, checkpoint: WorkflowCheckpoint) -> str:
        try:
            text = json.dumps(self._encode(checkpoint.to_dict()), indent=2)
            self._decode(json.loads(text))  # refuse now what could not be restored
        except Exception as error:
            raise WorkflowCheckpointException(
                f"cannot save checkpoint {checkpoint.checkpoint_id}: {error}"
            ) from error
        path = self._path(checkpoint.checkpoint_id)
        temporary = path.with_name(f".{path.name}.tmp")
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, path)  # a reader never sees half a file
        return checkpoint.checkpoint_id

    async def load(self, checkpoint_id: str) -> WorkflowCheckpoint:
        path = self._path(checkpoint_id)
        if not path.exists():
            raise WorkflowCheckpointException(
                f"No checkpoint found with ID {checkpoint_id}"
            )
        return self._read(path)

    async def list_checkpoints(self, *, workflow_name: str) -> list[WorkflowCheckpoint]:
        checkpoints = [self._read(path) for path in self._directory.glob("*.json")]
        return [cp for cp in checkpoints if cp.workflow_name == workflow_name]

    async def list_checkpoint_ids(self, *, workflow_name: str) -> list[str]:
        checkpoints = await self.list_checkpoints(workflow_name=workflow_name)
        return [cp.checkpoint_id for cp in checkpoints]

    async def get_latest(self, *, workflow_name: str) -> WorkflowCheckpoint | None:
        checkpoints = await self.list_checkpoints(workflow_name=workflow_name)
        if not checkpoints:
            return None
        return max(checkpoints, key=lambda cp: datetime.fromisoformat(cp.timestamp))

    async def delete(self, checkpoint_id: str) -> bool:
        path = self._path(checkpoint_id)
        if not path.exists():
            return False
        path.unlink()
        return True
