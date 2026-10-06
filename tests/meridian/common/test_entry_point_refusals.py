"""What a refused entry point says, and what it leaves for a log (S076, T-40, T-80).

The shared loader words nothing of a caller's own; it offers the fixed words
both groups' wrappers use, and it leaves no foreign error behind: the cause of a
refusal is the error's class and nothing else.
"""

import importlib
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


def entry(loads: Callable[[], object]) -> SimpleNamespace:
    return SimpleNamespace(
        name="claims-triage",
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
