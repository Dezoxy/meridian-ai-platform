"""The scaffold's edit of ``services.yaml`` (S061).

The Agent Runtime's identity may name an agent in a call only when the agent is in
the ``agents`` of its entry (``may_name``): the new agent is appended to that
list. The edit is made as text, inside the brackets of the one flow-style list on
one line (a YAML writer would drop the file's comments), and verified before it
is returned: the new text must parse to the old parse with exactly that name
appended to that list. A tenant must still list the agent before any call for it
is admitted; the scaffold never writes ``tenants.yaml``.

Every refusal is a fixed text that never quotes the name; a field holds a line
number of the person's own file. ``ServicesEditError`` is not ``ScaffoldError``
because ``scaffold`` imports this module: it wraps the one into the other.
"""

import yaml

from meridian.platform.registry.service_checks import RUNTIME_SERVICE

SERVICES_PATH = "config/registry/services.yaml"
SERVICES_NOT_YAML = "services.yaml cannot be read as YAML"
SERVICES_RUNTIME_MISSING = (
    f"the edit of services.yaml does not verify: no entry has `id: {RUNTIME_SERVICE}`; "
    "nothing was written"
)
# The field is the lines where the entries start, "3 and 9".
SERVICES_RUNTIME_TWICE = (
    f"the edit of services.yaml does not verify: the entry `id: {RUNTIME_SERVICE}` "
    "must be there once; entries start on lines {}; nothing was written"
)
# The field is the line where the entry starts.
SERVICES_AGENTS_MISSING = (
    f"the edit of services.yaml does not verify: the entry `id: {RUNTIME_SERVICE}` "
    "that starts on line {} has no `agents` key; nothing was written"
)
# The field is the line of the `agents` key.
SERVICES_AGENTS_UNUSABLE = (
    f"the edit of services.yaml does not verify: the `agents` of `{RUNTIME_SERVICE}`, "
    "on line {}, is not a list in flow style on one line; the text edit extends "
    "only `agents: [a, b]`; nothing was written"
)
SERVICES_AGENT_LISTED = (
    f"the edit of services.yaml does not verify: the `agents` of `{RUNTIME_SERVICE}`, "
    "on line {}, already lists the agent; nothing was written"
)
SERVICES_EDIT_UNVERIFIED = (
    "the edit of services.yaml does not verify: appending to the `agents` list "
    "on line {} does not give the old file plus one name; nothing was written"
)


class ServicesEditError(Exception):
    """Refused; the message is a fixed text and never quotes the name."""


def _value_of(entry: yaml.MappingNode, key: str) -> list[tuple[yaml.Node, yaml.Node]]:
    """The (key node, value node) pairs of ``entry`` whose key is the text ``key``."""
    return [
        (name, value)
        for name, value in entry.value
        if isinstance(name, yaml.ScalarNode) and name.value == key
    ]


def _has_id(entry: yaml.MappingNode) -> bool:
    return any(
        isinstance(value, yaml.ScalarNode) and value.value == RUNTIME_SERVICE
        for _, value in _value_of(entry, "id")
    )


def _runtime_entries(document: yaml.Node | None) -> list[yaml.MappingNode]:
    """The entries of the top-level ``services`` list whose ``id`` is the runtime's."""
    if not isinstance(document, yaml.MappingNode):
        return []
    entries: list[yaml.MappingNode] = []
    for services in (value for _, value in _value_of(document, "services")):
        if isinstance(services, yaml.SequenceNode):
            entries += [
                entry
                for entry in services.value
                if isinstance(entry, yaml.MappingNode) and _has_id(entry)
            ]
    return entries


def _runtime_agents(old: str) -> tuple[yaml.Node, yaml.Node]:
    """The key node and the value node of the runtime's one ``agents``; raise
    ``ServicesEditError`` when there is no entry, two, or no ``agents`` key."""
    try:
        entries = _runtime_entries(yaml.compose(old))
    except yaml.YAMLError:
        raise ServicesEditError(SERVICES_NOT_YAML) from None
    if not entries:
        raise ServicesEditError(SERVICES_RUNTIME_MISSING)
    lines = [str(entry.start_mark.line + 1) for entry in entries]
    if len(entries) > 1:
        numbers = ", ".join(lines[:-1]) + " and " + lines[-1]
        raise ServicesEditError(SERVICES_RUNTIME_TWICE.format(numbers))
    found = _value_of(entries[0], "agents")
    if not found:
        raise ServicesEditError(SERVICES_AGENTS_MISSING.format(lines[0]))
    return found[0]


def services_edit(old: str, name: str) -> str:
    """``old``, the text of ``services.yaml``, with ``name`` appended to the
    ``agents`` of the Agent Runtime's entry, verified."""
    key, value = _runtime_agents(old)
    line = key.start_mark.line + 1
    one_line = value.start_mark.line == value.end_mark.line
    if not (isinstance(value, yaml.SequenceNode) and value.flow_style and one_line):
        raise ServicesEditError(SERVICES_AGENTS_UNUSABLE.format(line))
    listed = [item.value for item in value.value if isinstance(item, yaml.ScalarNode)]
    if name in listed:
        raise ServicesEditError(SERVICES_AGENT_LISTED.format(line))
    # After the last item, so that a trailing comma and the spaces before `]` stay;
    # an empty list has none, and the name goes before its `]`.
    close = value.end_mark.index - 1
    if value.value:
        at, text = value.value[-1].end_mark.index, f", {name}"
    else:
        at, text = close, name
    if old[close] != "]":
        raise ServicesEditError(SERVICES_EDIT_UNVERIFIED.format(line))
    new = old[:at] + text + old[at:]
    try:
        expected = yaml.safe_load(old)
        runtime = next(s for s in expected["services"] if s["id"] == RUNTIME_SERVICE)
        runtime["agents"].append(name)
        verified = yaml.safe_load(new) == expected
    except (yaml.YAMLError, KeyError, TypeError, StopIteration, AttributeError):
        verified = False
    if not verified:
        raise ServicesEditError(SERVICES_EDIT_UNVERIFIED.format(line))
    return new
