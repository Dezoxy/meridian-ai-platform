"""JSON for a ``WorkflowCheckpoint``: no pickle anywhere on the path.

Adapted from ``spikes/s005-agent-framework/src/claimflow/maf_json_store.py``.
``encode`` takes primitives, lists, string-keyed dicts, ``WorkflowEvent``,
``WorkflowMessage`` and registered dataclasses, and raises ``TypeError`` for
anything else. ``decode`` builds only registered dataclasses: a document that
names any other type is refused, never imported.
"""

import dataclasses
from collections.abc import Iterable
from typing import Any

from agent_framework import WorkflowCheckpoint, WorkflowEvent, WorkflowMessage

_PRIMITIVES = (bool, int, float, str)


def registry_key(cls: type) -> str:
    return f"{cls.__module__}:{cls.__qualname__}"


class CheckpointCodec:
    def __init__(self, types: Iterable[type]) -> None:
        registered = list(types)
        self._types = {registry_key(cls): cls for cls in registered}
        # WorkflowEvent.from_dict wants "module.qualname" names for its own types.
        self._event_types = {f"{c.__module__}.{c.__qualname__}": c for c in registered}

    def encode(self, value: Any) -> Any:
        if value is None or isinstance(value, _PRIMITIVES):
            return value
        if isinstance(value, list):
            return [self.encode(item) for item in value]
        if isinstance(value, dict):
            # A "__" key would collide with the markers below, so it is refused.
            if not all(isinstance(k, str) and not k.startswith("__") for k in value):
                raise TypeError("only string keys without a '__' prefix are stored")
            return {key: self.encode(item) for key, item in value.items()}
        if isinstance(value, WorkflowEvent):
            fields = value.to_dict()
            return {"__event__": {**fields, "data": self.encode(fields["data"])}}
        if isinstance(value, WorkflowMessage):
            fields = value.to_dict()
            return {
                "__message__": {
                    **fields,
                    "data": self.encode(fields["data"]),
                    "original_request_info_event": self.encode(
                        fields["original_request_info_event"]
                    ),
                }
            }
        key = registry_key(type(value))
        if dataclasses.is_dataclass(value) and key in self._types:
            return {
                "__dataclass__": key,
                "fields": {
                    field.name: self.encode(getattr(value, field.name))
                    for field in dataclasses.fields(value)
                },
            }
        raise TypeError(f"unregistered checkpoint type {key}")

    def decode(self, value: Any) -> Any:
        if isinstance(value, list):
            return [self.decode(item) for item in value]
        if not isinstance(value, dict):
            return value
        if "__event__" in value:
            fields = value["__event__"]
            event = {**fields, "data": self.decode(fields["data"])}
            return WorkflowEvent.from_dict(event, allowed_types=self._event_types)
        if "__message__" in value:
            fields = value["__message__"]
            return WorkflowMessage.from_dict(
                {
                    **fields,
                    "data": self.decode(fields["data"]),
                    "original_request_info_event": self.decode(
                        fields["original_request_info_event"]
                    ),
                }
            )
        if "__dataclass__" in value:
            cls = self._types[value["__dataclass__"]]  # KeyError: not registered
            return cls(**{k: self.decode(v) for k, v in value["fields"].items()})
        return {key: self.decode(item) for key, item in value.items()}

    def to_document(self, checkpoint: WorkflowCheckpoint) -> Any:
        return self.encode(checkpoint.to_dict())

    def from_document(self, document: Any) -> WorkflowCheckpoint:
        return WorkflowCheckpoint.from_dict(self.decode(document))
