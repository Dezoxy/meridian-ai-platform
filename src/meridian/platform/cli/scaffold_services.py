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

from meridian.platform.registry.loader import MERGE_TAG
from meridian.platform.registry.service_checks import RUNTIME_SERVICE

SERVICES_PATH = "config/registry/services.yaml"
# The parser gave no line to name.
SERVICES_NOT_YAML = "services.yaml cannot be read as YAML"
# The field is the line of the person's own file where the parser stopped; the
# parser's own text is never quoted, it may hold the file's content.
SERVICES_NOT_YAML_AT = (
    "services.yaml cannot be read as YAML: the parser stopped at line {}"
)
# The field is the line of the first anchor or alias, or else of the first merge
# key: the text edit follows neither, so it could not see what its edit changes.
SERVICES_SHARED_NODE = (
    "the edit of services.yaml does not verify: line {} uses an anchor, an alias or "
    "a merge key, which the text edit does not follow; nothing was written"
)
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


def line_of(text: str, mark: yaml.Mark) -> int:
    """The 1-based line of ``text`` at ``mark``, as an editor counts it: only a line
    feed ends a line. The parser also counts U+0085, U+2028 and U+2029 (and a lone
    carriage return), which a person does not see as breaks. A mark at the very end
    of a text that ends with a line feed is on the last line, not the one after."""
    return text.count("\n", 0, min(mark.index, max(len(text) - 1, 0))) + 1


def _not_yaml(old: str, exc: yaml.YAMLError) -> str:
    """The refusal for a text the parser stopped on: with the line it gave (a
    mark, or the position of a character it cannot read), else without one."""
    mark = getattr(exc, "problem_mark", None)
    if mark is not None:
        return SERVICES_NOT_YAML_AT.format(line_of(old, mark))
    if isinstance(exc, yaml.reader.ReaderError):
        return SERVICES_NOT_YAML_AT.format(old.count("\n", 0, max(exc.position, 0)) + 1)
    return SERVICES_NOT_YAML


def _merge_key_indexes(node: yaml.Node) -> list[int]:
    """The character indexes of the merge keys in the tree under ``node``. Meant
    for a tree without aliases: with one a node can be its own descendant."""
    if isinstance(node, yaml.SequenceNode):
        return [index for child in node.value for index in _merge_key_indexes(child)]
    if not isinstance(node, yaml.MappingNode):
        return []
    found = [key.start_mark.index for key, _ in node.value if key.tag == MERGE_TAG]
    for key, value in node.value:
        found += _merge_key_indexes(key) + _merge_key_indexes(value)
    return found


def _shared_node_line(old: str, document: yaml.Node | None) -> int | None:
    """The 1-based line of the first anchor or alias of ``old``, or else of its
    first merge key, or ``None``. The registry's loader refuses all three, but this
    function stands on its own: ``yaml.compose`` resolves an alias to the anchor's
    node, so an anchor is found among the parser's events, and a merge key (which
    the loader refuses only when it constructs) in the composed tree."""
    for event in yaml.parse(old, Loader=yaml.SafeLoader):
        if getattr(event, "anchor", None) is not None:
            return line_of(old, event.start_mark)
    merges = _merge_key_indexes(document) if document is not None else []
    return old.count("\n", 0, min(merges)) + 1 if merges else None


def _runtime_agents(old: str) -> tuple[yaml.Node, yaml.Node]:
    """The key node and the value node of the runtime's one ``agents``; raise
    ``ServicesEditError`` when the text is not YAML, uses an anchor, an alias or a
    merge key, has no entry for the runtime, two, or no ``agents`` key."""
    try:
        document = yaml.compose(old, Loader=yaml.SafeLoader)
    except yaml.YAMLError as exc:
        raise ServicesEditError(_not_yaml(old, exc)) from None
    except RecursionError:
        # `compose` recurses with the nesting; `parse` below does not.
        raise ServicesEditError(SERVICES_NOT_YAML) from None
    line = _shared_node_line(old, document)
    if line is not None:
        raise ServicesEditError(SERVICES_SHARED_NODE.format(line))
    entries = _runtime_entries(document)
    if not entries:
        raise ServicesEditError(SERVICES_RUNTIME_MISSING)
    lines = [str(line_of(old, entry.start_mark)) for entry in entries]
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
    line = line_of(old, key.start_mark)
    # The editor's lines, not the parser's: a U+2028 inside a quoted item is a break
    # to the parser only. The parse check below still guards the result.
    one_line = "\n" not in old[value.start_mark.index : value.end_mark.index]
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
    except (
        yaml.YAMLError,
        KeyError,
        TypeError,
        StopIteration,
        AttributeError,
        RecursionError,  # `compose` fit the text; the loads that verify need more
    ):
        verified = False
    if not verified:
        raise ServicesEditError(SERVICES_EDIT_UNVERIFIED.format(line))
    return new
