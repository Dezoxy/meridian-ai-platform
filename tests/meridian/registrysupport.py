"""Edit one entry of a registry file, found by its key (S057).

A registry test plants a violation in a copy of ``config/registry/``. Anchoring
that edit on two adjacent lines of ``models.yaml`` breaks the day a field is
added between them, and a line that two entries share lets the edit land in
the wrong one. These helpers find the entry of ``- id: <key>``, the field by
its name inside it, and change that and nothing else.

They edit text, not parsed YAML: the files keep their comments and their
order, and the loader's messages that name a position still mean something.
Each change is a value (``set_field`` and its kin make one), applied to the
directory ``plant`` returns, so a case list can hold it where it holds a
``(file, old, new)`` edit::

    apply_changes(plant(), set_field("replay-chat", "residency", "global"))

A change that finds nothing to edit, or two things, fails with a message that
names the file, the key and the field: a test must not pass because its
violation was never planted (``plant`` has the same rule).
"""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, NamedTuple

# What the ``plant`` fixture takes: (file name, text to find, replacement).
Edit = tuple[str, str, str]
DEFAULT_FILE = "models.yaml"
# The `- ` of a list item: an entry's fields sit this far right of its dash.
ITEM_WIDTH = 2


class RegistryEditError(AssertionError):
    """The entry or the field a change names is not there, or is there twice."""


class _Entry(NamedTuple):
    start: int  # the `- id:` line
    end: int  # one past its last line that is not trailing filler
    field_indent: int


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _is_filler(line: str) -> bool:
    """A blank line or a comment: it carries no field."""
    stripped = line.strip()
    return not stripped or stripped.startswith("#")


def _key_of(line: str) -> str | None:
    """The key of a ``key: value`` line; ``None`` for filler and list items."""
    stripped = line.strip()
    if _is_filler(line) or stripped.startswith("- ") or ":" not in stripped:
        return None
    return stripped.split(":", 1)[0].strip()


def _trim_filler(lines: list[str], start: int, end: int) -> int:
    """``end`` moved back over trailing filler, never past ``start``."""
    while end - 1 > start and _is_filler(lines[end - 1]):
        end -= 1
    return end


def _find_entry(lines: list[str], file: str, key: str) -> _Entry:
    """The span of the entry of ``- id: <key>``.

    The key is matched whole (``aoai-sdc-gpt-4o`` is not ``aoai-sdc-gpt-4o-b``).
    The entry ends before the next real line at the item's indentation or less
    (the next item, or a key after the list), and the comments and blank lines
    that precede that line belong to it, not to this entry.
    """
    wanted = f"- id: {key}"
    found = [i for i, line in enumerate(lines) if line.strip() == wanted]
    if not found:
        raise RegistryEditError(f"{file}: no entry {wanted!r}")
    if len(found) > 1:
        raise RegistryEditError(f"{file}: entry {wanted!r} is there twice")
    start = found[0]
    item_indent = _indent(lines[start])
    end = start + 1
    while end < len(lines) and (
        _is_filler(lines[end]) or _indent(lines[end]) > item_indent
    ):
        end += 1
    # Trailing comments deeper than the item are the entry's; the rest are the
    # next entry's own.
    while end - 1 > start and (
        not lines[end - 1].strip()
        or (_is_filler(lines[end - 1]) and _indent(lines[end - 1]) <= item_indent)
    ):
        end -= 1
    return _Entry(start, end, item_indent + ITEM_WIDTH)


def _locate(
    lines: list[str], entry: _Entry, file: str, key: str, field: str
) -> tuple[int, int] | None:
    """The first line and the end of ``field`` in the entry, or ``None``.

    ``field`` is a name at the entry's own level (``residency``) or a dotted
    path into a nested mapping (``price.checked``). The end is one past its
    last line: a nested mapping takes its children, comments between them
    included and trailing ones not.
    """
    low, high, indent = entry.start + 1, entry.end, entry.field_indent
    span = (low, low)
    for depth, name in enumerate(field.split("."), start=1):
        matches = [
            i
            for i in range(low, high)
            if _indent(lines[i]) == indent and _key_of(lines[i]) == name
        ]
        if not matches:
            return None
        if len(matches) > 1:
            raise RegistryEditError(
                f"{file}: entry {key!r} has field {_dotted(field, depth)!r} twice"
            )
        first = matches[0]
        last = first + 1
        while last < high and (
            _is_filler(lines[last]) or _indent(lines[last]) > _indent(lines[first])
        ):
            last += 1
        last = _trim_filler(lines, first, last)
        span = (first, last)
        low, high = first + 1, last
        children = [i for i in range(low, high) if not _is_filler(lines[i])]
        indent = _indent(lines[children[0]]) if children else indent
    return span


def _dotted(field: str, depth: int) -> str:
    return ".".join(field.split(".")[:depth])


@dataclass(frozen=True)
class Change:
    """One edit of one field of one entry; ``apply`` it to a registry copy."""

    key: str
    field: str
    operation: Literal["set", "remove", "add"]
    value: str = ""
    file: str = DEFAULT_FILE

    def apply(self, directory: Path) -> None:
        path = directory / self.file
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        entry = _find_entry(lines, self.file, self.key)
        span = _locate(lines, entry, self.file, self.key, self.field)
        if self.operation == "add":
            lines = self._added(lines, entry, span)
        elif span is None:
            raise RegistryEditError(
                f"{self.file}: entry {self.key!r} has no field {self.field!r}"
            )
        elif self.operation == "remove":
            del lines[span[0] : span[1]]
        else:
            first = lines[span[0]]
            name = self.field.rsplit(".", 1)[-1]
            lines[span[0] : span[1]] = [f"{' ' * _indent(first)}{name}: {self.value}\n"]
        path.write_text("".join(lines), encoding="utf-8")

    def _added(
        self, lines: list[str], entry: _Entry, span: tuple[int, int] | None
    ) -> list[str]:
        if span is not None:
            raise RegistryEditError(
                f"{self.file}: entry {self.key!r} already has field {self.field!r}"
            )
        if "." in self.field:
            raise ValueError(f"add_field adds a field of the entry, not {self.field!r}")
        new = f"{' ' * entry.field_indent}{self.field}: {self.value}\n"
        before = lines[: entry.end]
        if before and not before[-1].endswith("\n"):
            before[-1] += "\n"
        return [*before, new, *lines[entry.end :]]


def set_field(key: str, field: str, value: str, *, file: str = DEFAULT_FILE) -> Change:
    """Make ``field`` of the entry ``key`` say ``value`` (YAML text).

    A nested mapping can be replaced whole, by a flow mapping
    (``{a: 1, b: 2}``), or one line of it by a dotted path (``price.checked``).
    """
    return Change(key, field, "set", value, file)


def remove_field(key: str, field: str, *, file: str = DEFAULT_FILE) -> Change:
    """Take ``field`` (a nested mapping with its lines) out of the entry."""
    return Change(key, field, "remove", file=file)


def add_field(key: str, field: str, value: str, *, file: str = DEFAULT_FILE) -> Change:
    """Add ``field: value`` as the last field of the entry; it must be new."""
    return Change(key, field, "add", value, file)


def apply_changes(directory: Path, *changes: Change) -> Path:
    """Apply the changes in order to the registry copy; return the directory."""
    for change in changes:
        change.apply(directory)
    return directory


def planted(plant: Callable[..., Path], *edits: Edit | Change) -> Path:
    """The ``plant`` fixture's copy with both kinds of edit applied.

    For a case list that holds ``(file, old, new)`` text edits of the other
    files beside changes of a deployment's entry: the text edits go first, in
    order, then the changes, in order.
    """
    directory = plant(*(e for e in edits if not isinstance(e, Change)))
    return apply_changes(directory, *(e for e in edits if isinstance(e, Change)))
