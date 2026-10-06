"""The trust checks every entry-point group of the platform shares (S061).

A workload publishes its graph in the group ``meridian.graphs`` and its
evaluation in ``meridian.evaluations``. Both groups load only what the
``meridian`` distribution published, only when exactly one entry point carries
the name, only when the entry point's value names a module under
``meridian.workloads`` and only when that module's file lies in the installed
``meridian`` package: a second installed package, or a distribution that calls
itself ``meridian`` and hides the real one, cannot substitute a graph or an
evaluation. The location is read from the module's spec before the module's own
code runs, and again from the module once it has loaded. Finding the spec
imports the module's parent packages, so a parent package's ``__init__`` has
run by then, whatever the check decides; only the module itself waits for it.

``load_trusted_entry_point`` does these checks, in that order, and returns the
loaded object or raises ``EntryPointRefused`` with a closed reason. Each caller
turns the reason into its own error; ``fixed_text`` gives both the same words,
which hold nothing a foreign package chose, with two exceptions that a foreign
directory can still use. A refusal for a name nobody published lists the names
the distribution called ``meridian`` did publish, and a directory that calls
itself that can publish any: up to ``MAX_KNOWN_LISTED`` (ten) names of the
registry's ID shape, 64 characters each at most, and a count of the rest. And
the class name of an error the trusted package's own module raised at import is
kept when it is a run of up to 64 identifier characters (``safe_class_name``):
a class there can name itself like a sentence without spaces. Both need a write
to the Python path or to the trusted package, which is already a compromise, so
neither is closed here. What the loaded object must be (callable, a protocol) is
the caller's to check.

Importing this module imports the registry's models (for the ID pattern and its
length, so the two cannot drift), and through them ``pydantic``, but neither the
agent framework nor a workload. Its importers (the evaluation's loader, the
graphs' loader and the scaffold) already import the registry, so nothing is
brought in that they did not have.
"""

import importlib.util
import re
import sys
from collections.abc import Callable, Iterable
from enum import Enum
from importlib import metadata
from importlib.metadata import EntryPoint
from pathlib import Path

import meridian
from meridian.platform.registry.models import ENTITY_ID_MAX_LENGTH, ENTITY_ID_PATTERN

GRAPHS_GROUP = "meridian.graphs"
EVALUATIONS_GROUP = "meridian.evaluations"
TRUSTED_DISTRIBUTION = "meridian"
TRUSTED_VALUE_PREFIX = "meridian.workloads."
# The directory of the installed package. Tests that load a stand-in from
# outside it point their loader's own ``TRUSTED_ROOT`` at their own directory.
TRUSTED_ROOT = Path(meridian.__file__).resolve().parent
# A name in a refusal's ``known`` list must be one the registry would accept as
# an ID (the pattern is the registry's own, imported, as ``common/identity.py``
# does; ``registry`` imports nothing of ``common``, so there is no cycle). A
# directory that calls itself the trusted distribution can publish an entry
# point under any name, and a name that is no ID is never printed. At most
# ``MAX_KNOWN_LISTED`` names are listed, the rest as a count.
_LISTABLE_NAME = re.compile(ENTITY_ID_PATTERN)
MAX_KNOWN_LISTED = 10
# What a class's name becomes when it is not a short identifier or cannot be
# read: a class every refusal's cause can honestly be called.
_UNREADABLE_CLASS_NAME = "Exception"
_CLASS_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
_CLASS_NAME_OF_TYPE = type.__dict__["__name__"]

EntryPoints = Callable[..., Iterable[EntryPoint]]


class Refusal(Enum):
    PUBLISHED_TWICE = "published more than once"
    NOT_PUBLISHED = "not published"
    OTHER_DISTRIBUTION = "another distribution"
    OUTSIDE_WORKLOADS = "a module outside the workloads"
    # The spec cannot be read at all (a parent that does not import, a name that
    # is no module name).
    UNLOCATABLE = "the module's spec cannot be read"
    # No spec, no file, or a file outside the trusted root, before the load.
    OUTSIDE_ROOT = "the module is not found or outside the trusted root"
    FAILED_TO_IMPORT = "failed to import"
    MOVED_OUTSIDE_ROOT = "outside the trusted root once loaded"


class LoadFailure(Exception):
    """The cause a refusal carries for an error the loaded code raised: the
    error's class name as its only text, no traceback and no message. A plain
    traceback of a refusal shows ``LoadFailure: ImportError`` and nothing the
    failing module wrote."""


def safe_class_name(exc: BaseException) -> str:
    """The name of ``type(exc)`` when it is an ASCII identifier of at most 64
    characters, else ``"Exception"``. A class chooses its own name: a metaclass
    can make ``__name__`` raise, and the name can hold a newline and a sentence.

    What is read: the name stored in the type itself, taken straight from
    ``type.__dict__``, and returned as an exact ``str``, never a subclass with
    methods of its own. What is never run: the class's own code (its metaclass'
    ``__name__``, ``__getattribute__``, ``__str__``), because ``type(exc)`` reads
    the object's real type and the stored name is read without asking the class.
    What a class can still choose is any name that passes: it may read like a
    message, but it is one token."""
    try:
        name = _CLASS_NAME_OF_TYPE.__get__(type(exc))
        if isinstance(name, str) and _CLASS_NAME.fullmatch(name):
            return str.__str__(name)
    except Exception:
        return _UNREADABLE_CLASS_NAME
    return _UNREADABLE_CLASS_NAME


def _listable_names(names: Iterable[str]) -> tuple[tuple[str, ...], int]:
    """The names that may be printed (``ENTITY_ID_PATTERN`` in full, at most
    ``ENTITY_ID_MAX_LENGTH`` characters), at most ``MAX_KNOWN_LISTED`` of them,
    and how many more were left out."""
    listable = tuple(
        name
        for name in names
        if len(name) <= ENTITY_ID_MAX_LENGTH and _LISTABLE_NAME.fullmatch(name)
    )
    return listable[:MAX_KNOWN_LISTED], max(0, len(listable) - MAX_KNOWN_LISTED)


class EntryPointRefused(Exception):
    """Why an entry point was not loaded. ``distribution`` is the name of the
    distribution that published it (for ``OTHER_DISTRIBUTION``), ``known`` the
    sorted names the trusted distribution published in the group (for
    ``NOT_PUBLISHED``), reduced to the names the registry would accept as IDs
    and capped, with ``known_more`` the count of those left out. For
    ``UNLOCATABLE`` and ``FAILED_TO_IMPORT`` the cause is a ``LoadFailure`` that
    names the class of the error (``safe_class_name``) and holds nothing of it:
    the refusal is raised outside the ``except`` block that caught the error, so
    the error is neither its cause nor its context, and a traceback in a log
    cannot quote the failing module's message, its frames or its source lines.
    Nothing here quotes the entry point's value or an import error's message."""

    def __init__(
        self,
        reason: Refusal,
        *,
        distribution: str | None = None,
        known: tuple[str, ...] = (),
    ) -> None:
        super().__init__(reason.value)
        self.reason = reason
        self.distribution = distribution
        self.known, self.known_more = _listable_names(known)


_CANNOT_LOAD = "the workload's {subject} cannot be loaded"
_NOT_FROM_THE_PACKAGE = (
    "the workload's {subject} does not come from the meridian package"
)
# The fixed words of each reason, with the caller's word for what it loads in
# place of ``{subject}``: the evaluations' words (T-80), shared so that the
# graphs' loader can say the same. Reasons an operator cannot act on differently
# share a sentence. Every reason has a text (checked when this module is
# imported), and no text holds anything a foreign package chose; ``fixed_text``
# adds only names of the shape the registry accepts for an ID, a few of them.
_TEXTS: dict[Refusal, str] = {
    Refusal.PUBLISHED_TWICE: "the workload's {subject} is published more than once",
    Refusal.NOT_PUBLISHED: "no {subject} is published for this workload",
    Refusal.UNLOCATABLE: _CANNOT_LOAD,
    Refusal.FAILED_TO_IMPORT: _CANNOT_LOAD,
    Refusal.OTHER_DISTRIBUTION: _NOT_FROM_THE_PACKAGE,
    Refusal.OUTSIDE_WORKLOADS: _NOT_FROM_THE_PACKAGE,
    Refusal.OUTSIDE_ROOT: _NOT_FROM_THE_PACKAGE,
    Refusal.MOVED_OUTSIDE_ROOT: _NOT_FROM_THE_PACKAGE,
}


def _require_a_text_for_every_reason(texts: dict[Refusal, str]) -> None:
    """Raise when a ``Refusal`` member has no text (or an empty one), naming the
    members, so a member added later fails when this module is imported, not at
    the first refusal it causes. The names are this module's own."""
    lacking = sorted(member.name for member in Refusal if not texts.get(member))
    if lacking or set(texts) != set(Refusal):
        raise AssertionError(
            "every refusal reason needs a fixed text: " + ", ".join(lacking)
        )


_require_a_text_for_every_reason(_TEXTS)


def reason_text(reason: Refusal, subject: str) -> str:
    """The fixed sentence for ``reason``; ``subject`` is what the caller loads
    ("evaluation", "graph"). A reason with no text is an error, never silence."""
    template = _TEXTS.get(reason)
    if template is None:
        raise AssertionError("a refusal reason has no fixed text")
    return template.format(subject=subject)


def fixed_text(refused: EntryPointRefused, subject: str) -> str:
    """What a caller may say about a refusal, in the shared fixed words. It
    quotes neither the distribution nor the entry point's value nor an error's
    message; ``NOT_PUBLISHED`` adds the names the trusted distribution did
    publish that the registry would accept as IDs (or "none"), at most
    ``MAX_KNOWN_LISTED`` of them and the rest as a count. What a directory that
    calls itself the trusted distribution can still choose there: up to ten
    names of the registry's ID shape, 64 characters each, and the count (which
    carries no text of its own)."""
    text = reason_text(refused.reason, subject)
    if refused.reason is Refusal.NOT_PUBLISHED:
        known = ", ".join(refused.known) if refused.known else "none"
        more = f" and {refused.known_more} more" if refused.known_more else ""
        return f"{text}; known: {known}{more}"
    return text


def _normalised(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _distribution_of(entry: EntryPoint) -> str | None:
    return entry.dist.name if entry.dist is not None else None


def _from_trusted_distribution(entry: EntryPoint) -> bool:
    dist = _distribution_of(entry)
    return dist is not None and _normalised(dist) == TRUSTED_DISTRIBUTION


def _module_of(entry: EntryPoint) -> str:
    return entry.value.partition(":")[0].strip()


def _inside(root: Path, file: str | None) -> bool:
    """Whether ``file`` lies under ``root``, both with their links resolved: a
    root that is a link, or lies behind one, admits the modules inside it."""
    return file is not None and Path(file).resolve().is_relative_to(root.resolve())


def _located_in(root: Path, module: str) -> bool:
    """Whether the module's spec puts it under ``root``, read before the module
    runs. ``find_spec`` imports the module's parent packages (under
    ``meridian.workloads``, by the prefix check), not the module itself. When it
    raises (a parent that does not import, a name that is no module name), the
    refusal is ``UNLOCATABLE`` and its cause names the class of what
    ``find_spec`` raised, and nothing else of it."""
    try:
        spec = importlib.util.find_spec(module)
    except Exception as exc:
        failure = LoadFailure(safe_class_name(exc))
    else:
        if spec is None or not spec.has_location:
            return False
        return _inside(root, spec.origin)
    # Outside the ``except`` block, so the error is not the refusal's context.
    raise EntryPointRefused(Refusal.UNLOCATABLE) from failure


def _loaded_in(root: Path, module: str) -> bool:
    """Whether the module that loaded is a file under ``root``."""
    return _inside(root, getattr(sys.modules.get(module), "__file__", None))


def load_trusted_entry_point(
    group: str,
    name: str,
    *,
    entry_points: EntryPoints = metadata.entry_points,
    trusted_root: Path = TRUSTED_ROOT,
    value_prefix: str = TRUSTED_VALUE_PREFIX,
) -> object:
    """The object the ``meridian`` distribution published under ``name`` in
    ``group``; raise ``EntryPointRefused`` when any check fails. Both callers
    pass all three options; the defaults are for the tests' own loaders."""
    published = list(entry_points(group=group))
    named = [e for e in published if e.name == name]
    if len(named) > 1:
        raise EntryPointRefused(Refusal.PUBLISHED_TWICE)
    if not named:
        known = sorted({e.name for e in published if _from_trusted_distribution(e)})
        raise EntryPointRefused(Refusal.NOT_PUBLISHED, known=tuple(known))
    (entry,) = named
    if not _from_trusted_distribution(entry):
        raise EntryPointRefused(
            Refusal.OTHER_DISTRIBUTION, distribution=_distribution_of(entry)
        )
    if not entry.value.startswith(value_prefix):
        raise EntryPointRefused(Refusal.OUTSIDE_WORKLOADS)
    module = _module_of(entry)
    if not _located_in(trusted_root, module):
        raise EntryPointRefused(Refusal.OUTSIDE_ROOT)
    try:
        loaded = entry.load()
    except Exception as exc:
        failure = LoadFailure(safe_class_name(exc))
    else:
        if not _loaded_in(trusted_root, module):
            raise EntryPointRefused(Refusal.MOVED_OUTSIDE_ROOT)
        return loaded
    # Outside the ``except`` block, so the error is not the refusal's context.
    raise EntryPointRefused(Refusal.FAILED_TO_IMPORT) from failure
