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
MODULE = REPO_ROOT / "infra" / "terraform" / "aws"
TRIVYIGNORE = MODULE / ".trivyignore"
TARGETS = ("aws-validate", "aws-scan", "aws-plan", "aws-apply", "aws-destroy")
# The shortest reason that can say what a check says, why it holds here and what
# production does: a handful of words.
REASON_WORDS = 5
# An entry is a check ID and nothing else: Trivy takes `AWS-0107 exp:2099-12-31`
# as an expiry, `AWS-0107 file.tf` as the ID alone (for every file), and the
# older `AVD-AWS-0107` spelling as the same check.
ENTRY = re.compile(r"AWS-\d{4}")
# What Trivy reads from the directory it scans, besides .trivyignore, and what
# can switch a finding off or change the severity and the exit code.
SCANNER_FILES = ("trivy*.yaml", "trivy*.yml", ".trivy*.yaml", ".trivy*.yml")


def recipe(target: str) -> str:
    """The lines under ``target:`` up to the next blank line."""
    return MAKEFILE.split(f"\n{target}:\n", 1)[1].split("\n\n", 1)[0]


def help_line(target: str) -> str:
    (line,) = re.findall(rf"^## {target} +(.+)$", MAKEFILE, re.MULTILINE)
    return line


def comment_above(assignment: str) -> str:
    """The comment lines directly above a Makefile variable's assignment."""
    lines = MAKEFILE.splitlines()
    (index,) = [i for i, line in enumerate(lines) if line.startswith(assignment)]
    found = []
    for line in reversed(lines[:index]):
        if not line.startswith("#"):
            break
        found.append(line)
    return "\n".join(reversed(found))


def entries(text: str) -> list[str]:
    return [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def entries_without_a_reason(text: str) -> list[str]:
    """The entries of an ignore file whose line above is not a comment of at
    least a handful of words. A blank line between a comment and an entry
    breaks the link, and so does another entry."""
    lines = text.splitlines()
    found = []
    for number, line in enumerate(lines):
        entry = line.strip()
        if not entry or entry.startswith("#"):
            continue
        above = lines[number - 1].strip() if number else ""
        reason = above.lstrip("#").strip() if above.startswith("#") else ""
        if len(reason.split()) < REASON_WORDS:
            found.append(entry)
    return found


def malformed_entries(text: str) -> list[str]:
    """The entries that are not exactly a check ID of the AWS- and four digits
    shape: nothing after it (no expiry, no file name), not the older AVD-
    spelling, no wildcard."""
    return [entry for entry in entries(text) if not ENTRY.fullmatch(entry)]


def inline_ignores(text: str) -> list[str]:
    """Lines of a Terraform file that carry a Trivy ignore comment, which
    silences a check for one resource where no reason is asked for."""
    return [line for line in text.splitlines() if "trivy:ignore" in line.lower()]


def scanner_files(directory) -> list[str]:
    return sorted(
        path.name for pattern in SCANNER_FILES for path in directory.glob(pattern)
    )


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
    text = "# Public subnets: a test lives an hour.\nAWS-0164\nAWS-0040\n"

    assert entries_without_a_reason(text) == ["AWS-0040"]


def test_a_comment_with_no_words_is_not_a_reason() -> None:
    text = "#\nAWS-0164\n# \nAWS-0040\n"

    assert entries_without_a_reason(text) == ["AWS-0164", "AWS-0040"]


def test_a_reason_of_fewer_than_a_handful_of_words_is_not_a_reason() -> None:
    for text in (
        "# x\nAWS-0164\n",
        "# Public subnets.\nAWS-0164\n",
        "# a b c d\nAWS-0164\n",
    ):
        assert entries_without_a_reason(text) == ["AWS-0164"], text


def test_a_reason_of_a_handful_of_words_is_a_reason() -> None:
    text = "# one two three four five\nAWS-0164\n"

    assert entries_without_a_reason(text) == []


def test_the_committed_entries_are_each_exactly_a_check_id() -> None:
    text = TRIVYIGNORE.read_text(encoding="utf-8")

    assert entries(text) != []  # the reader found them
    assert malformed_entries(text) == []


@pytest.mark.parametrize(
    "entry",
    [
        "AWS-0107 exp:2099-12-31",  # an expiry: the entry is dropped when it passes
        "AWS-0107 cluster.tf",  # trailing text: Trivy keeps the ID, for every file
        "AWS-0107 # a reason on the line itself",
        "AVD-AWS-0107",  # the older spelling of the same check
        "avd-aws-0107",
        "aws-0107",
        "AWS-107",
        "AWS-01070",
        "AWS-*",
        "*",
        "AWS-0107,AWS-0040",
    ],
)
def test_an_entry_that_is_not_exactly_a_check_id_is_found(entry: str) -> None:
    assert malformed_entries(f"# a reason of enough words here\n{entry}\n") == [entry]


def test_an_entry_that_is_exactly_a_check_id_is_not_found() -> None:
    assert malformed_entries("# a reason of enough words here\nAWS-0107\n") == []


# ── what could silence the scan some other way ──────────────────────────────


def test_no_terraform_file_of_the_module_carries_an_inline_ignore_comment() -> None:
    found = {
        path.name: inline_ignores(path.read_text(encoding="utf-8"))
        for path in sorted(MODULE.glob("*.tf"))
    }

    assert {name: lines for name, lines in found.items() if lines} == {}


@pytest.mark.parametrize(
    "line",
    [
        "  #trivy:ignore:AWS-0107",
        "# trivy:ignore:AWS-0107:exp:2099-12-31",
        "  //trivy:ignore:aws-0107",
        "#TRIVY:IGNORE:AWS-0107",
    ],
)
def test_an_inline_ignore_comment_is_found(line: str) -> None:
    assert inline_ignores(f'resource "x" "y" {{\n{line}\n}}\n') == [line]


def test_an_ordinary_comment_is_not_an_inline_ignore() -> None:
    assert inline_ignores("# Trivy accepts this, see .trivyignore\n") == []


def test_the_module_directory_holds_no_scanner_configuration_or_yaml_ignore_file() -> (
    None
):
    assert scanner_files(MODULE) == []


@pytest.mark.parametrize(
    "name", ["trivy.yaml", "trivy.yml", ".trivyignore.yaml", ".trivyignore.yml"]
)
def test_a_scanner_configuration_or_yaml_ignore_file_is_found(
    tmp_path, name: str
) -> None:
    (tmp_path / name).write_text("scan:\n  skip-files: []\n")

    assert scanner_files(tmp_path) == [name]


def test_the_clusters_and_subnets_are_held_to_what_the_ignore_reasons_describe() -> (
    None
):
    """AWS-0039 and AWS-0040 are about a cluster, AWS-0164 about a subnet, and an
    entry in .trivyignore applies to every resource of the directory: a second
    cluster or subnet block would be accepted unseen."""
    text = "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(MODULE.glob("*.tf"))
    )

    clusters = len(re.findall(r'^resource "aws_eks_cluster" ', text, re.MULTILINE))
    subnets = len(re.findall(r'^resource "aws_subnet" ', text, re.MULTILINE))

    reasons = (
        "infra/terraform/aws/.trivyignore accepts AWS-0039 and AWS-0040 for the one "
        "cluster and AWS-0164 for the one subnet block (two zones, by count) its "
        "reasons describe, and an ignore entry applies to every resource of the "
        "directory: a new cluster or subnet is accepted silently. Decide again in "
        "that file, in the reasons, before changing this number."
    )
    assert clusters == 1, reasons
    assert subnets == 1, reasons


def test_the_cluster_reasons_say_what_eks_encrypts_and_who_needs_the_endpoint() -> None:
    text = TRIVYIGNORE.read_text(encoding="utf-8")

    (secrets,) = re.findall(r"^# (AWS-0039 .+)$", text, re.MULTILINE)
    (endpoint,) = re.findall(r"^# (AWS-0040 .+)$", text, re.MULTILINE)
    # EKS 1.28 and later encrypts Kubernetes API data by default, with an
    # AWS-owned key; the check asks for a customer-managed one.
    assert "AWS-owned" in secrets
    assert "customer-managed" in secrets
    # Terraform needs only the EKS API; the Kubernetes API's public endpoint is
    # for kubectl (and anything else that talks to the cluster itself).
    assert "kubectl" in endpoint
    assert "EKS API" in endpoint


def test_the_scan_leaves_out_the_plan_the_record_and_the_state_files() -> None:
    text = recipe("aws-scan")

    (skipped,) = re.findall(r"--skip-files (\S+)", text)
    assert set(skipped.split(",")) == {
        "aws.tfplan",
        "aws.tfplan.meta",
        "terraform.tfstate",
        "terraform.tfstate.backup",
    }


@pytest.mark.parametrize("variable", ["PROMTOOL_IMAGE", "TRIVY_IMAGE"])
def test_the_comment_on_a_pinned_image_says_a_command_line_can_override_it(
    variable: str,
) -> None:
    comment = " ".join(comment_above(f"{variable} ").replace("#", " ").split())

    assert "does not override" not in comment
    assert "command line" in comment
    assert "can override it" in comment.lower()


def test_the_removal_target_does_not_claim_more_than_the_terminal_check_does() -> None:
    line = help_line("aws-destroy")

    assert "no session can run" not in line
    assert "cannot run" not in line
    assert "not a session that makes itself a terminal" in line


def test_the_readme_says_what_stops_a_session_and_what_does_not() -> None:
    readme = (MODULE / "README.md").read_text(encoding="utf-8")

    assert "## What stops a session, and what does not" in readme
    assert "a session cannot run it" not in readme
    assert "which a session's shell does not have" not in readme
