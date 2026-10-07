"""The self-managed cluster's checks and accepted scan findings (S079).

The managed module's ``make aws-scan`` and the rules for its ``.trivyignore``
are tested in ``test_aws_scan.py``; this module has the same kind of file with
its own findings. A finding the owner accepts for an environment that lives an
hour goes in ``infra/terraform/aws-kubeadm/.trivyignore`` with its reason on the
line above, and these tests hold the file to exactly the findings the first
scan of the module reported (the scan itself runs in a container and is not run
here).

``make aws-kubeadm-validate`` and ``make aws-kubeadm-scan`` are the twins of the
Google Cloud scaffold's pair, and the tests of the targets are the twins of
``test_gcp_scan.py``'s, whose helpers they use (with the managed module's).
The module has these two targets and, in this contract, no target that plans,
applies or removes it: a later contract adds those, and changes the one test
that says so.
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from servicesupport import REPO_ROOT
from test_aws_scan import (
    count_resources,
    entries,
    entries_without_a_reason,
    inline_ignores,
    malformed_entries,
    scanner_files,
)
from test_gcp_scan import MAKEFILE, help_line, phony_names, recipe

MODULE = REPO_ROOT / "infra" / "terraform" / "aws-kubeadm"
TRIVYIGNORE = MODULE / ".trivyignore"
ACCEPTED = ["AWS-0104", "AWS-0164", "AWS-0178"]
TARGETS = ("aws-kubeadm-validate", "aws-kubeadm-scan")
# The names a plan, an apply and a removal would have, by the pattern of the
# managed module's targets. None exists yet (a later contract, after the
# command guard can read them).
NOT_YET_BUILT = ("aws-kubeadm-plan", "aws-kubeadm-apply", "aws-kubeadm-destroy")
# What a by-hand run in the module's directory leaves (Terraform's default state
# names) and what the wrapper would leave of a plan, by the managed module's
# pattern (aws.tfplan and its record): a name nobody makes yet is skipped all the
# same, a file the scan would otherwise read as a configuration.
SKIPPED_FILES = {
    "aws-kubeadm.tfplan",
    "aws-kubeadm.tfplan.meta",
    "terraform.tfstate",
    "terraform.tfstate.backup",
}
# What the module's own tests must refuse in place of the ignore file's reading:
# an inline ignore of either of the pinned scanner's spellings.
INLINE_PLANT = "  #trivy:ignore:AWS-0104\n"


def module_text() -> str:
    return "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(MODULE.glob("*.tf"))
    )


def test_the_modules_ignore_file_has_a_reason_above_every_entry() -> None:
    text = TRIVYIGNORE.read_text(encoding="utf-8")

    assert text.startswith("#")
    assert entries(text) != []  # the reader found them
    assert entries_without_a_reason(text) == []


def test_the_modules_entries_are_each_exactly_a_check_id() -> None:
    text = TRIVYIGNORE.read_text(encoding="utf-8")

    assert malformed_entries(text) == []


def test_the_accepted_findings_are_the_three_the_scan_reports_unfiltered() -> None:
    """The scan, run with every severity, reports one HIGH (AWS-0164, the
    subnet), two CRITICAL (AWS-0104, the two egress rules) and one MEDIUM
    (AWS-0178, the VPC's missing flow logs), and nothing else. A fourth ID needs
    a change of this list, with its reason, in the same diff."""
    text = TRIVYIGNORE.read_text(encoding="utf-8")

    assert sorted(entries(text)) == ACCEPTED
    assert len(entries(text)) == len(set(entries(text)))


def test_each_reason_names_its_check_and_says_what_production_does() -> None:
    text = TRIVYIGNORE.read_text(encoding="utf-8")

    for check in ACCEPTED:
        (reason,) = re.findall(rf"^# ({check} .+)$", text, re.MULTILINE)
        assert "Production:" in reason, check
    (egress,) = re.findall(r"^# (AWS-0104 .+)$", text, re.MULTILINE)
    # The reason is the one security.tf gives: no fixed addresses, ports could
    # be narrowed and are not, and nothing of value on the nodes.
    assert "fixed address" in egress
    assert "limited by port and is not" in egress
    assert "nothing of value" in egress
    (subnet,) = re.findall(r"^# (AWS-0164 .+)$", text, re.MULTILINE)
    assert "NAT gateway" in subnet
    # The flow logs: why a network of an hour that holds no data goes without,
    # and what a production network sets instead.
    (flow_logs,) = re.findall(r"^# (AWS-0178 .+)$", text, re.MULTILINE)
    assert "flow logs" in flow_logs
    assert "lives an hour" in flow_logs
    assert "holds no data" in flow_logs
    assert "(medium)" in flow_logs
    assert "Production: flow logs" in flow_logs


def test_no_terraform_file_of_the_module_carries_an_inline_ignore_comment() -> None:
    found = {
        path.name: inline_ignores(path.read_text(encoding="utf-8"))
        for path in sorted(MODULE.glob("*.tf"))
    }

    assert {name: lines for name, lines in found.items() if lines} == {}


def test_an_inline_ignore_planted_in_the_modules_text_would_be_found() -> None:
    planted = INLINE_PLANT + (MODULE / "security.tf").read_text(encoding="utf-8")

    assert inline_ignores(planted) == [INLINE_PLANT.rstrip("\n")]


def test_the_module_directory_holds_no_scanner_configuration_or_yaml_ignore() -> None:
    assert scanner_files(MODULE) == []


def test_the_vpc_subnet_and_egress_rules_are_what_the_ignore_reasons_describe() -> None:
    """An entry applies to every resource of the directory: a second VPC, a
    second subnet or a third egress rule would be accepted unseen."""
    text = module_text()

    vpcs = count_resources(text, "aws_vpc")
    subnets = count_resources(text, "aws_subnet")
    egress = count_resources(text, "aws_vpc_security_group_egress_rule")

    reasons = (
        "infra/terraform/aws-kubeadm/.trivyignore accepts AWS-0178 for the one "
        "VPC, AWS-0164 for the one subnet and AWS-0104 for the two egress rules "
        "its reasons describe, and an ignore entry applies to every resource of "
        "the directory: a new VPC, subnet or egress rule is accepted silently. "
        "Decide again in that file, in the reasons, before changing these numbers."
    )
    assert vpcs == 1, reasons
    assert subnets == 1, reasons
    assert egress == 2, reasons


def test_the_module_makes_no_other_kind_of_egress_rule() -> None:
    text = module_text()

    # AWS-0104 reads these too, and each would be accepted by the same entry.
    assert count_resources(text, "aws_security_group_rule") == 0
    assert count_resources(text, "aws_network_acl_rule") == 0


@pytest.mark.parametrize(
    "line",
    [
        "  #trivy:ignore:AWS-0104",
        "// tfsec:ignore:AWS-0164",
        "/* trivy:ignore:* */",
    ],
)
def test_an_inline_ignore_of_either_spelling_is_found(line: str) -> None:
    assert inline_ignores(f'resource "x" "y" {{\n{line}\n}}\n') == [line]


def test_the_module_calls_no_module_that_could_hide_a_subnet_or_a_rule() -> None:
    found = [
        line
        for path in sorted(MODULE.glob("*.tf"))
        for line in path.read_text(encoding="utf-8").splitlines()
        if re.match(r'\s*module\s+("[^"]+"|\w+)', line)
    ]

    assert found == []
    assert sorted(path.name for path in MODULE.glob("*.tf.json")) == []


# ── the targets (twins of test_gcp_scan.py's) ────────────────────────────────


def without_the_module(text: str) -> str:
    """A scan recipe with what is the module's own made the same: the skipped
    file names, and the word that names the module (the directory, the target in
    the refusal's message). The longer name comes first in the pattern."""
    text = re.sub(r"--skip-files \S+", "--skip-files SKIPPED", text.strip())
    return re.sub(r"\b(aws-kubeadm|aws)\b", "MODULE", text)


@pytest.mark.parametrize("target", TARGETS)
def test_each_aws_kubeadm_target_is_phony_and_has_a_help_line(target: str) -> None:
    assert target in phony_names()
    assert re.search(rf"^{target}:", MAKEFILE, re.MULTILINE)
    assert help_line(target)


def test_the_validate_target_runs_the_script_with_the_module_name_alone() -> None:
    assert (
        recipe("aws-kubeadm-validate").strip()
        == "infra/terraform/aws.sh validate aws-kubeadm"
    )


def test_the_two_targets_say_they_change_nothing_in_aws_and_need_no_account() -> None:
    for target in TARGETS:
        line = help_line(target)
        assert "changes nothing in AWS" in line
        assert "needs no account" in line
    assert "offline" in help_line("aws-kubeadm-scan")


def test_no_target_plans_applies_or_removes_the_self_managed_module_yet() -> None:
    declared = set(re.findall(r"^(aws-kubeadm-[\w-]+):", MAKEFILE, re.MULTILINE))

    assert declared == set(TARGETS)
    assert not set(NOT_YET_BUILT) & set(phony_names())


def test_the_scan_uses_the_one_pinned_image_and_no_second_pin() -> None:
    assert "$(TRIVY_IMAGE)" in recipe("aws-kubeadm-scan")
    assert len(re.findall(r"^\w*TRIVY\w*\s*:=", MAKEFILE, re.MULTILINE)) == 1
    assert "aquasecurity/trivy" not in recipe("aws-kubeadm-scan")


def test_the_scan_runs_with_no_network_and_no_rights_on_this_directory_alone() -> None:
    text = recipe("aws-kubeadm-scan")

    for flag in (
        "--network none",
        "--read-only",
        "--cap-drop ALL",
        "--security-opt no-new-privileges",
    ):
        assert flag in text
    # --mount refuses a path that is not there, where -v would make an empty
    # directory that scans clean; and the managed module's directory is not it.
    assert re.search(r"--mount type=bind,[^ ]*readonly", text)
    assert 'source="$(CURDIR)/infra/terraform/aws-kubeadm"' in text
    assert "infra/terraform/aws/" not in text
    assert 'infra/terraform/aws"' not in text


def test_the_scan_stays_offline_and_fails_on_high_and_critical() -> None:
    text = recipe("aws-kubeadm-scan")

    assert " config " in text
    assert "--severity HIGH,CRITICAL" in text
    assert "--exit-code 1" in text
    for flag in ("--skip-check-update", "--skip-version-check", "--disable-telemetry"):
        assert flag in text


def test_the_scan_reads_the_ignore_file_that_sits_in_the_modules_directory() -> None:
    text = recipe("aws-kubeadm-scan")

    assert "-w /work" in text  # Trivy's default .trivyignore is the working dir's
    assert "--ignorefile" not in text  # which fails when the file is missing
    # The directory the recipe mounts is the one whose ignore file holds the three
    # accepted findings and nothing else: a recipe that mounted another directory
    # would read another file, or none.
    (source,) = re.findall(r'source="\$\(CURDIR\)/([^"]+)"', text)
    mounted = REPO_ROOT / source
    assert mounted == MODULE
    assert sorted(entries((mounted / ".trivyignore").read_text(encoding="utf-8"))) == (
        ACCEPTED
    )


def test_the_scan_leaves_out_the_terraform_directory_and_the_files_a_run_leaves() -> (
    None
):
    text = recipe("aws-kubeadm-scan")

    assert "--skip-dirs .terraform " in text
    (skipped,) = re.findall(r"--skip-files (\S+)", text)
    assert set(skipped.split(",")) == SKIPPED_FILES


def test_the_scan_refuses_a_directory_with_no_terraform_in_it_before_docker_runs(
    tmp_path: Path,
) -> None:
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
            "aws-kubeadm-scan",
        ],
        capture_output=True,
        text=True,
        check=False,
        env={"PATH": f"{bin_dir}{os.pathsep}/usr/bin{os.pathsep}/bin"},
    )

    assert result.returncode != 0
    assert (
        "aws-kubeadm-scan: no .tf file in infra/terraform/aws-kubeadm" in result.stderr
    )
    assert not called.exists()


def test_the_two_scan_recipes_are_equal_but_for_directory_and_skipped_names() -> None:
    kubeadm = without_the_module(recipe("aws-kubeadm-scan"))
    aws = without_the_module(recipe("aws-scan"))

    assert kubeadm == aws
    # The comparison is not vacuous: each recipe names its own directory.
    assert "infra/terraform/aws-kubeadm" in recipe("aws-kubeadm-scan")
    assert "infra/terraform/aws/" in recipe("aws-scan")
    assert "--skip-files" in kubeadm


def test_a_flag_in_one_recipe_only_makes_the_recipes_differ() -> None:
    aws = recipe("aws-scan")
    kubeadm = recipe("aws-kubeadm-scan")
    for flag in ("--network none", "--cap-drop ALL", "--exit-code 1"):
        assert flag in kubeadm
        weakened = kubeadm.replace(flag, "")
        assert without_the_module(weakened) != without_the_module(aws), flag
    extra = kubeadm.replace(" config ", " config --scanners misconfig ")
    assert without_the_module(extra) != without_the_module(aws)
