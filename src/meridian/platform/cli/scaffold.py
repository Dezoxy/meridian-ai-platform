"""Plan and write a new workload into a checkout of this repository (S039, T-81).

``plan_workload`` reads the tree and returns what would be written; it writes
nothing. ``write_plan`` (in ``scaffold_writes``, with its undo) writes it. Every
edit of an existing file is made as text, after the last line of its table or
list or inside the brackets of one line (a YAML or TOML writer would drop the
file's comments), and is verified before the plan is returned: the new
``pyproject.toml`` must parse to the old one plus exactly two entry points, and a
copy of the registry with the new ``agents.yaml`` and ``services.yaml`` must
validate (``scaffold_services`` makes the second edit and checks it parses to the
old one plus one name).

The command declares and never grants: the agent lists no tool and no tenant
lists it, so the gateway refuses its model calls and no tool server answers it
until a person adds both in a reviewed change (T-81). The one thing it writes
beyond the agent itself is the Agent Runtime's right to name it in a call
(``services.yaml``); no call is admitted before a tenant lists the agent.

The templates are text files filled with ``string.Template``: a ``.py`` file
under ``meridian.platform`` that imports LangGraph would break the import
contract (hard rule 5). Only ``$name`` and ``$module`` are substituted. Every
refusal is a fixed text that never quotes the name; the one place a name can
appear is the path of a file left behind after a failed write, which the person
has to find and which a kind of path would not help them find.
"""

import copy
import hashlib
import json
import keyword
import os
import re
import shutil
import string
import tempfile
import tomllib
from collections.abc import Mapping
from importlib import resources
from pathlib import Path
from types import MappingProxyType
from typing import Any

import yaml

from meridian.platform.cli.scaffold_services import (
    SERVICES_PATH,
    ServicesEditError,
    line_of,
    services_edit,
)
from meridian.platform.cli.scaffold_writes import (
    AGENTS_PATH,
    AN_INTERRUPT,
    AN_UNEXPECTED_ERROR,
    CHECKING_THE_PLAN,
    CREATING_A_FILE,
    LEFT_AS_SAVED,
    LEFT_BEHIND,
    MAKING_A_DIRECTORY,
    PATH_EXISTS,
    PLAN_CHANGED,
    PYPROJECT_PATH,
    REPLACING,
    ROLLBACK_FAILED,
    STALE_PLAN,
    WRITE_FAILED,
    WRITE_ORDER,
    Plan,
    ScaffoldError,
    ScaffoldWriteError,
    _digest,
    write_plan,
)
from meridian.platform.common.entry_points import EVALUATIONS_GROUP, GRAPHS_GROUP
from meridian.platform.registry.loader import RegistryError, load_registry

# What moved to ``scaffold_writes`` and is still imported from here: the command,
# the errors, the plan and the fixed texts of the write.
__all__ = [
    "AN_INTERRUPT",
    "AN_UNEXPECTED_ERROR",
    "CHECKING_THE_PLAN",
    "CREATING_A_FILE",
    "LEFT_AS_SAVED",
    "LEFT_BEHIND",
    "MAKING_A_DIRECTORY",
    "PATH_EXISTS",
    "PLAN_CHANGED",
    "REPLACING",
    "ROLLBACK_FAILED",
    "STALE_PLAN",
    "WRITE_FAILED",
    "WRITE_ORDER",
    "Plan",
    "ScaffoldError",
    "ScaffoldWriteError",
    "plan_workload",
    "write_plan",
]

MAX_NAME_CHARS = 40
# The registry's agent ID, with a length limit. Used with fullmatch: an end
# anchor would accept a trailing newline.
NAME = re.compile(r"[a-z][a-z0-9]*(-[a-z0-9]+)*")

# Fixed texts: none quotes the name, which is the caller's own and may hold
# anything. The two that name an exception name its type only.
BAD_NAME = (
    f"the name must be at most {MAX_NAME_CHARS} characters: lower-case letters and "
    "digits, starting with a letter, groups joined by single hyphens, and a "
    "Python identifier that is no keyword once hyphens become underscores"
)
NOT_A_CHECKOUT = (
    "the directory is not a checkout of the meridian repository: run the command "
    "at the checkout's root or pass --root"
)
# The parser gave no line to name: bytes that are not UTF-8, or an error "at end
# of document".
PYPROJECT_NOT_TOML = "pyproject.toml cannot be read as TOML"
# The field is the line of the person's own file where the parser stopped; the
# parser's own text is never quoted, it may hold the file's content.
PYPROJECT_NOT_TOML_AT = (
    "pyproject.toml cannot be read as TOML: the parser stopped at line {}"
)
# The end of a TOML error's text, where it names the position it stopped at.
TOML_POSITION = re.compile(r"\(at line (\d+), column \d+\)\Z")
REGISTRY_INVALID = (
    "the registry does not validate: run `meridian registry validate --registry-dir` "
    "on the checkout's config/registry and fix it first"
)
REGISTRY_UNREADABLE = "the registry cannot be read ({})"
AMBIGUOUS_NAME = "the name is a word YAML reads as something other than text"
# The field holds the kinds of thing that hold the name, never the name or a path.
NAME_TAKEN = "the name is taken: held by {}"
HELD_BY_AGENT = "an agent of the registry"
HELD_BY_ENTRY_POINT = "an entry point of the {} group"
# What each path ``_taken_paths`` checks is, in the same order.
HELD_BY_PATH = (
    "the workload's package",
    "the workload's module file",
    "the workload's tests",
    "the workload's evaluation data",
)
AGENTS_EDIT_UNVERIFIED = (
    "the edit of agents.yaml does not verify: the text edit extends only a list in "
    "block style with `agents` last in the file; nothing was written"
)
# The file's last value is a block scalar with no final newline: the newline the
# new entry needs would become part of that value.
AGENTS_NO_FINAL_NEWLINE = (
    "the edit of agents.yaml does not verify: the file does not end with a newline, "
    "and adding one changes the value before the new entry (a block scalar at the "
    "end of the file); end the file with a newline and run the command again; "
    "nothing was written"
)
# The field is the line, in the person's own file, where the `agents` list starts.
AGENTS_LIST_UNUSABLE = (
    "the edit of agents.yaml does not verify: the `agents` list starts on line {}; "
    "the text edit extends only a list in block style with `agents` last in the "
    "file; nothing was written"
)
REGISTRY_EDIT_INVALID = "the registry with the new agent does not validate"
REGISTRY_COPY_FAILED = "the registry could not be copied to check the edit ({})"
PYPROJECT_EDIT_UNVERIFIED = (
    "the edit of pyproject.toml does not verify; nothing was written"
)
# The field is the line of the person's own file where the table's header is.
PYPROJECT_TABLE_UNVERIFIED = (
    "the edit of pyproject.toml does not verify: adding the entry point to the "
    "table whose header is on line {} does not give the old file plus one entry; "
    "nothing was written"
)
# The fields are the table header and the lines of the person's own file that
# start with it.
PYPROJECT_HEADER_UNUSABLE = (
    "the edit of pyproject.toml does not verify: the scaffold needs the table "
    "header {} alone on its line, and once; lines that start with it: {}; "
    "nothing was written"
)
PATH_OUTSIDE = (
    "a path the workload would be written to leaves the checkout or is a symbolic link"
)

REGISTRY_DIRECTORY = "config/registry"
TEMPLATES = ("templates", "workload")
# Template file -> where its rendering goes; {module} and {name} are filled in.
RENDERED_FILES = {
    "__init__.py.tmpl": "src/meridian/workloads/{module}/__init__.py",
    "graph.py.tmpl": "src/meridian/workloads/{module}/graph.py",
    "evaluation.py.tmpl": "src/meridian/workloads/{module}/evaluation.py",
    "test_scaffold.py.tmpl": (
        "tests/meridian/workloads/{module}/test_{module}_scaffold.py"
    ),
}
# Directories the command writes into or reads the registry from: each must
# resolve to a path inside the checkout. The first two are required to exist
# (``_read_pyproject`` checks the workloads one and the registry's agents file);
# the others are looked at when they do.
CONTAINED_DIRECTORIES = ("src/meridian/workloads", REGISTRY_DIRECTORY)
CONTAINED_WHEN_PRESENT = (
    "tests/meridian/workloads",
    "tests/meridian",
    "tests",
    "data/evaluation",
    "data",
)
# Files the command replaces: a symbolic link would send the edit elsewhere.
EDITED_FILES = (PYPROJECT_PATH, AGENTS_PATH, SERVICES_PATH)
GOLDEN_DIRECTORY = "data/evaluation/{name}/golden"
CASES = "[]\n"
AGENT_DESCRIPTION = (
    "The {name} workload, generated by `meridian workload new`; it calls no tool."
)
# Lines that split a text into lines without dropping or changing a byte; unlike
# str.splitlines, only a line feed ends a line.
LINES = re.compile(r"[^\n]*\n|[^\n]+")
# A line that can be a table header: "[", anything, "]", then at most a comment.
# A superset of the headers: whether it is one is the parser's to say.
HEADER_SHAPE = re.compile(r"\[.*\](?:\s*#.*)?")


def _module_of(name: str) -> str:
    """The module name for ``name``; refuse a name that is not safe to put in a
    path, a YAML value or a TOML key, or that YAML would not read as text."""
    if len(name) > MAX_NAME_CHARS or NAME.fullmatch(name) is None:
        raise ScaffoldError(BAD_NAME)
    module = name.replace("-", "_")
    if not module.isidentifier() or keyword.iskeyword(module):
        raise ScaffoldError(BAD_NAME)
    # Such an ID (yes, no, on, off, true, false, null) would also be read as a
    # boolean or null wherever a person lists the agent later, for example in a
    # tenant's ``agents: [...]``. Derived from the parser, not from a word list.
    if yaml.safe_load(name) != name:
        raise ScaffoldError(AMBIGUOUS_NAME)
    return module


def _read_pyproject(root: Path) -> tuple[str, dict[str, Any]]:
    """The text of ``pyproject.toml`` and what it parses to, for a checkout of
    this repository; raise ``ScaffoldError`` for any other tree."""
    try:
        text = (root / PYPROJECT_PATH).read_bytes().decode("utf-8")
        document = tomllib.loads(text)
    except OSError:
        raise ScaffoldError(NOT_A_CHECKOUT) from None
    except (UnicodeDecodeError, RecursionError):
        # No line either: the parser stopped on its own depth, not on a position.
        raise ScaffoldError(PYPROJECT_NOT_TOML) from None
    except tomllib.TOMLDecodeError as exc:
        # The error has no line attribute: the number is read from the end of its
        # text with a fixed pattern, and printed alone.
        position = TOML_POSITION.search(str(exc))
        if position is None:
            raise ScaffoldError(PYPROJECT_NOT_TOML) from None
        raise ScaffoldError(PYPROJECT_NOT_TOML_AT.format(position[1])) from None
    project = document.get("project")
    if not isinstance(project, dict) or project.get("name") != "meridian":
        raise ScaffoldError(NOT_A_CHECKOUT)
    entry_points = project.get("entry-points")
    if not isinstance(entry_points, dict) or not all(
        isinstance(entry_points.get(group), dict)
        for group in (GRAPHS_GROUP, EVALUATIONS_GROUP)
    ):
        raise ScaffoldError(NOT_A_CHECKOUT)
    try:
        has_agents_file = (root / AGENTS_PATH).is_file()
    except OSError as exc:
        # The one place the registry's being unreadable reaches this module as an
        # error: `load_registry` turns its own into a `RegistryError`, but this
        # stat is made before it (a directory with no search bit).
        raise ScaffoldError(REGISTRY_UNREADABLE.format(type(exc).__name__)) from None
    if not has_agents_file:
        raise ScaffoldError(NOT_A_CHECKOUT)
    if not (root / "src" / "meridian" / "workloads").is_dir():
        raise ScaffoldError(NOT_A_CHECKOUT)
    return text, document


def _refuse_paths_outside_the_checkout(root: Path) -> None:
    """Refuse a tree where a directory the command writes to resolves outside
    ``root`` or a file it edits is a symbolic link."""
    inside = root.resolve()
    directories = [
        *CONTAINED_DIRECTORIES,
        *(r for r in CONTAINED_WHEN_PRESENT if os.path.lexists(root / r)),
    ]
    escapes = any(
        not (root / relative).resolve().is_relative_to(inside)
        for relative in directories
    )
    if escapes or any((root / relative).is_symlink() for relative in EDITED_FILES):
        raise ScaffoldError(PATH_OUTSIDE)


def _taken_paths(name: str, module: str) -> tuple[str, ...]:
    return (
        f"src/meridian/workloads/{module}",
        f"src/meridian/workloads/{module}.py",
        f"tests/meridian/workloads/{module}",
        f"data/evaluation/{name}",
    )


def _refuse_a_taken_name(
    root: Path, name: str, module: str, document: Mapping[str, Any], agent_exists: bool
) -> None:
    entry_points = document["project"]["entry-points"]
    holders = [HELD_BY_AGENT] if agent_exists else []
    holders += [
        HELD_BY_ENTRY_POINT.format(group)
        for group in (GRAPHS_GROUP, EVALUATIONS_GROUP)
        if name in entry_points[group]
    ]
    holders += [
        kind
        for kind, relative in zip(HELD_BY_PATH, _taken_paths(name, module), strict=True)
        if os.path.lexists(root / relative)
    ]
    if holders:
        raise ScaffoldError(NAME_TAKEN.format(_listed(holders)))


def _listed(items: list[str]) -> str:
    """``items`` as prose: "a", "a and b", "a, b and c"."""
    return " and ".join([", ".join(items[:-1]), items[-1]] if items[1:] else items)


def _template(file: str) -> string.Template:
    directory = resources.files("meridian.platform.cli").joinpath(*TEMPLATES)
    return string.Template(directory.joinpath(file).read_text(encoding="utf-8"))


def _created_files(name: str, module: str) -> dict[str, str]:
    created = {
        RENDERED_FILES[file].format(module=module): _template(file).substitute(
            name=name, module=module
        )
        for file in RENDERED_FILES
    }
    golden = GOLDEN_DIRECTORY.format(name=name)
    digest = hashlib.sha256(CASES.encode("utf-8")).hexdigest()
    # An empty set has no generator, so the manifest claims none.
    manifest = {
        "workload": name,
        "generator_version": "none",
        "seed": 0,
        "files": {"cases.json": digest},
    }
    created[f"{golden}/cases.json"] = CASES
    created[f"{golden}/manifest.json"] = json.dumps(manifest, indent=2) + "\n"
    return created


def _read_text(root: Path, relative: str) -> str:
    try:
        return (root / relative).read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        raise ScaffoldError(NOT_A_CHECKOUT) from None


def _agents_edit(old: str, name: str) -> str:
    """``old``, the text of ``agents.yaml``, with the new agent appended, verified
    as a parse: the registry check is made on both registry files together."""
    description = AGENT_DESCRIPTION.format(name=name)
    ending = "\r\n" if "\r\n" in old else "\n"
    block = ending.join(
        [f"  - id: {name}", f"    description: {description}", "    tools: []", ""]
    )
    new = old + ("" if old.endswith("\n") else ending) + block
    agent = {"id": name, "description": description, "tools": []}
    try:
        before = yaml.safe_load(old)
        expected = {**before, "agents": [*before["agents"], agent]}
        verified = yaml.safe_load(new) == expected
    except (yaml.YAMLError, TypeError, KeyError):
        # The parse error's own mark is on the line the scaffold appended, not on
        # one of the person's: the person's list is found in the old text.
        line = _agents_list_line(old)
        if line is None:
            raise ScaffoldError(AGENTS_EDIT_UNVERIFIED) from None
        raise ScaffoldError(AGENTS_LIST_UNUSABLE.format(line)) from None
    if verified:
        return new
    # With no final newline the new entry's newline can change the last value (a
    # block scalar); the text for that case says so, the other keeps its reason.
    if not old.endswith("\n") and yaml.safe_load(old + ending) != yaml.safe_load(old):
        raise ScaffoldError(AGENTS_NO_FINAL_NEWLINE)
    raise ScaffoldError(AGENTS_EDIT_UNVERIFIED)


def _services_edit(old: str, name: str) -> str:
    """``old``, the text of ``services.yaml``, with the agent in the runtime's
    ``agents``, verified as a parse."""
    try:
        return services_edit(old, name)
    except ServicesEditError as exc:
        raise ScaffoldError(str(exc)) from None


def _validated(root: Path, texts: Mapping[str, str]) -> None:
    """Refuse when a copy of the registry holding the new registry files, by file
    name, does not validate."""
    try:
        _registry_with(root / REGISTRY_DIRECTORY, texts)
    except RegistryError as exc:
        raise ScaffoldError(
            REGISTRY_EDIT_INVALID, details=_registry_details(exc)
        ) from None
    except OSError as exc:
        raise ScaffoldError(REGISTRY_COPY_FAILED.format(type(exc).__name__)) from None


def _agents_list_line(text: str) -> int | None:
    """The 1-based line of ``text`` where the value of its top-level ``agents`` key
    starts, or ``None`` when ``text`` has no such key."""
    try:
        document = yaml.compose(text, Loader=yaml.SafeLoader)
    except yaml.YAMLError:
        return None
    if not isinstance(document, yaml.MappingNode):
        return None
    for key, value in document.value:
        if isinstance(key, yaml.ScalarNode) and key.value == "agents":
            return line_of(text, value.start_mark)
    return None


def _registry_details(exc: RegistryError) -> tuple[str, ...]:
    """The registry's own messages, as ``meridian eval run`` prints them."""
    return tuple(f"the registry: {message}" for message in exc.errors)


def _registry_with(registry_directory: Path, texts: Mapping[str, str]) -> None:
    """Validate a copy of the registry in which each file of ``texts`` (by file
    name, as ``agents.yaml``) has the text it maps to."""
    with tempfile.TemporaryDirectory() as temporary:
        staged = Path(temporary) / "registry"
        shutil.copytree(registry_directory, staged)
        for file, text in texts.items():
            (staged / file).write_bytes(text.encode("utf-8"))
        load_registry(staged)


def _header_line(lines: list[str], group: str) -> int:
    """The index in ``lines`` of the header line of the table of ``group``, which
    must be the header alone on its line, once. Raise ``ScaffoldError`` otherwise:
    with the 1-based numbers of the lines that start with the header when there
    are any (a trailing comment, a second copy), without them when there are none."""
    header = f'[project.entry-points."{group}"]'
    starting = [i for i, each in enumerate(lines) if each.lstrip().startswith(header)]
    alone = [i for i in starting if lines[i].strip() == header]
    if len(starting) == 1 and alone == starting:
        return starting[0]
    if not starting:
        raise ScaffoldError(PYPROJECT_EDIT_UNVERIFIED)
    numbers = _listed([str(i + 1) for i in starting])
    raise ScaffoldError(PYPROJECT_HEADER_UNUSABLE.format(header, numbers))


def _parsed(text: str) -> dict[str, Any] | None:
    """What ``text`` parses to as TOML, or ``None`` when it does not."""
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return None


def _could_be_a_header(line: str) -> bool:
    """Whether ``line`` can be a table header: with a trailing comment and the space
    around it removed, it starts with ``[`` and ends with ``]``, and it is valid TOML
    on its own, as every header is. A line of an array (``  [1],``, ``  [1], # ]``)
    or of a key is no candidate, so it costs no parse of the text before it. A ``#``
    inside a quoted key (``["a#b"]``) does not hide a header: the pattern asks for a
    ``]`` followed by a comment or by nothing, wherever the ``]`` is. The parse of
    one line is as long as the line."""
    stripped = line.strip()
    return HEADER_SHAPE.fullmatch(stripped) is not None and (
        _parsed(stripped + "\n") is not None
    )


def _table_end(lines: list[str], start: int) -> int:
    """The index of the line where the table whose header is at ``start`` ends: the
    first later line that can be a header (``_could_be_a_header``; TOML ignores
    indentation), with a complete document before it; the end of the file when
    there is none. A candidate inside an array or a multi-line string is no header:
    the text before it ends inside a value and does not parse. The parser gives no
    positions, so it is asked, and only on a candidate: the text before it is
    joined and parsed for no other line."""
    for index in range(start + 1, len(lines)):
        if not _could_be_a_header(lines[index]):
            continue
        if _parsed("".join(lines[:index])) is not None:
            return index
    return len(lines)


def _with_entry_point(text: str, group: str, line: str) -> str:
    """``text`` with ``line`` after the last key line of the table of ``group``
    (see ``_table_end`` for where the table ends); the blank and comment lines
    after its last key stay after ``line``. Raise ``ScaffoldError`` when the
    table's header line is not usable."""
    lines = LINES.findall(text)
    start = _header_line(lines, group)
    last = start
    for index in range(start + 1, _table_end(lines, start)):
        if lines[index].strip() and not lines[index].lstrip().startswith("#"):
            last = index
    if lines[last].endswith("\n"):
        ending = "\r\n" if lines[last].endswith("\r\n") else "\n"
        lines.insert(last + 1, line + ending)
    else:
        # The last line of a file with no final newline: it gets the file's line
        # ending, and the inserted line, now last, has none.
        lines[last] += "\r\n" if "\r\n" in text else "\n"
        lines.append(line)
    return "".join(lines)


def _pyproject_edit(
    old: str, document: Mapping[str, Any], name: str, module: str
) -> str:
    """The text of ``pyproject.toml`` with the two entry points, verified."""
    values = {
        GRAPHS_GROUP: f"meridian.workloads.{module}.graph:build",
        EVALUATIONS_GROUP: f"meridian.workloads.{module}.evaluation:EVALUATION",
    }
    # Found in the person's own text first: once the first line is inserted, a line
    # number found in the edited text would be off by one for a later table.
    headers = {group: _header_line(LINES.findall(old), group) + 1 for group in values}
    new = old
    try:
        expected = copy.deepcopy(document)
        for group, value in values.items():
            new = _with_entry_point(new, group, f'{name} = "{value}"')
            expected["project"]["entry-points"][group][name] = value
            if _parsed(new) != expected:
                raise ScaffoldError(PYPROJECT_TABLE_UNVERIFIED.format(headers[group]))
    except RecursionError:
        # A file nested deeper than a copy or a comparison of it can follow, though
        # the parser took it (it has a limit of its own, caught in ``_read_pyproject``):
        # refused as that is, with no line, never a traceback.
        raise ScaffoldError(PYPROJECT_NOT_TOML) from None
    return new


def plan_workload(root: Path, name: str) -> Plan:
    """What ``meridian workload new NAME`` would write under ``root``. Reads
    only. Refuse, in this order: a bad name, a tree that is not a checkout of
    this repository, a path that leaves it or a link, a registry that does not
    validate, a name that is taken and an edit that does not verify (the agents
    file, the services file, the two together as a registry, then pyproject)."""
    module = _module_of(name)
    pyproject, document = _read_pyproject(root)
    _refuse_paths_outside_the_checkout(root)
    try:
        registry = load_registry(root / REGISTRY_DIRECTORY)
    except RegistryError as exc:
        # An unreadable directory or file is a `RegistryError` too: the loader
        # turns an `OSError` into one itself.
        raise ScaffoldError(REGISTRY_INVALID, details=_registry_details(exc)) from None
    _refuse_a_taken_name(root, name, module, document, registry.agent(name) is not None)
    created = _created_files(name, module)
    agents = _read_text(root, AGENTS_PATH)
    services = _read_text(root, SERVICES_PATH)
    new_agents = _agents_edit(agents, name)
    new_services = _services_edit(services, name)
    _validated(root, {"agents.yaml": new_agents, "services.yaml": new_services})
    changed = {
        AGENTS_PATH: new_agents,
        SERVICES_PATH: new_services,
        PYPROJECT_PATH: _pyproject_edit(pyproject, document, name, module),
    }
    base = {
        AGENTS_PATH: _digest(agents),
        SERVICES_PATH: _digest(services),
        PYPROJECT_PATH: _digest(pyproject),
    }
    return Plan(
        name=name,
        module=module,
        created=MappingProxyType(created),
        changed=MappingProxyType(changed),
        base=MappingProxyType(base),
    )
