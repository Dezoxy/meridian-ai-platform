"""What a refused entry point says, and what it leaves for a log (S076, T-40, T-80).

The shared loader words nothing of a caller's own; it offers the fixed words
both groups' wrappers use, and it leaves no foreign error behind: the cause of a
refusal is the error's class and nothing else.
"""

import importlib
import importlib.util
import inspect
import traceback
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

from meridian.platform.cli import app
from meridian.platform.cli import evaluation as cli
from meridian.platform.common import entry_points as shared
from meridian.platform.common.entry_points import (
    EntryPointRefused,
    Refusal,
    load_trusted_entry_point,
)
from meridian.platform.evaluation import workload

CANARY = "canary-in-the-message"
REAL_VALUE = "meridian.workloads.claims_triage.evaluation_http:EVALUATION"
GROUP = "meridian.test-group"

runner = CliRunner()


def entry(
    loads: Callable[[], object] = lambda: None, *, name: str = "claims-triage"
) -> SimpleNamespace:
    return SimpleNamespace(
        name=name,
        value=REAL_VALUE,
        dist=SimpleNamespace(name="meridian"),
        load=loads,
    )


def refusal_of(entry_point: SimpleNamespace, **options: Any) -> EntryPointRefused:
    with pytest.raises(EntryPointRefused) as refused:
        load_trusted_entry_point(
            GROUP,
            "claims-triage",
            entry_points=lambda *, group: [entry_point],
            **options,
        )
    return refused.value


def refusal_of_named(entries: list[SimpleNamespace]) -> EntryPointRefused:
    with pytest.raises(EntryPointRefused) as refused:
        load_trusted_entry_point(
            GROUP, "missing", entry_points=lambda *, group: entries
        )
    return refused.value


def broken_import() -> object:
    raise ImportError("canary-in-the-message /opt/somewhere/module.py")


def test_every_reason_of_a_refusal_has_a_fixed_text() -> None:
    for reason in Refusal:
        text = shared.fixed_text(EntryPointRefused(reason), "evaluation")

        assert text
        assert "{" not in text


def test_a_reason_without_a_text_is_an_error_not_silence() -> None:
    unknown = SimpleNamespace(reason="a reason added later", known=())

    with pytest.raises(AssertionError):
        shared.fixed_text(unknown, "evaluation")


def test_the_evaluations_words_are_the_ones_callers_saw_before() -> None:
    assert workload.NOT_PUBLISHED == "no evaluation is published for this workload"
    assert (
        workload.UNTRUSTED
        == "the workload's evaluation does not come from the meridian package"
    )
    assert workload.UNLOADABLE == "the workload's evaluation cannot be loaded"
    assert (
        workload.NOT_AN_EVALUATION
        == "the workload's evaluation is not a WorkloadEvaluation"
    )
    assert (
        workload.PUBLISHED_TWICE
        == "the workload's evaluation is published more than once"
    )


def test_the_subject_is_the_callers_word_in_the_same_sentence() -> None:
    text = shared.fixed_text(EntryPointRefused(Refusal.PUBLISHED_TWICE), "graph")

    assert text == "the workload's graph is published more than once"


def test_a_name_that_is_not_published_lists_the_known_ones_or_says_none() -> None:
    names = ("a-workload", "b-workload")
    some = EntryPointRefused(Refusal.NOT_PUBLISHED, known=names)
    none = EntryPointRefused(Refusal.NOT_PUBLISHED)

    listed = shared.fixed_text(some, "evaluation")
    assert listed.endswith("; known: a-workload, b-workload")
    assert shared.fixed_text(none, "evaluation").endswith("; known: none")


def test_the_distribution_a_refusal_names_is_never_in_its_text() -> None:
    refused = EntryPointRefused(Refusal.OTHER_DISTRIBUTION, distribution=CANARY)

    assert CANARY not in shared.fixed_text(refused, "evaluation")
    assert CANARY not in str(refused)


def test_an_import_error_leaves_no_message_in_the_refusal_or_its_log_form() -> None:
    refused = refusal_of(entry(broken_import))

    logged = "".join(traceback.format_exception(refused))
    assert refused.reason is Refusal.FAILED_TO_IMPORT
    assert CANARY not in str(refused)
    assert CANARY not in repr(refused.__cause__)
    assert CANARY not in logged
    assert "/opt/somewhere" not in logged
    assert "ImportError" in logged


def test_an_import_error_is_not_kept_as_the_context_of_the_refusal() -> None:
    refused = refusal_of(entry(broken_import))

    assert refused.__context__ is None
    assert refused.__suppress_context__


def test_a_parent_package_that_fails_leaves_no_message_in_the_refusal_or_its_log_form(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = tmp_path / "canary_parent_plugin"
    package.mkdir()
    (package / "__init__.py").write_text(
        'raise ImportError("canary-in-the-message")\n', encoding="utf-8"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    calls: list[int] = []

    refused = refusal_of(
        SimpleNamespace(
            name="claims-triage",
            value="canary_parent_plugin.module:VALUE",
            dist=SimpleNamespace(name="meridian"),
            load=lambda: calls.append(1),
        ),
        value_prefix="canary_parent_plugin.",
    )

    logged = "".join(traceback.format_exception(refused))
    assert refused.reason is Refusal.UNLOCATABLE
    assert calls == []
    assert CANARY not in str(refused)
    assert CANARY not in repr(refused.__cause__)
    assert CANARY not in logged
    assert str(refused.__cause__) == "ImportError"


def test_the_command_prints_no_message_of_the_import_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        workload, "entry_points", lambda *, group: [entry(broken_import)]
    )

    result = runner.invoke(
        app,
        [
            "eval", "run", "--base-url", "http://stack.test",
            "--report", str(tmp_path / "report.json"),
        ],
    )  # fmt: skip

    assert result.exit_code == cli.EXIT_UNREADABLE, result.output
    assert workload.UNLOADABLE in result.stderr
    assert CANARY not in result.output
    assert "/opt/somewhere" not in result.output


def raise_from(cls: type[BaseException]) -> Callable[..., object]:
    def loads(*_args: object) -> object:
        raise cls("canary-in-the-message")

    return loads


def error_class_with_a_newline_in_its_name() -> type[BaseException]:
    cls = type("Boom", (Exception,), {})
    cls.__name__ = "Boom\nERROR planted=hunter2 /etc/passwd"
    return cls


def error_class_whose_name_raises() -> type[BaseException]:
    class RaisingName(type):
        @property
        def __name__(cls) -> str:  # type: ignore[override]
            raise ValueError("planted-message-from-name")

    return RaisingName("Boom", (Exception,), {})


# The class, and the name the cause carries: a name that is no identifier is
# the fixed word; a metaclass's property is never run, so the type's own name
# is read.
HOSTILE_CLASSES = [
    pytest.param(
        error_class_with_a_newline_in_its_name, "Exception", id="newline-in-the-name"
    ),
    pytest.param(error_class_whose_name_raises, "Boom", id="a-name-that-raises"),
]
HOSTILE_TEXT = ("hunter2", "/etc/passwd", "planted-message-from-name", "ERROR")


def assert_nothing_hostile_is_left(refused: EntryPointRefused, name: str) -> None:
    logged = "".join(traceback.format_exception(refused))
    for text in (str(refused), repr(refused.__cause__), logged):
        for hostile in HOSTILE_TEXT:
            assert hostile not in text
    assert str(refused.__cause__) == name
    assert refused.__context__ is None
    assert refused.__suppress_context__


@pytest.mark.parametrize(("make_class", "name"), HOSTILE_CLASSES)
def test_a_class_name_the_loaded_code_chose_is_not_text_of_a_load_refusal(
    make_class: Callable[[], type[BaseException]], name: str
) -> None:
    refused = refusal_of(entry(raise_from(make_class())))

    assert refused.reason is Refusal.FAILED_TO_IMPORT
    assert_nothing_hostile_is_left(refused, name)


@pytest.mark.parametrize(("make_class", "name"), HOSTILE_CLASSES)
def test_a_class_name_the_finder_chose_is_not_text_of_a_locate_refusal(
    make_class: Callable[[], type[BaseException]],
    name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(importlib.util, "find_spec", raise_from(make_class()))
    calls: list[int] = []

    refused = refusal_of(entry(lambda: calls.append(1)))

    assert refused.reason is Refusal.UNLOCATABLE
    assert calls == []
    assert_nothing_hostile_is_left(refused, name)


def test_the_class_name_is_read_without_running_the_classs_own_code() -> None:
    hostile = error_class_whose_name_raises()

    with pytest.raises(ValueError, match="planted-message-from-name"):
        hostile.__name__  # noqa: B018 - the property is what the helper must not run

    assert shared.safe_class_name(ImportError("x")) == "ImportError"
    assert shared.safe_class_name(hostile("x")) == "Boom"


@pytest.mark.parametrize(
    "name",
    ["Boom\nsecond line", "two words", "", "1Boom", "Bööm", "x" * 65, "a.b", "a\n"],
)
def test_a_class_name_that_is_not_a_short_identifier_is_a_fixed_word(
    name: str,
) -> None:
    cls = type("Boom", (Exception,), {})
    cls.__name__ = name

    assert shared.safe_class_name(cls("x")) == "Exception"


def test_an_identifier_of_the_longest_length_is_kept() -> None:
    cls = type("Boom", (Exception,), {})
    cls.__name__ = "_" + "x" * 63

    assert shared.safe_class_name(cls("x")) == "_" + "x" * 63


def test_a_name_that_is_a_str_subclass_with_its_own_methods_is_a_plain_str() -> None:
    class Loud(str):
        def __str__(self) -> str:
            return "ERROR planted=hunter2"

    cls = type("Boom", (Exception,), {})
    cls.__name__ = Loud("Boom")

    name = shared.safe_class_name(cls("x"))

    assert name == "Boom"
    assert type(name) is str


NAMES = [f"workload-{number:02d}" for number in range(15)]


def test_only_names_the_registry_would_accept_as_ids_are_listed_as_known() -> None:
    hostile = [
        "INJECTED ERROR planted",
        "Upper",
        "x" * 65,
        "with\nnewline",
        "trailing-newline\n",
        "",
        "-leading-dash",
        "under_score",
    ]
    published = [entry(name=name) for name in (*hostile, "good-one", "x" * 64)]

    refused = refusal_of_named(published)

    assert refused.known == ("good-one", "x" * 64)
    text = shared.fixed_text(refused, "evaluation")
    assert text.endswith(f"; known: good-one, {'x' * 64}")
    assert "INJECTED" not in text


def test_the_names_listed_are_capped_and_the_rest_is_a_count() -> None:
    refused = refusal_of_named([entry(name=name) for name in NAMES])

    text = shared.fixed_text(refused, "evaluation")
    assert len(refused.known) == shared.MAX_KNOWN_LISTED
    assert refused.known == tuple(NAMES[: shared.MAX_KNOWN_LISTED])
    assert text.endswith(f" and {len(NAMES) - shared.MAX_KNOWN_LISTED} more")
    assert NAMES[-1] not in text


def test_names_exactly_at_the_cap_are_listed_with_no_count() -> None:
    names = NAMES[: shared.MAX_KNOWN_LISTED]

    refused = refusal_of_named([entry(name=name) for name in names])

    text = shared.fixed_text(refused, "evaluation")
    assert refused.known == tuple(names)
    assert "more" not in text


def test_a_refusal_built_with_hostile_names_lists_none_of_them() -> None:
    refused = EntryPointRefused(
        Refusal.NOT_PUBLISHED, known=("INJECTED ERROR planted", "fine")
    )

    assert shared.fixed_text(refused, "evaluation").endswith("; known: fine")


def test_a_directory_that_calls_itself_the_trusted_distribution_cannot_write_a_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dist = tmp_path / "MERIDIAN-9.dist-info"
    dist.mkdir()
    (dist / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: MERIDIAN\nVersion: 9\n", encoding="utf-8"
    )
    (dist / "entry_points.txt").write_text(
        f"[{GROUP}]\nINJECTED ERROR planted = evil.mod:X\nother = evil.mod:Y\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()

    with pytest.raises(EntryPointRefused) as refused:
        load_trusted_entry_point(GROUP, "claims-triage-x")

    text = shared.fixed_text(refused.value, "evaluation")
    assert "INJECTED" not in text
    assert "INJECTED" not in repr(refused.value.known)
    assert text.endswith("; known: other")


def test_a_reason_with_no_text_is_found_when_the_table_is_checked() -> None:
    texts = dict(shared._TEXTS)
    del texts[Refusal.OUTSIDE_ROOT]

    with pytest.raises(AssertionError):
        shared._require_a_text_for_every_reason(texts)

    shared._require_a_text_for_every_reason(shared._TEXTS)


def test_the_error_for_a_reason_with_no_text_names_the_member() -> None:
    texts = dict(shared._TEXTS)
    del texts[Refusal.OUTSIDE_ROOT]
    texts[Refusal.UNLOCATABLE] = ""

    with pytest.raises(AssertionError) as error:
        shared._require_a_text_for_every_reason(texts)

    assert str(error.value).endswith(": OUTSIDE_ROOT, UNLOCATABLE")


def run_the_module_source_with(old: str, new: str) -> None:
    """Run this module's own source in a fresh namespace with one line changed,
    as if it were imported: the module itself is not reloaded, because other
    tests hold references to its classes."""
    source = inspect.getsource(shared)
    changed = source.replace(old, new, 1)
    assert changed != source, "the line to change is not in the module's source"
    code = compile(changed, shared.__file__, "exec")

    exec(code, {"__name__": "entry_points_with_a_changed_table"})  # noqa: S102


@pytest.mark.parametrize(
    ("old", "new"),
    [
        pytest.param(
            "    Refusal.OUTSIDE_ROOT: _NOT_FROM_THE_PACKAGE,\n",
            "",
            id="a-member-loses-its-text",
        ),
        pytest.param(
            '    MOVED_OUTSIDE_ROOT = "outside the trusted root once loaded"\n',
            '    MOVED_OUTSIDE_ROOT = "outside the trusted root once loaded"\n'
            '    A_REASON_ADDED_LATER = "added later"\n',
            id="a-member-is-added-without-a-text",
        ),
    ],
)
def test_importing_the_module_fails_when_a_reason_has_no_text(
    old: str, new: str
) -> None:
    with pytest.raises(AssertionError, match="every refusal reason needs a fixed text"):
        run_the_module_source_with(old, new)


def test_importing_the_module_passes_when_nothing_is_changed() -> None:
    run_the_module_source_with(
        "MAX_KNOWN_LISTED = 10\n", "MAX_KNOWN_LISTED = 10  # unchanged in effect\n"
    )
