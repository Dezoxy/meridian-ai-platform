"""The make targets of the Google Cloud scaffold (S078).

The module under ``infra/terraform/gcp`` is checked and never planned or
applied, so it has one target that runs its check (``gcp-validate``) and, by the
design's first decision, no target that plans, applies or removes it. The scan's
target and its tests join this file in the next contract.
"""

import re

from servicesupport import REPO_ROOT

MAKEFILE = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
TARGETS = ("gcp-validate",)
# The names a plan, an apply and a removal would have had, by the pattern of the
# AWS targets. The design decided against all three: nothing here is ever
# applied, and a command that does it is the most dangerous part of a module.
NEVER_BUILT = ("gcp-plan", "gcp-apply", "gcp-destroy")


def recipe(target: str) -> str:
    """The lines under ``target:`` up to the next blank line."""
    return MAKEFILE.split(f"\n{target}:\n", 1)[1].split("\n\n", 1)[0]


def help_line(target: str) -> str:
    (line,) = re.findall(rf"^## {target} +(.+)$", MAKEFILE, re.MULTILINE)
    return line


def phony_names() -> list[str]:
    return next(
        line for line in MAKEFILE.splitlines() if line.startswith(".PHONY:")
    ).split()


def test_the_validate_target_is_phony_and_has_a_help_line() -> None:
    assert "gcp-validate" in phony_names()
    assert re.search(r"^gcp-validate:", MAKEFILE, re.MULTILINE)
    assert help_line("gcp-validate")


def test_the_validate_target_runs_the_script_with_the_module_name_alone() -> None:
    assert recipe("gcp-validate").strip() == "infra/terraform/aws.sh validate gcp"


def test_the_validate_target_says_it_changes_nothing_in_google_cloud() -> None:
    assert "changes nothing in Google Cloud" in help_line("gcp-validate")


def test_no_target_plans_applies_or_removes_the_google_cloud_module() -> None:
    declared = set(re.findall(r"^(gcp-[\w-]+):", MAKEFILE, re.MULTILINE))

    assert declared.isdisjoint(NEVER_BUILT)
    assert not set(NEVER_BUILT) & set(phony_names())
