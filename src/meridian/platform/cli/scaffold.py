"""Plan and write a new workload into a checkout of this repository (S039, T-81).

``plan_workload`` reads the tree and returns what would be written; it writes
nothing. ``write_plan`` writes it. Every edit of an existing file is made as
text, after the last line of its table or list (a YAML or TOML writer would drop
the file's comments), and is verified before the plan is returned: the new
``pyproject.toml`` must parse to the old one plus exactly two entry points, and
a copy of the registry with the new ``agents.yaml`` must validate.

The command declares and never grants: the agent lists no tool and no tenant
lists it, so the gateway refuses its model calls and no tool server answers it
until a person adds both in a reviewed change (T-81).

The templates are text files filled with ``string.Template``: a ``.py`` file
under ``meridian.platform`` that imports LangGraph would break the import
contract (hard rule 5). Only ``$name`` and ``$module`` are substituted. Every
refusal is a fixed text that never quotes the name.
"""

import copy
import hashlib
import json
import keyword
import os
import re
import shutil
import stat
import string
import tempfile
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from types import MappingProxyType
from typing import Any

import yaml

from meridian.platform.common.entry_points import EVALUATIONS_GROUP, GRAPHS_GROUP
from meridian.platform.registry.loader import RegistryError, load_registry

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
PYPROJECT_NOT_TOML = "pyproject.toml cannot be read as TOML"
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
# The fields are the table header and the lines of the person's own file that
# start with it.
PYPROJECT_HEADER_UNUSABLE = (
    "the edit of pyproject.toml does not verify: the scaffold needs the table "
    "header {} alone on its line, and once; lines that start with it: {}; "
    "nothing was written"
)
PATH_EXISTS = "a file or directory the workload would create already exists"
STALE_PLAN = (
    "agents.yaml or pyproject.toml changed after the plan was made, or cannot be "
    "read again: nothing was written; run the command again"
)
PATH_OUTSIDE = (
    "a path the workload would be written to leaves the checkout or is a symbolic link"
)
WRITE_FAILED = "writing the workload failed ({}); what was written has been removed"
ROLLBACK_FAILED = (
    "writing the workload failed ({}) and could not be fully undone: "
    "check the working tree"
)
LEFT_BEHIND = "left behind: {}"

AGENTS_PATH = "config/registry/agents.yaml"
PYPROJECT_PATH = "pyproject.toml"
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
EDITED_FILES = (PYPROJECT_PATH, AGENTS_PATH)
GOLDEN_DIRECTORY = "data/evaluation/{name}/golden"
CASES = "[]\n"
AGENT_DESCRIPTION = (
    "The {name} workload, generated by `meridian workload new`; it calls no tool."
)
# Lines that split a text into lines without dropping or changing a byte; unlike
# str.splitlines, only a line feed ends a line.
LINES = re.compile(r"[^\n]*\n|[^\n]+")


class ScaffoldError(Exception):
    """Refused; the message is a fixed text and never quotes the name. ``details``
    are further lines the caller prints after it: the registry's own messages, or
    the paths a rollback could not put back."""

    def __init__(self, message: str, *, details: tuple[str, ...] = ()) -> None:
        super().__init__(message)
        self.details = details


class ScaffoldWriteError(ScaffoldError):
    """A write failed: not a refusal, the plan was sound."""


@dataclass(frozen=True, slots=True)
class Plan:
    name: str  # the agent ID and the entry points' name
    module: str  # name with "-" replaced by "_"
    created: Mapping[str, str]  # relative POSIX path -> text of a new file
    changed: Mapping[str, str]  # relative POSIX path -> whole new text
    # relative POSIX path -> SHA-256 of the bytes a changed file was planned from
    base: Mapping[str, str]


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
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        raise ScaffoldError(PYPROJECT_NOT_TOML) from None
    project = document.get("project")
    if not isinstance(project, dict) or project.get("name") != "meridian":
        raise ScaffoldError(NOT_A_CHECKOUT)
    entry_points = project.get("entry-points")
    if not isinstance(entry_points, dict) or not all(
        isinstance(entry_points.get(group), dict)
        for group in (GRAPHS_GROUP, EVALUATIONS_GROUP)
    ):
        raise ScaffoldError(NOT_A_CHECKOUT)
    if not (root / AGENTS_PATH).is_file():
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
        "generator_version": "none",
        "seed": 0,
        "files": {"cases.json": digest},
    }
    created[f"{golden}/cases.json"] = CASES
    created[f"{golden}/manifest.json"] = json.dumps(manifest, indent=2) + "\n"
    return created


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _refuse_a_stale_plan(root: Path, plan: Plan) -> None:
    """Refuse when a file the plan edits is not what it was planned from."""
    try:
        stale = any(
            _digest((root / relative).read_bytes().decode("utf-8")) != digest
            for relative, digest in plan.base.items()
        )
    except (OSError, UnicodeDecodeError):
        stale = True
    if stale:
        raise ScaffoldError(STALE_PLAN)


def _read_agents(root: Path) -> str:
    try:
        return (root / AGENTS_PATH).read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        raise ScaffoldError(NOT_A_CHECKOUT) from None


def _agents_edit(root: Path, old: str, name: str) -> str:
    """``old``, the text of ``agents.yaml``, with the new agent appended, verified."""
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
    if not verified:
        raise ScaffoldError(AGENTS_EDIT_UNVERIFIED)
    try:
        _registry_with(root / REGISTRY_DIRECTORY, new)
    except RegistryError as exc:
        raise ScaffoldError(
            REGISTRY_EDIT_INVALID, details=_registry_details(exc)
        ) from None
    except OSError as exc:
        raise ScaffoldError(REGISTRY_COPY_FAILED.format(type(exc).__name__)) from None
    return new


def _agents_list_line(text: str) -> int | None:
    """The 1-based line of ``text`` where the value of its top-level ``agents`` key
    starts, or ``None`` when ``text`` has no such key."""
    try:
        document = yaml.compose(text)
    except yaml.YAMLError:
        return None
    if not isinstance(document, yaml.MappingNode):
        return None
    for key, value in document.value:
        if isinstance(key, yaml.ScalarNode) and key.value == "agents":
            return value.start_mark.line + 1
    return None


def _registry_details(exc: RegistryError) -> tuple[str, ...]:
    """The registry's own messages, as ``meridian eval run`` prints them."""
    return tuple(f"the registry: {message}" for message in exc.errors)


def _registry_with(registry_directory: Path, agents_text: str) -> None:
    """Validate a copy of the registry whose ``agents.yaml`` is ``agents_text``."""
    with tempfile.TemporaryDirectory() as temporary:
        staged = Path(temporary) / "registry"
        shutil.copytree(registry_directory, staged)
        (staged / "agents.yaml").write_bytes(agents_text.encode("utf-8"))
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


def _with_entry_point(text: str, group: str, line: str) -> str:
    """``text`` with ``line`` after the last key line of the table of ``group``;
    the table ends at the next line that starts with ``[`` or at the end of the
    file, and the blank and comment lines after its last key stay after ``line``.
    Raise ``ScaffoldError`` when the table's header line is not usable."""
    lines = LINES.findall(text)
    start = _header_line(lines, group)
    last = start
    for index in range(start + 1, len(lines)):
        if lines[index].startswith("["):
            break
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
    # Checked in the person's own text first: once the first line is inserted, a
    # line number found in the edited text would be off by one for a later table.
    for group in values:
        _header_line(LINES.findall(old), group)
    new = old
    for group, value in values.items():
        new = _with_entry_point(new, group, f'{name} = "{value}"')
    expected = copy.deepcopy(document)
    for group, value in values.items():
        expected["project"]["entry-points"][group][name] = value
    try:
        verified = tomllib.loads(new) == expected
    except tomllib.TOMLDecodeError:
        verified = False
    if not verified:
        raise ScaffoldError(PYPROJECT_EDIT_UNVERIFIED)
    return new


def plan_workload(root: Path, name: str) -> Plan:
    """What ``meridian workload new NAME`` would write under ``root``. Reads
    only. Refuse, in this order: a bad name, a tree that is not a checkout of
    this repository, a path that leaves it or a link, a registry that does not
    validate, a name that is taken and an edit that does not verify."""
    module = _module_of(name)
    pyproject, document = _read_pyproject(root)
    _refuse_paths_outside_the_checkout(root)
    try:
        registry = load_registry(root / REGISTRY_DIRECTORY)
    except RegistryError as exc:
        raise ScaffoldError(REGISTRY_INVALID, details=_registry_details(exc)) from None
    except OSError as exc:
        raise ScaffoldError(REGISTRY_UNREADABLE.format(type(exc).__name__)) from None
    _refuse_a_taken_name(root, name, module, document, registry.agent(name) is not None)
    created = _created_files(name, module)
    agents = _read_agents(root)
    changed = {
        AGENTS_PATH: _agents_edit(root, agents, name),
        PYPROJECT_PATH: _pyproject_edit(pyproject, document, name, module),
    }
    base = {AGENTS_PATH: _digest(agents), PYPROJECT_PATH: _digest(pyproject)}
    return Plan(
        name=name,
        module=module,
        created=MappingProxyType(created),
        changed=MappingProxyType(changed),
        base=MappingProxyType(base),
    )


def _replace(path: Path, data: bytes) -> None:
    """Replace ``path`` with ``data`` in one step, keeping its mode: the bytes go
    to a temporary file beside it, which then takes its place."""
    mode = stat.S_IMODE(path.stat().st_mode)
    descriptor, temporary = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def _make_directories(directory: Path, made: list[Path]) -> None:
    """Create ``directory`` and the parents that are missing, noting each one
    this call made in ``made``, outermost first."""
    missing = []
    current = directory
    while not os.path.lexists(current):
        missing.append(current)
        current = current.parent
    for each in reversed(missing):
        each.mkdir()
        made.append(each)


def _failure(exc: OSError) -> str:
    """What the error says of an ``OSError``: its type and, when it carries an
    errno, the operating system's text for it. Never a path or the name."""
    if exc.errno is None:
        return type(exc).__name__
    return f"{type(exc).__name__}: {os.strerror(exc.errno)}"


def _undo(
    root: Path,
    files: list[Path],
    directories: list[Path],
    restore: Path | None,
    old: bytes,
) -> list[str]:
    """Put back what a failed write changed; the relative paths that did not go
    back, in the order tried."""
    left: list[Path] = []
    if restore is not None:
        try:
            _replace(restore, old)
        except OSError:
            left.append(restore)
    for file in files:
        try:
            file.unlink(missing_ok=True)
        except OSError:
            left.append(file)
    for directory in reversed(directories):
        try:
            directory.rmdir()
        except OSError:
            left.append(directory)
    return [path.relative_to(root).as_posix() for path in left]


def write_plan(root: Path, plan: Plan) -> None:
    """Write ``plan`` under ``root``: the new files, then ``agents.yaml``, then
    ``pyproject.toml``, whose entry points are what makes the platform load the
    workload. Refuse, before the first write, a plan whose two edited files are no
    longer what it was planned from. When a write fails, or the process is
    interrupted, remove what this call made and put ``agents.yaml`` back; an
    ``OSError`` becomes a ``ScaffoldWriteError`` and anything else propagates
    unchanged."""
    targets = [(root / relative, text) for relative, text in plan.created.items()]
    if any(os.path.lexists(path) for path, _ in targets):
        raise ScaffoldError(PATH_EXISTS)
    _refuse_a_stale_plan(root, plan)
    files: list[Path] = []
    directories: list[Path] = []
    agents = root / AGENTS_PATH
    old_agents = b""
    replaced: Path | None = None
    try:
        for path, text in targets:
            _make_directories(path.parent, directories)
            with open(path, "x", encoding="utf-8", newline="\n") as stream:
                files.append(path)
                stream.write(text)
        old_agents = agents.read_bytes()
        _replace(agents, plan.changed[AGENTS_PATH].encode("utf-8"))
        replaced = agents
        _replace(root / PYPROJECT_PATH, plan.changed[PYPROJECT_PATH].encode("utf-8"))
    except BaseException as exc:
        left = _undo(root, files, directories, replaced, old_agents)
        if not isinstance(exc, OSError):
            raise
        if not left:
            raise ScaffoldWriteError(WRITE_FAILED.format(_failure(exc))) from None
        raise ScaffoldWriteError(
            ROLLBACK_FAILED.format(_failure(exc)),
            details=tuple(LEFT_BEHIND.format(path) for path in left),
        ) from None
