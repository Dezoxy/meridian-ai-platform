"""The make targets of the Google Cloud scaffold and its policy scan (S078).

The module under ``infra/terraform/gcp`` is checked and never planned or
applied, so it has two targets that run a check (``gcp-validate`` and
``gcp-scan``) and, by the design's first decision, no target that plans, applies
or removes it.

``make gcp-scan`` is the twin of ``make aws-scan``: Trivy's configuration scan
from the same pinned image (``TRIVY_IMAGE``, no second pin), network off, fails
on a HIGH or CRITICAL finding that ``infra/terraform/gcp/.trivyignore`` does not
list. A test holds the two recipes equal but for the mounted directory and the
skipped file names, so that a flag added to one is not forgotten in the other.
The shared helpers (the reason above an entry, the resource count, the inline
ignore and the module block) are the AWS file's own; what is Google's here is
the check ID's shape and the module's files.
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
MODULE = REPO_ROOT / "infra" / "terraform" / "gcp"
TRIVYIGNORE = MODULE / ".trivyignore"
TARGETS = ("gcp-validate", "gcp-scan")
# The names a plan, an apply and a removal would have had, by the pattern of the
# AWS targets. The design decided against all three: nothing here is ever
# applied, and a command that does it is the most dangerous part of a module.
NEVER_BUILT = ("gcp-plan", "gcp-apply", "gcp-destroy")
# A Google Cloud check of the pinned image has the ID shape GCP-NNNN (the three
# the proof planted in a scratch copy were GCP-0017, GCP-0053 and GCP-0027). An
# entry is a check ID and nothing else: Trivy takes `GCP-0017 exp:2099-12-31` as
# an expiry, `GCP-0017 file.tf` as the ID alone (for every file), and the older
# `AVD-GCP-0017` spelling as the same check.
ENTRY = re.compile(r"GCP-\d{4}")
# What a by-hand run of Terraform in the module's directory would leave: a
# state file (the local backend has no path, so Terraform's default names) and
# a plan saved with `-out=gcp.tfplan`, the name the AWS module's script gives
# its own. No command of the repository makes any of them.
GCP_SKIPPED_FILES = {"gcp.tfplan", "terraform.tfstate", "terraform.tfstate.backup"}


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
    file names, and the word that names the module (the directory, the target
    in the refusal's message)."""
    text = re.sub(r"--skip-files \S+", "--skip-files SKIPPED", text.strip())
    return re.sub(r"\b(aws|gcp)\b", "MODULE", text)


def malformed_entries(text: str) -> list[str]:
    """The entries that are not exactly a check ID of the GCP- and four digits
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
def test_each_gcp_target_is_phony_and_has_a_help_line(target: str) -> None:
    assert target in phony_names()
    assert re.search(rf"^{target}:", MAKEFILE, re.MULTILINE)
    assert help_line(target)


def test_the_validate_target_runs_the_script_with_the_module_name_alone() -> None:
    assert recipe("gcp-validate").strip() == "infra/terraform/aws.sh validate gcp"


def test_the_free_targets_say_they_change_nothing_in_google_cloud() -> None:
    for target in TARGETS:
        assert "changes nothing in Google Cloud" in help_line(target)


def test_the_scan_target_says_it_is_offline_and_needs_no_project_or_credential() -> (
    None
):
    line = help_line("gcp-scan")

    assert "offline" in line
    assert "needs no project and no credential" in line


def test_no_target_plans_applies_or_removes_the_google_cloud_module() -> None:
    declared = set(re.findall(r"^(gcp-[\w-]+):", MAKEFILE, re.MULTILINE))

    assert declared.isdisjoint(NEVER_BUILT)
    assert not set(NEVER_BUILT) & set(phony_names())


# ── the scan's image and recipe ──────────────────────────────────────────────


def test_the_scan_uses_the_one_pinned_image_and_no_second_pin() -> None:
    assert "$(TRIVY_IMAGE)" in recipe("gcp-scan")
    assert len(re.findall(r"^\w*TRIVY\w*\s*:=", MAKEFILE, re.MULTILINE)) == 1
    assert "aquasecurity/trivy" not in recipe("gcp-scan")


def test_the_scan_runs_with_no_network_and_no_rights() -> None:
    text = recipe("gcp-scan")

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
    assert 'source="$(CURDIR)/infra/terraform/gcp"' in text
    assert "aws" not in text


def test_the_scan_stays_offline_and_fails_on_high_and_critical() -> None:
    text = recipe("gcp-scan")

    assert " config " in text
    assert "--severity HIGH,CRITICAL" in text
    assert "--exit-code 1" in text
    # Without --skip-check-update Trivy tries to download its checks first and,
    # with the network off, falls back to the embedded ones after a wait.
    for flag in ("--skip-check-update", "--skip-version-check", "--disable-telemetry"):
        assert flag in text


def test_the_scan_reads_the_ignore_file_that_sits_in_the_module() -> None:
    text = recipe("gcp-scan")

    assert "-w /work" in text  # Trivy's default .trivyignore is the working dir's
    assert "--ignorefile" not in text  # which fails when the file is missing


def test_the_scan_leaves_out_the_terraform_directory_and_a_hand_run_s_files() -> None:
    text = recipe("gcp-scan")

    assert "--skip-dirs .terraform " in text
    (skipped,) = re.findall(r"--skip-files (\S+)", text)
    assert set(skipped.split(",")) == GCP_SKIPPED_FILES


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
        [make, "-C", str(empty), "-f", str(REPO_ROOT / "Makefile"), "gcp-scan"],
        capture_output=True,
        text=True,
        check=False,
        env={"PATH": f"{bin_dir}{os.pathsep}/usr/bin{os.pathsep}/bin"},
    )

    assert result.returncode != 0
    assert "gcp-scan: no .tf file in infra/terraform/gcp" in result.stderr
    assert not called.exists()


# ── the twin of aws-scan ─────────────────────────────────────────────────────


def test_the_two_scan_recipes_are_equal_but_for_directory_and_skipped_names() -> None:
    gcp = without_the_module(recipe("gcp-scan"))
    aws = without_the_module(recipe("aws-scan"))

    assert gcp == aws
    # The comparison is not vacuous: each recipe names its own directory.
    assert "infra/terraform/gcp" in recipe("gcp-scan")
    assert "infra/terraform/aws" in recipe("aws-scan")
    assert "--skip-files" in gcp


def test_a_flag_in_one_recipe_only_makes_the_recipes_differ() -> None:
    aws = recipe("aws-scan")
    gcp = recipe("gcp-scan")
    for flag in ("--network none", "--cap-drop ALL", "--exit-code 1"):
        assert flag in gcp
        weakened = gcp.replace(flag, "")
        assert without_the_module(weakened) != without_the_module(aws), flag
    extra = gcp.replace(" config ", " config --scanners misconfig ")
    assert without_the_module(extra) != without_the_module(aws)


# ── the ignore file ──────────────────────────────────────────────────────────


def test_the_ignore_file_is_a_header_and_accepts_no_finding() -> None:
    """The module's own HIGH and CRITICAL findings on the pinned image were none,
    so nothing is accepted. A first entry needs a change of this list, with its
    reason on the line above, in the same diff as the entry: a new ID with a
    reason of five words would otherwise pass the shape and the reason tests."""
    text = TRIVYIGNORE.read_text(encoding="utf-8")

    assert text.startswith("#")
    assert entries(text) == []


def test_the_committed_ignore_file_would_hold_a_reason_above_every_entry() -> None:
    text = TRIVYIGNORE.read_text(encoding="utf-8")

    assert entries_without_a_reason(text) == []
    assert malformed_entries(text) == []


def test_an_entry_with_its_reason_directly_above_passes() -> None:
    text = "# header\n\n# Open range: a test is never applied.\nGCP-0017\n"

    assert entries_without_a_reason(text) == []


def test_an_entry_with_no_comment_above_is_found() -> None:
    assert entries_without_a_reason("# header\n\nGCP-0017\n") == ["GCP-0017"]


def test_a_comment_across_a_blank_line_is_not_a_reason() -> None:
    text = "# Open range: a test is never applied.\n\nGCP-0017\n"

    assert entries_without_a_reason(text) == ["GCP-0017"]


def test_an_entry_under_another_entry_needs_its_own_reason() -> None:
    text = "# Open range: a test is never applied.\nGCP-0017\nGCP-0053\n"

    assert entries_without_a_reason(text) == ["GCP-0053"]


def test_a_reason_is_a_handful_of_words_and_no_fewer() -> None:
    short = " ".join(["word"] * (REASON_WORDS - 1))
    enough = " ".join(["word"] * REASON_WORDS)

    assert entries_without_a_reason(f"# {short}\nGCP-0017\n") == ["GCP-0017"]
    assert entries_without_a_reason(f"# {enough}\nGCP-0017\n") == []


@pytest.mark.parametrize(
    "entry",
    [
        "GCP-0017 exp:2099-12-31",  # an expiry: the entry is dropped when it passes
        "GCP-0017 database.tf",  # trailing text: Trivy keeps the ID, for every file
        "GCP-0017 # a reason on the line itself",
        "AVD-GCP-0017",  # the older spelling of the same check
        "avd-gcp-0017",
        "gcp-0017",
        "GCP-17",
        "GCP-00170",
        "GCP-*",
        "*",
        "AWS-0107",  # the other module's shape
        "GCP-0017,GCP-0053",
    ],
)
def test_an_entry_that_is_not_exactly_a_check_id_is_found(entry: str) -> None:
    assert malformed_entries(f"# a reason of enough words here\n{entry}\n") == [entry]


def test_an_entry_that_is_exactly_a_check_id_is_not_found() -> None:
    assert malformed_entries("# a reason of enough words here\nGCP-0017\n") == []


def test_a_duplicated_entry_is_found() -> None:
    twice = "# one two three four five\nGCP-0017\n" * 2
    once = "# one two three four five\nGCP-0017\n"

    assert duplicated_entries(twice) == ["GCP-0017"]
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


def test_the_module_has_one_cluster_and_one_subnet_block() -> None:
    """An entry in .trivyignore applies to every resource of the directory, and
    the scan's reading of the module is of these two: a second cluster or subnet
    block would be judged by what was decided for the first, unseen."""
    text = module_text()

    reasons = (
        "infra/terraform/gcp scans one cluster and one subnet; an ignore entry "
        "applies to every resource of the directory. Decide again, in "
        ".trivyignore, before changing this number."
    )
    assert count_resources(text, "google_container_cluster") == 1, reasons
    assert count_resources(text, "google_compute_subnetwork") == 1, reasons


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('resource "google_compute_subnetwork" "a" {}\n', 1),
        ("resource google_compute_subnetwork a {}\n", 1),
        ('resource google_compute_subnetwork "a" {}\n', 1),
        (
            'resource "google_compute_subnetwork" "a" {}\n'
            "resource google_compute_subnetwork b {}\n",
            2,
        ),
        ('# resource "google_compute_subnetwork" "a" {}\n', 0),
    ],
)
def test_the_subnet_count_reads_quoted_and_bare_labels(
    text: str, expected: int
) -> None:
    assert count_resources(text, "google_compute_subnetwork") == expected
