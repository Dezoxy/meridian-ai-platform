"""The make targets of the Azure platform module and its policy scan (S020).

The module under ``infra/terraform/azure`` is written, checked and, in this
change, never planned or applied, so it has two targets that run a check
(``azure-platform-validate`` and ``azure-platform-scan``) and no target that
plans, applies or removes it: those arrive with their wrapper and the guard's
rules in a later change.

``make azure-platform-scan`` is the twin of ``make aws-scan`` and ``make
gcp-scan``: Trivy's configuration scan from the same pinned image
(``TRIVY_IMAGE``, no second pin), network off, fails on a HIGH or CRITICAL
finding that ``infra/terraform/azure/.trivyignore`` does not list. A test holds
the recipes equal but for the mounted directory and the skipped file names, so
that a flag added to one is not forgotten in the other. The shared helpers (the
reason above an entry, the resource count, the inline ignore and the module
block) are the AWS file's own; what is Azure's here is the check ID's shape and
the module's files.
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from servicesupport import REPO_ROOT
from test_aws_scan import (
    REASON_WORDS,
    count_resources,
    entries,
    entries_without_a_reason,
    inline_ignores,
    module_blocks,
    scanner_files,
)

MAKEFILE = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
MODULE = REPO_ROOT / "infra" / "terraform" / "azure"
TRIVYIGNORE = MODULE / ".trivyignore"
TARGETS = ("azure-platform-validate", "azure-platform-scan")
# The names a plan, an apply and a removal would have had, by the pattern of the
# AWS targets. They arrive with the wrapper that gives them the protections
# aws.sh has and foundation.sh lacks (a clean environment, the lock read-only, a
# plan record bound to its commit), and with the guard's and the settings' rules
# for the new names, written BEFORE the targets exist and security-reviewed
# (design D15). A target of these names without that is a command that applies
# or removes an Azure environment and that neither layer asks about.
NEVER_BUILT = ("azure-platform-plan", "azure-platform-apply", "azure-platform-destroy")
# An Azure check of the pinned image has the ID shape AZU-NNNN (the eight findings
# of the module's first scan were AZU-0017 to AZU-0067). An entry is a check ID
# and nothing else: Trivy takes `AZU-0017 exp:2099-12-31` as an expiry,
# `AZU-0017 file.tf` as the ID alone (for every file), and the older
# `AVD-AZU-0017` spelling as the same check.
ENTRY = re.compile(r"AZU-\d{4}")
# What a by-hand run of Terraform in the module's directory would leave: a state
# file (the module's backend is remote, but a run with `-backend=false` and a
# local `-state` leaves Terraform's default names) and a plan saved with
# `-out=azure.tfplan`, the name the AWS script gives a module's plan. No command
# of the repository makes any of them.
AZURE_SKIPPED_FILES = {"azure.tfplan", "terraform.tfstate", "terraform.tfstate.backup"}


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


def without_the_module(text: str) -> str:
    """A scan recipe with what is the module's own made the same: the skipped
    file names, and the words that name the module (the directory, the target
    in the refusal's message)."""
    text = re.sub(r"--skip-files \S+", "--skip-files SKIPPED", text.strip())
    text = text.replace("azure-platform", "MODULE")
    return re.sub(r"\b(aws|azure|gcp)\b", "MODULE", text)


def malformed_entries(text: str) -> list[str]:
    """The entries that are not exactly a check ID of the AZU- and four digits
    shape: nothing after it (no expiry, no file name), not the older AVD-
    spelling, no wildcard."""
    return [entry for entry in entries(text) if not ENTRY.fullmatch(entry)]


def duplicated_entries(text: str) -> list[str]:
    found = entries(text)
    return sorted({entry for entry in found if found.count(entry) > 1})


def module_text() -> str:
    return "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(MODULE.glob("*.tf"))
    )


# ── the targets ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("target", TARGETS)
def test_each_azure_platform_target_is_phony_and_has_a_help_line(target: str) -> None:
    assert phony_names().count(target) == 1
    assert re.search(rf"^{target}:", MAKEFILE, re.MULTILINE)
    assert help_line(target)


def test_the_validate_target_runs_the_script_with_the_module_name_alone() -> None:
    assert (
        recipe("azure-platform-validate").strip()
        == "infra/terraform/aws.sh validate azure"
    )


def test_the_two_targets_say_they_change_nothing_in_azure() -> None:
    for target in TARGETS:
        assert "changes nothing in Azure" in help_line(target)
        assert "needs no Azure sign-in" in help_line(target)


def test_the_scan_target_says_it_is_offline_and_needs_no_subscription() -> None:
    line = help_line("azure-platform-scan")

    assert "offline" in line
    assert "needs no Azure sign-in and no subscription" in line
    assert "(needs Docker)" in line


def test_only_the_two_checking_targets_carry_the_modules_name() -> None:
    """The comment on NEVER_BUILT says why: a plan, an apply and a removal come
    with their wrapper and the guard's rules, and no target of the module's name
    may exist before them, whatever it is called."""
    declared = set(re.findall(r"^(azure-platform[\w-]*):", MAKEFILE, re.MULTILINE))
    named_in_phony = {
        name for name in phony_names() if name.startswith("azure-platform")
    }

    assert declared == set(TARGETS)
    assert named_in_phony == set(TARGETS)


def test_no_target_plans_applies_or_removes_the_azure_platform_module() -> None:
    declared = set(re.findall(r"^([\w-]+):", MAKEFILE, re.MULTILINE))

    assert declared.isdisjoint(NEVER_BUILT)
    assert not set(NEVER_BUILT) & set(phony_names())


def test_no_target_runs_the_script_with_a_word_other_than_validate_on_azure() -> None:
    # The script refuses `plan azure`, `apply azure` and `destroy azure` with its
    # usage line, but a recipe that tried would be a target of the kind the test
    # above forbids under another name.
    assert not re.search(r"aws\.sh\s+(plan|apply|destroy)\s+azure", MAKEFILE), (
        "a make recipe calls the wrapper's plan, apply or removal on the Azure module"
    )


def test_the_azure_foundation_targets_are_not_this_module_s() -> None:
    # The foundation's targets keep their names and their script; the module's
    # have their own, so that nobody reads `azure-apply` as the module's.
    assert recipe("azure-plan").strip().startswith("infra/terraform/foundation.sh")
    assert recipe("azure-apply").strip().startswith("infra/terraform/foundation.sh")


# ── the scan's image and recipe ──────────────────────────────────────────────


def test_the_scan_uses_the_one_pinned_image_and_no_second_pin() -> None:
    assert "$(TRIVY_IMAGE)" in recipe("azure-platform-scan")
    assert len(re.findall(r"^\w*TRIVY\w*\s*:=", MAKEFILE, re.MULTILINE)) == 1
    assert "aquasecurity/trivy" not in recipe("azure-platform-scan")


def test_the_scan_runs_with_no_network_and_no_rights() -> None:
    text = recipe("azure-platform-scan")

    for flag in (
        "--network none",
        "--read-only",
        "--cap-drop ALL",
        "--security-opt no-new-privileges",
    ):
        assert flag in text
    # The module is mounted read-only; --mount refuses a path that is not there
    # where -v would make an empty directory that scans clean.
    assert re.search(r"--mount type=bind,[^ ]*readonly", text)
    assert 'source="$(CURDIR)/infra/terraform/azure"' in text
    assert "aws" not in text
    assert "gcp" not in text


def test_the_scan_stays_offline_and_fails_on_high_and_critical() -> None:
    text = recipe("azure-platform-scan")

    assert " config " in text
    assert "--severity HIGH,CRITICAL" in text
    assert "--exit-code 1" in text
    # Without --skip-check-update Trivy tries to download its checks first and,
    # with the network off, falls back to the embedded ones after a wait.
    for flag in ("--skip-check-update", "--skip-version-check", "--disable-telemetry"):
        assert flag in text


def test_the_scan_reads_the_ignore_file_that_sits_in_the_module() -> None:
    text = recipe("azure-platform-scan")

    assert "-w /work" in text  # Trivy's default .trivyignore is the working dir's
    assert "--ignorefile" not in text  # which fails when the file is missing


def test_the_scan_leaves_out_the_terraform_directory_and_a_hand_run_s_files() -> None:
    text = recipe("azure-platform-scan")

    assert "--skip-dirs .terraform " in text
    (skipped,) = re.findall(r"--skip-files (\S+)", text)
    assert set(skipped.split(",")) == AZURE_SKIPPED_FILES


def test_the_scan_refuses_a_directory_with_no_terraform_in_it_before_docker_runs(
    tmp_path: Path,
) -> None:
    # A scan of nothing passes: the recipe says so when there is nothing to scan.
    # The make runs in an empty directory, with a docker that records its call.
    make = shutil.which("make")
    if make is None:
        pytest.skip("make is not installed")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    called = tmp_path / "docker-was-called"
    stub = bin_dir / "docker"
    stub.write_text(f"#!/bin/sh\ntouch {called}\n", encoding="utf-8")
    stub.chmod(0o755)
    empty = tmp_path / "empty"
    empty.mkdir()

    result = subprocess.run(
        [
            make,
            "-C",
            str(empty),
            "-f",
            str(REPO_ROOT / "Makefile"),
            "azure-platform-scan",
        ],
        capture_output=True,
        text=True,
        check=False,
        env={"PATH": f"{bin_dir}{os.pathsep}/usr/bin{os.pathsep}/bin"},
    )

    assert result.returncode != 0
    assert "azure-platform-scan: no .tf file in infra/terraform/azure" in result.stderr
    assert not called.exists()


# ── the twin of aws-scan and gcp-scan ────────────────────────────────────────


@pytest.mark.parametrize("twin", ["aws-scan", "gcp-scan"])
def test_the_scan_recipe_is_equal_to_its_twins_but_for_directory_and_skipped_names(
    twin: str,
) -> None:
    azure = without_the_module(recipe("azure-platform-scan"))
    other = without_the_module(recipe(twin))

    assert azure == other
    # The comparison is not vacuous: each recipe names its own directory.
    assert "infra/terraform/azure" in recipe("azure-platform-scan")
    assert f"infra/terraform/{twin.removesuffix('-scan')}" in recipe(twin)
    assert "--skip-files" in azure


def test_a_flag_in_one_recipe_only_makes_the_recipes_differ() -> None:
    aws = recipe("aws-scan")
    azure = recipe("azure-platform-scan")
    for flag in ("--network none", "--cap-drop ALL", "--exit-code 1"):
        assert flag in azure
        weakened = azure.replace(flag, "")
        assert without_the_module(weakened) != without_the_module(aws), flag
    extra = azure.replace(" config ", " config --scanners misconfig ")
    assert without_the_module(extra) != without_the_module(aws)


# ── the ignore file ──────────────────────────────────────────────────────────


def test_the_ignore_file_is_a_header_and_accepts_no_finding() -> None:
    """The module's own HIGH and CRITICAL findings on the pinned image were none
    (its eight MEDIUM and LOW findings are not what the scan gates on), so nothing
    is accepted. A first entry needs a change of this list, with its reason on
    the line above, in the same diff as the entry: a new ID with a reason of five
    words would otherwise pass the shape and the reason tests."""
    text = TRIVYIGNORE.read_text(encoding="utf-8")

    assert text.startswith("#")
    assert entries(text) == []


def test_the_committed_ignore_file_would_hold_a_reason_above_every_entry() -> None:
    text = TRIVYIGNORE.read_text(encoding="utf-8")

    assert entries_without_a_reason(text) == []
    assert malformed_entries(text) == []


def test_an_entry_with_its_reason_directly_above_passes() -> None:
    text = "# header\n\n# Open range: a test is never applied.\nAZU-0017\n"

    assert entries_without_a_reason(text) == []


def test_an_entry_with_no_comment_above_is_found() -> None:
    assert entries_without_a_reason("# header\n\nAZU-0017\n") == ["AZU-0017"]


def test_a_comment_across_a_blank_line_is_not_a_reason() -> None:
    text = "# Open range: a test is never applied.\n\nAZU-0017\n"

    assert entries_without_a_reason(text) == ["AZU-0017"]


def test_an_entry_under_another_entry_needs_its_own_reason() -> None:
    text = "# Open range: a test is never applied.\nAZU-0017\nAZU-0019\n"

    assert entries_without_a_reason(text) == ["AZU-0019"]


def test_a_reason_is_a_handful_of_words_and_no_fewer() -> None:
    short = " ".join(["word"] * (REASON_WORDS - 1))
    enough = " ".join(["word"] * REASON_WORDS)

    assert entries_without_a_reason(f"# {short}\nAZU-0017\n") == ["AZU-0017"]
    assert entries_without_a_reason(f"# {enough}\nAZU-0017\n") == []


@pytest.mark.parametrize(
    "entry",
    [
        "AZU-0017 exp:2099-12-31",  # an expiry: the entry is dropped when it passes
        "AZU-0017 database.tf",  # trailing text: Trivy keeps the ID, for every file
        "AZU-0017 # a reason on the line itself",
        "AVD-AZU-0017",  # the older spelling of the same check
        "avd-azu-0017",
        "azu-0017",
        "AZU-17",
        "AZU-00170",
        "AZU-*",
        "*",
        "AWS-0107",  # another module's shape
        "GCP-0017",
        "AZU-0017,AZU-0019",
    ],
)
def test_an_entry_that_is_not_exactly_a_check_id_is_found(entry: str) -> None:
    assert malformed_entries(f"# a reason of enough words here\n{entry}\n") == [entry]


def test_an_entry_that_is_exactly_a_check_id_is_not_found() -> None:
    assert malformed_entries("# a reason of enough words here\nAZU-0017\n") == []


def test_a_duplicated_entry_is_found() -> None:
    twice = "# one two three four five\nAZU-0017\n" * 2
    once = "# one two three four five\nAZU-0017\n"

    assert duplicated_entries(twice) == ["AZU-0017"]
    assert duplicated_entries(once) == []
    assert duplicated_entries(TRIVYIGNORE.read_text(encoding="utf-8")) == []


# ── what could silence the scan some other way ──────────────────────────────


def test_no_terraform_file_of_the_module_carries_an_inline_ignore_comment() -> None:
    found = {
        path.name: inline_ignores(path.read_text(encoding="utf-8"))
        for path in sorted(MODULE.glob("*.tf"))
    }

    assert {name: lines for name, lines in found.items() if lines} == {}


def test_the_module_directory_holds_no_scanner_configuration_or_yaml_ignore_file() -> (
    None
):
    assert scanner_files(MODULE) == []


def test_the_module_calls_no_module_that_could_hide_a_cluster_or_a_subnet() -> None:
    found = {
        path.name: module_blocks(path.read_text(encoding="utf-8"))
        for path in sorted(MODULE.glob("*.tf"))
    }

    assert {name: lines for name, lines in found.items() if lines} == {}
    # A file the glob above does not read would hide a resource just as well.
    assert sorted(path.name for path in MODULE.glob("*.tf.json")) == []


@pytest.mark.parametrize(
    ("resource_type", "expected"),
    [
        ("azurerm_kubernetes_cluster", 1),
        ("azurerm_container_registry", 1),
        ("azurerm_postgresql_flexible_server", 1),
        ("azurerm_subnet", 3),
    ],
)
def test_the_module_has_these_many_blocks_of_the_kinds_the_scan_judges(
    resource_type: str, expected: int
) -> None:
    """An entry in .trivyignore applies to every resource of the directory, and
    the scan's reading of the module is of these kinds: a second cluster,
    registry or database server, or a fourth subnet, would be judged by what was
    decided for the first, unseen."""
    reason = (
        f"infra/terraform/azure has {expected} {resource_type} block(s); an ignore "
        "entry applies to every resource of the directory. Decide again, in "
        ".trivyignore, before changing this number."
    )

    assert count_resources(module_text(), resource_type) == expected, reason


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('resource "azurerm_subnet" "a" {}\n', 1),
        ("resource azurerm_subnet a {}\n", 1),
        ('resource azurerm_subnet "a" {}\n', 1),
        (
            'resource "azurerm_subnet" "a" {}\nresource azurerm_subnet b {}\n',
            2,
        ),
        ('# resource "azurerm_subnet" "a" {}\n', 0),
    ],
)
def test_the_subnet_count_reads_quoted_and_bare_labels(
    text: str, expected: int
) -> None:
    assert count_resources(text, "azurerm_subnet") == expected
