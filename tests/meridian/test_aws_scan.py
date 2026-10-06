"""The policy scan of the AWS module and its targets (S036).

``make aws-scan`` runs Trivy's configuration scan over ``infra/terraform/aws``
from an image pinned by digest, with the network off, and fails on a HIGH or
CRITICAL finding that ``infra/terraform/aws/.trivyignore`` does not list. A
finding the owner accepts for a test environment that lives an hour goes in
that file, with its reason on the line directly above; a test fails on an entry
without one. Here: the Makefile's pin and recipes, and that rule on the
committed file and on fixtures.
"""

import re

import pytest
from servicesupport import REPO_ROOT

MAKEFILE = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
TRIVYIGNORE = REPO_ROOT / "infra" / "terraform" / "aws" / ".trivyignore"
TARGETS = ("aws-validate", "aws-scan", "aws-plan", "aws-apply", "aws-destroy")


def recipe(target: str) -> str:
    """The lines under ``target:`` up to the next blank line."""
    return MAKEFILE.split(f"\n{target}:\n", 1)[1].split("\n\n", 1)[0]


def help_line(target: str) -> str:
    (line,) = re.findall(rf"^## {target} +(.+)$", MAKEFILE, re.MULTILINE)
    return line


def entries_without_a_reason(text: str) -> list[str]:
    """The entries of an ignore file whose line above is not a comment that
    says something. A blank line between a comment and an entry breaks the
    link, and so does another entry."""
    lines = text.splitlines()
    found = []
    for number, line in enumerate(lines):
        entry = line.strip()
        if not entry or entry.startswith("#"):
            continue
        above = lines[number - 1].strip() if number else ""
        if not above.startswith("#") or not above.lstrip("#").strip():
            found.append(entry)
    return found


# ── the targets ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("target", TARGETS)
def test_each_aws_target_is_phony_and_has_a_help_line(target: str) -> None:
    phony = next(line for line in MAKEFILE.splitlines() if line.startswith(".PHONY:"))

    assert target in phony.split()
    assert re.search(rf"^{target}:", MAKEFILE, re.MULTILINE)
    assert help_line(target)


@pytest.mark.parametrize(
    ("target", "command"),
    [
        ("aws-validate", "infra/terraform/aws.sh validate"),
        ("aws-plan", "infra/terraform/aws.sh plan"),
        ("aws-apply", "infra/terraform/aws.sh apply"),
        ("aws-destroy", "infra/terraform/aws.sh destroy"),
    ],
)
def test_each_aws_target_that_drives_terraform_runs_the_script_and_nothing_else(
    target: str, command: str
) -> None:
    assert recipe(target).strip() == command


def test_the_apply_target_says_it_spends_money_and_that_the_owner_runs_it() -> None:
    line = help_line("aws-apply")

    assert line.startswith("SPENDS MONEY")
    assert "owner" in line


def test_the_removal_target_says_the_owner_runs_it_in_a_terminal() -> None:
    line = help_line("aws-destroy")

    assert "owner" in line
    assert "terminal" in line


def test_the_free_targets_say_they_change_nothing_in_aws() -> None:
    for target in ("aws-validate", "aws-scan", "aws-plan"):
        assert "changes nothing in AWS" in help_line(target)


# ── the scan's image and recipe ──────────────────────────────────────────────


def test_the_scanner_image_is_pinned_by_tag_and_index_digest_with_a_walrus() -> None:
    (value,) = re.findall(r"^TRIVY_IMAGE\s*:=\s*(\S+)$", MAKEFILE, re.MULTILINE)

    assert re.fullmatch(
        r"ghcr\.io/aquasecurity/trivy:\d+\.\d+\.\d+@sha256:[0-9a-f]{64}", value
    )


def test_the_scan_runs_the_pinned_image_with_no_network_and_no_rights() -> None:
    text = recipe("aws-scan")

    assert "$(TRIVY_IMAGE)" in text
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
    assert "infra/terraform/aws" in text


def test_the_scan_fails_on_high_and_critical_and_uses_the_embedded_checks() -> None:
    text = recipe("aws-scan")

    assert " config " in text
    assert "--severity HIGH,CRITICAL" in text
    assert "--exit-code 1" in text
    # Without this Trivy tries to download its checks first and, with the network
    # off, falls back to the embedded ones after a wait.
    assert "--skip-check-update" in text


def test_the_scan_reads_the_ignore_file_that_sits_in_the_module() -> None:
    text = recipe("aws-scan")

    assert "-w /work" in text  # Trivy's default .trivyignore is the working dir's
    assert "--ignorefile" not in text  # which fails when the file is missing


def test_the_scan_refuses_a_directory_with_no_terraform_in_it() -> None:
    # A scan of nothing passes: say so when there is nothing to scan.
    assert ".tf" in recipe("aws-scan")


# ── the ignore file ──────────────────────────────────────────────────────────


def test_the_committed_ignore_file_has_a_reason_above_every_entry() -> None:
    text = TRIVYIGNORE.read_text(encoding="utf-8")

    assert text.startswith("#")
    assert entries_without_a_reason(text) == []


def test_an_entry_with_its_reason_directly_above_passes() -> None:
    text = "# header\n\n# Public subnets: a test lives an hour.\nAWS-0164\n"

    assert entries_without_a_reason(text) == []


def test_an_entry_with_no_comment_above_is_found() -> None:
    text = "# header\n\nAWS-0164\n"

    assert entries_without_a_reason(text) == ["AWS-0164"]


def test_an_entry_first_in_the_file_is_found() -> None:
    assert entries_without_a_reason("AWS-0164\n") == ["AWS-0164"]


def test_a_comment_across_a_blank_line_is_not_a_reason() -> None:
    text = "# Public subnets: a test lives an hour.\n\nAWS-0164\n"

    assert entries_without_a_reason(text) == ["AWS-0164"]


def test_an_entry_under_another_entry_needs_its_own_reason() -> None:
    text = "# Public subnets.\nAWS-0164\nAWS-0040\n"

    assert entries_without_a_reason(text) == ["AWS-0040"]


def test_a_comment_with_no_words_is_not_a_reason() -> None:
    text = "#\nAWS-0164\n# \nAWS-0040\n"

    assert entries_without_a_reason(text) == ["AWS-0164", "AWS-0040"]
