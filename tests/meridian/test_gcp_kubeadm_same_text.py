"""The twin's boot scripts say what the AWS module's say, where they can (S079).

``infra/terraform/gcp-kubeadm/templates/`` holds the cloud-init scripts of the
Google Cloud twin of the self-managed cluster. The parts that are not about a
cloud (the package lock wait and retry, the kernel modules, the signing key's
pins, the kubeadm install, the run-once guards, the strict reading of a join
command, the network plugin's digest check) must be the SAME TEXT as the AWS
module's, so that a fix made in one module is not forgotten in the other. The AWS
module's files are not moved or changed to make that so: the twin holds a copy,
and these tests hold the two equal, function by function and setting by setting.

What may differ is a table below, each entry with its reason. A difference that
is not in the table fails a test, and so does an entry whose words are not in the
AWS text any more (so that the table cannot go on excusing a difference that no
longer exists). The twin's own functions (the metadata server, Secret Manager,
the waits that are about an address or a secret) are listed too, and a test
holds that none of them has the name of an AWS-only function.
"""

import re
from pathlib import Path

import pytest
from servicesupport import REPO_ROOT

AWS = REPO_ROOT / "infra" / "terraform" / "aws-kubeadm" / "templates"
GCP = REPO_ROOT / "infra" / "terraform" / "gcp-kubeadm" / "templates"

# Each function that is the same text on both clouds: (template, name). Its
# comment block (the unbroken run of comment lines above it) is part of its text.
SHARED_FUNCTIONS = [
    ("node-common", "log"),
    ("node-common", "die"),
    ("node-common", "require_number"),
    ("node-common", "fetch"),
    ("node-common", "parse_join_command"),
    ("node-common", "apt_retry"),
    ("node-common", "apt_get"),
    ("node-common", "apt_mark_hold"),
    ("node-common", "make_the_pinned_keyring"),
    ("node-common", "wait_for_containerd"),
    ("node-common", "install_node"),
    ("control-plane", "refuse_a_second_run"),
    ("control-plane", "initialise_the_control_plane"),
    ("control-plane", "apply_the_network_plugin"),
    ("worker", "refuse_a_second_run"),
    ("worker", "join_the_cluster"),
]

# The settings that are the same text on both clouds: (template, name). A
# setting's text is its comment block and its one line.
SHARED_SETTINGS = [
    ("node-common", "KUBERNETES_MINOR"),
    ("node-common", "KUBERNETES_APT_KEY_FINGERPRINT"),
    ("node-common", "CONTAINERD_ATTEMPTS"),
    ("node-common", "APT_ATTEMPTS"),
    ("node-common", "JOIN_PATTERN"),
]

# The words of the AWS text that the twin says differently, by function: each is
# applied to the AWS text before the comparison, and each must be found in it.
# The join command is kept in a secret here and in a parameter there, and the
# control plane's endpoint is the node's internal address here (the workers join
# there, so no node reaches another through its public address) where it is the
# Elastic IP there.
DIFFERENCES: dict[str, list[tuple[str, str, str]]] = {
    "parse_join_command": [
        ("the parameter", "the secret", "the join command is kept in a secret"),
    ],
    "join_the_cluster": [
        ("the parameter", "the secret", "the join command is kept in a secret"),
    ],
    "initialise_the_control_plane": [
        (
            '--control-plane-endpoint "$PUBLIC_ADDRESS:$API_PORT"',
            '--control-plane-endpoint "$INTERNAL_ADDRESS:$API_PORT"',
            "the endpoint is the internal address, the public one is a name",
        ),
    ],
}

# What each side has that the other does not, by template: the AWS module installs
# the AWS command line and reads the metadata service at version 2; the twin has
# the metadata server's token, the project, the two addresses, Secret Manager, and
# its own work directory (it names its own files).
AWS_ONLY = {"install_aws_cli", "imds", "wait_for_the_elastic_ip"}
GCP_ONLY = {
    "metadata",
    "read_the_project_id",
    "make_the_auth_header",
    "secret_api",
    "wait_for_the_addresses",
    "read_the_secret",
}
# Different on purpose, and not compared as text: each is in both modules and does
# the same job in each cloud's own way (where the join command is published and
# read, how a failed read is worded, and which files the work directory holds).
DIFFERENT_BY_DESIGN = {
    "make_work_dir",
    "publish_the_join_command",
    "wait_for_the_join_command",
    "the_last_error",
}


def text_of(directory: Path, template: str) -> str:
    return (directory / f"{template}.sh.tftpl").read_text(encoding="utf-8")


def with_comments(text: str, first: int, last: int) -> str:
    """Lines ``first``..``last`` and the unbroken run of comment lines above."""
    lines = text.splitlines()
    top = first
    while top > 0 and lines[top - 1].startswith("#"):
        top -= 1
    return "\n".join(lines[top : last + 1])


def function_text(text: str, name: str) -> str | None:
    """The function ``name`` with its comment block, or None if it is not there.
    A function starts at ``name() {`` in the first column and ends at the first
    ``}`` in the first column."""
    lines = text.splitlines()
    starts = [n for n, line in enumerate(lines) if line == f"{name}() {{"]
    if not starts:
        return None
    assert len(starts) == 1, f"{name} is defined twice"
    (start,) = starts
    end = next(n for n in range(start, len(lines)) if lines[n] == "}")
    return with_comments(text, start, end)


def setting_text(text: str, name: str) -> str | None:
    lines = text.splitlines()
    found = [n for n, line in enumerate(lines) if line.startswith(f"{name}=")]
    if not found:
        return None
    assert len(found) == 1, f"{name} is set twice"
    return with_comments(text, found[0], found[0])


def defined_functions(text: str) -> set[str]:
    return set(re.findall(r"^(\w+)\(\) \{$", text, re.MULTILINE))


def aws_text_as_the_twin_says_it(name: str, text: str) -> str:
    for old, new, _reason in DIFFERENCES.get(name, []):
        assert old in text, f"{name}: {old!r} is not in the AWS text any more"
        text = text.replace(old, new)
    return text


@pytest.mark.parametrize(("template", "name"), SHARED_FUNCTIONS)
def test_a_function_that_is_not_about_a_cloud_is_the_same_text_on_both_clouds(
    template: str, name: str
) -> None:
    aws = function_text(text_of(AWS, template), name)
    gcp = function_text(text_of(GCP, template), name)

    assert aws is not None, f"{template}: the AWS module has no function {name}"
    assert gcp is not None, f"{template}: the twin has no function {name}"
    assert gcp == aws_text_as_the_twin_says_it(name, aws)


@pytest.mark.parametrize(("template", "name"), SHARED_SETTINGS)
def test_a_setting_that_is_not_about_a_cloud_is_the_same_text_on_both_clouds(
    template: str, name: str
) -> None:
    aws = setting_text(text_of(AWS, template), name)
    gcp = setting_text(text_of(GCP, template), name)

    assert aws is not None, f"{template}: the AWS module does not set {name}"
    assert gcp is not None, f"{template}: the twin does not set {name}"
    assert gcp == aws


@pytest.mark.parametrize("name", sorted(DIFFERENCES))
def test_a_word_the_twin_says_differently_is_still_in_the_aws_text(name: str) -> None:
    template = next(t for t, n in SHARED_FUNCTIONS if n == name)
    aws = function_text(text_of(AWS, template), name)

    assert aws is not None
    for old, _new, reason in DIFFERENCES[name]:
        assert old in aws, reason


def test_the_table_of_differences_names_only_functions_that_are_held_equal() -> None:
    held = {name for _template, name in SHARED_FUNCTIONS}

    assert set(DIFFERENCES) <= held


def all_functions(directory: Path) -> set[str]:
    found: set[str] = set()
    for template in ("node-common", "control-plane", "worker"):
        found |= defined_functions(text_of(directory, template))
    return found


def test_every_function_of_the_aws_scripts_is_held_equal_or_named_as_different() -> (
    None
):
    """A function added to the AWS module's scripts must be either held equal here
    or named as different on purpose: it cannot be left out unseen."""
    held = {name for _template, name in SHARED_FUNCTIONS}

    assert all_functions(AWS) == held | AWS_ONLY | DIFFERENT_BY_DESIGN


def test_every_function_of_the_twins_scripts_is_held_equal_or_is_one_of_its_own() -> (
    None
):
    held = {name for _template, name in SHARED_FUNCTIONS}

    assert all_functions(GCP) == held | GCP_ONLY | DIFFERENT_BY_DESIGN


def test_no_function_that_is_about_aws_is_in_the_twin() -> None:
    assert all_functions(GCP) & AWS_ONLY == set()


@pytest.mark.parametrize("template", ["control-plane", "worker"])
def test_the_two_lines_that_set_the_shell_and_its_alphabet_are_the_same_text(
    template: str,
) -> None:
    def preamble(text: str) -> list[str]:
        return [
            line
            for line in text.splitlines()
            if line in {"set -euo pipefail", "export LC_ALL=C", "export HOME=/root"}
        ]

    assert preamble(text_of(GCP, template)) == preamble(text_of(AWS, template))
    assert len(preamble(text_of(GCP, template))) == 3


def test_a_function_found_in_one_text_and_changed_in_the_other_is_not_equal() -> None:
    aws = function_text(text_of(AWS, "node-common"), "apt_retry")
    assert aws is not None
    changed = aws.replace('sleep "$POLL_SECONDS"', "sleep 1")

    assert changed != aws
    assert changed != aws_text_as_the_twin_says_it("apt_retry", aws)


def test_a_comment_block_is_part_of_a_functions_text() -> None:
    text = "# one\n# two\nname() {\n  :\n}\n"

    assert function_text(text, "name") == text.rstrip("\n")
    assert function_text("# one\n\nname() {\n  :\n}\n", "name") == "name() {\n  :\n}"
