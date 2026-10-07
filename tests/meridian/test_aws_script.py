"""The commands around the AWS modules, part one: ``infra/terraform/aws.sh``.

This file holds what the script does before it plans: its usage, the validate
door and its module word, the pin of the account, the local file it reads and
never runs, the environment it gives its programs, tracing, the umask, the git
it needs, the files that may not change a plan unseen. The plan, the state, the
saved plan, apply and the removal are in ``test_aws_script_plan.py``, and the
text of the managed module's own files in ``test_aws_module_rules.py``.
``awsscriptsupport.py`` holds what the three share: the stand-in tree and its
programs, and the table of the two modules (``module`` runs a test once on each).
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from awsscriptsupport import (
    AWS_NAMES,
    BASE_NAMES,
    BUDGET_EMAIL,
    ENDPOINT_CIDR,
    GIT_ENV,
    LOCAL_FILE,
    LOCAL_LINES,
    MANAGED,
    MODULE_IDS,
    MODULES,
    NOISE,
    OTHER,
    PINNED,
    REFUSING,
    SCRIPT_SETS,
    SELF_MANAGED,
    SHELL_ADDS,
    TERRAFORM_DIR,
    TF_VAR_NAMES,
    Module,
    Tree,
    assert_no_identifier_printed,
    assert_plan_refused_and_dropped,
    assert_refused,
    each_module,
    env_names,
    everything_printed,
    git,
    only_the_managed_module,
    only_the_self_managed_module,
    run_refusing,
    tree_of,
    variable_block,
    with_local_file,
    with_saved_plan,
)
from servicesupport import REPO_ROOT

module_fixture = pytest.fixture(params=MODULES, ids=MODULE_IDS, name="module")(
    each_module
)
tree_fixture = pytest.fixture(name="tree")(tree_of)

# ── usage ────────────────────────────────────────────────────────────────────


@only_the_managed_module
@pytest.mark.parametrize("words", [[], ["frobnicate"], ["plan", "apply"]])
def test_an_unknown_or_missing_sub_command_prints_usage_and_exits_two(
    tree: Tree, words: list[str]
) -> None:
    command = [str(tree.script), *words]

    done = subprocess.run(
        command,
        capture_output=True,
        text=True,
        env=tree.base_env,
        cwd=tree.root,
        stdin=subprocess.DEVNULL,
        timeout=60,
        check=False,
    )

    assert done.returncode == 2
    assert "usage:" in done.stderr
    assert "validate|plan|apply|destroy" in done.stderr
    assert tree.calls() == []


# ── validate ─────────────────────────────────────────────────────────────────


def test_validate_checks_the_format_then_initialises_with_no_backend_then_validates(
    tree: Tree,
) -> None:
    done = tree.run("validate")

    assert done.returncode == 0, done.stderr
    assert tree.subcommands() == ["fmt", "init", "validate"]
    fmt, init, _ = tree.calls()
    assert f"-chdir={tree.module}" in fmt
    assert "-check" in fmt
    assert "-backend=false" in init
    assert "-input=false" in init
    assert "-backend-config" not in init


def test_validate_needs_no_local_file_and_never_calls_the_aws_cli(tree: Tree) -> None:
    assert not tree.local_file.exists()

    done = tree.run("validate")

    assert done.returncode == 0, done.stderr
    assert tree.aws_calls() == []


def test_validate_stops_at_a_format_difference_and_says_how_to_fix_it(
    tree: Tree,
) -> None:
    done = tree.run("validate", STUB_FMT_STATUS="3")

    assert done.returncode != 0
    assert "fmt" in done.stderr
    assert "terraform -chdir=" in done.stderr
    assert tree.subcommands() == ["fmt"]


def test_what_terraform_prints_passes_through_redact(tree: Tree) -> None:
    done = tree.run("validate")

    assert "stub terraform validate: <arn>" in done.stdout
    assert NOISE not in everything_printed(done)


def test_validate_runs_terraform_with_no_aws_credential_at_all(tree: Tree) -> None:
    done = tree.run(
        "validate",
        **CREDENTIALS,
        AWS_REGION="eu-central-1",
    )

    assert done.returncode == 0, done.stderr
    names = env_names(tree.terraform_env())
    assert {n for n in names if n.startswith("AWS_")} == set()
    assert "PATH" in names  # the run did reach the stub, and it saw a path


# ── validate takes the name of a module (S078) ───────────────────────────────

GOOGLE_CREDENTIALS = {
    "GOOGLE_APPLICATION_CREDENTIALS": "/nonexistent/a-key-file",
    "GOOGLE_CREDENTIALS": "not-a-real-credential",
    "GOOGLE_OAUTH_ACCESS_TOKEN": "not-a-real-token",
    "GOOGLE_CLOUD_PROJECT": "example-project",
    "CLOUDSDK_CORE_PROJECT": "example-project",
    "CLOUDSDK_AUTH_ACCESS_TOKEN_FILE": "/nonexistent/a-token-file",
    "CLOUDSDK_CONFIG": "/nonexistent/a-gcloud-directory",
}
# Every name that could carry a credential of a cloud: none may reach the
# programs of a validate, whichever module it checks.
CREDENTIAL_PREFIXES = ("AWS_", "GOOGLE_", "CLOUDSDK_", "ARM_", "AZURE_")


def with_a_gcp_directory(tree: Tree) -> Path:
    """The stand-in tree has the AWS module's directory only; the Google Cloud
    one is made by the tests that name it."""
    gcp = tree.root / "infra" / "terraform" / "gcp"
    gcp.mkdir()
    (gcp / "main.tf").write_text("# a stand-in module\n")
    return gcp


def with_an_aws_kubeadm_directory(tree: Tree) -> Path:
    """The self-managed module's directory (S079), made by the tests that name it."""
    kubeadm = tree.root / "infra" / "terraform" / "aws-kubeadm"
    kubeadm.mkdir()
    (kubeadm / "main.tf").write_text("# a stand-in module\n")
    return kubeadm


def with_a_gcp_kubeadm_directory(tree: Tree) -> Path:
    """The Google Cloud twin's directory (S079), made by the tests that name it."""
    twin = tree.root / "infra" / "terraform" / "gcp-kubeadm"
    twin.mkdir()
    (twin / "main.tf").write_text("# a stand-in module\n")
    return twin


def run_words(tree: Tree, *words: str, **env: str) -> subprocess.CompletedProcess[str]:
    """The script with these words exactly (tree.run takes one sub-command)."""
    return subprocess.run(
        tree.command(*words),
        capture_output=True,
        text=True,
        env={**tree.base_env, **env},
        cwd=tree.root,
        stdin=subprocess.DEVNULL,
        timeout=60,
        check=False,
    )


@only_the_managed_module
def test_validate_with_the_word_gcp_runs_the_three_commands_on_the_gcp_directory(
    tree: Tree,
) -> None:
    gcp = with_a_gcp_directory(tree)

    done = run_words(tree, "validate", "gcp")

    assert done.returncode == 0, everything_printed(done)
    assert tree.subcommands() == ["fmt", "init", "validate"]
    for call in tree.calls():
        assert f"-chdir={gcp} " in call
        assert f"-chdir={tree.module}" not in call
    fmt, init, _ = tree.calls()
    assert "-check" in fmt
    assert "-backend=false" in init
    assert "-lockfile=readonly" in init


@only_the_managed_module
def test_validate_with_the_word_aws_kubeadm_runs_the_three_commands_on_its_directory(
    tree: Tree,
) -> None:
    kubeadm = with_an_aws_kubeadm_directory(tree)

    done = run_words(tree, "validate", "aws-kubeadm")

    assert done.returncode == 0, everything_printed(done)
    assert tree.subcommands() == ["fmt", "init", "validate"]
    for call in tree.calls():
        assert f"-chdir={kubeadm} " in call
        assert f"-chdir={tree.module} " not in call
    fmt, init, _ = tree.calls()
    assert "-check" in fmt
    assert "-backend=false" in init
    assert "-lockfile=readonly" in init


@only_the_managed_module
def test_validate_with_the_word_gcp_kubeadm_runs_the_three_commands_on_its_directory(
    tree: Tree,
) -> None:
    twin = with_a_gcp_kubeadm_directory(tree)
    with_a_gcp_directory(tree)

    done = run_words(tree, "validate", "gcp-kubeadm")

    assert done.returncode == 0, everything_printed(done)
    assert tree.subcommands() == ["fmt", "init", "validate"]
    for call in tree.calls():
        assert f"-chdir={twin} " in call
        assert f"-chdir={tree.module} " not in call
    fmt, init, _ = tree.calls()
    assert "-check" in fmt
    assert "-backend=false" in init
    assert "-lockfile=readonly" in init


@only_the_managed_module
def test_validate_with_the_word_aws_checks_the_aws_directory_as_with_no_word(
    tree: Tree,
) -> None:
    with_a_gcp_directory(tree)

    done = run_words(tree, "validate", "aws")

    assert done.returncode == 0, everything_printed(done)
    assert tree.subcommands() == ["fmt", "init", "validate"]
    for call in tree.calls():
        assert f"-chdir={tree.module} " in call


@only_the_managed_module
def test_a_format_difference_in_the_gcp_module_names_the_gcp_directory(
    tree: Tree,
) -> None:
    with_a_gcp_directory(tree)
    tree.write_stub_env({"STUB_FMT_STATUS": "3"})

    done = run_words(tree, "validate", "gcp")

    assert done.returncode != 0
    assert "terraform -chdir=infra/terraform/gcp fmt" in done.stderr
    assert "infra/terraform/aws" not in done.stderr
    assert tree.subcommands() == ["fmt"]


@only_the_managed_module
def test_a_format_difference_in_the_aws_kubeadm_module_names_its_directory(
    tree: Tree,
) -> None:
    with_an_aws_kubeadm_directory(tree)
    tree.write_stub_env({"STUB_FMT_STATUS": "3"})

    done = run_words(tree, "validate", "aws-kubeadm")

    assert done.returncode != 0
    assert "terraform -chdir=infra/terraform/aws-kubeadm fmt" in done.stderr
    assert "terraform -chdir=infra/terraform/aws fmt" not in done.stderr
    assert tree.subcommands() == ["fmt"]


@only_the_managed_module
def test_a_format_difference_in_the_gcp_kubeadm_module_names_its_directory(
    tree: Tree,
) -> None:
    with_a_gcp_kubeadm_directory(tree)
    tree.write_stub_env({"STUB_FMT_STATUS": "3"})

    done = run_words(tree, "validate", "gcp-kubeadm")

    assert done.returncode != 0
    assert "terraform -chdir=infra/terraform/gcp-kubeadm fmt" in done.stderr
    assert "terraform -chdir=infra/terraform/aws fmt" not in done.stderr
    assert "terraform -chdir=infra/terraform/gcp fmt" not in done.stderr
    assert tree.subcommands() == ["fmt"]


@only_the_managed_module
@pytest.mark.parametrize(
    "words",
    [
        ["validate", "azure"],
        ["validate", "foundation"],
        ["validate", "GCP"],
        ["validate", "gcp/"],
        ["validate", "gcp "],
        ["validate", ""],
        ["validate", "infra/terraform/gcp"],
        ["validate", "/nonexistent"],
        ["validate", "."],
        ["validate", ".."],
        ["validate", "../aws"],
        ["validate", "gcp/../aws"],
        ["validate", "-chdir=/nonexistent"],
        ["validate", "aws", "gcp"],
        ["validate", "gcp", "gcp"],
        ["validate", "gcp", "aws"],
        # The self-managed module's near misses: the literal list holds the one
        # name, and no pattern, path or other spelling of it.
        ["validate", "aws-kubeadm/"],
        ["validate", "aws-kubeadm "],
        ["validate", "aws_kubeadm"],
        ["validate", "AWS-KUBEADM"],
        ["validate", "aws-kubeadm-"],
        ["validate", "aws-kubeadm*"],
        ["validate", "aws-*"],
        ["validate", "../aws-kubeadm"],
        ["validate", "infra/terraform/aws-kubeadm"],
        ["validate", "./aws-kubeadm"],
        ["validate", "kubeadm"],
        ["validate", "aws-kubeadm", "aws-kubeadm"],
        ["validate", "aws-kubeadm", "aws"],
        ["validate", "aws", "aws-kubeadm"],
        # The Google Cloud twin's near misses, as the AWS module's above.
        ["validate", "gcp-kubeadm/"],
        ["validate", "gcp-kubeadm "],
        ["validate", "gcp_kubeadm"],
        ["validate", "GCP-KUBEADM"],
        ["validate", "gcp-kubeadm-"],
        ["validate", "gcp-kubeadm*"],
        ["validate", "gcp-*"],
        ["validate", "../gcp-kubeadm"],
        ["validate", "infra/terraform/gcp-kubeadm"],
        ["validate", "./gcp-kubeadm"],
        ["validate", "gcp-kubeadm", "gcp-kubeadm"],
        ["validate", "gcp-kubeadm", "gcp"],
        ["validate", "gcp-kubeadm", "aws-kubeadm"],
        ["validate", "gcp", "gcp-kubeadm"],
    ],
)
def test_validate_with_any_other_word_or_with_two_is_refused_before_a_program_runs(
    tree: Tree, words: list[str]
) -> None:
    with_a_gcp_directory(tree)
    with_an_aws_kubeadm_directory(tree)
    with_a_gcp_kubeadm_directory(tree)

    done = run_words(tree, *words)

    assert done.returncode == 2, everything_printed(done)
    assert "usage:" in done.stderr
    assert tree.calls() == []
    assert tree.aws_calls() == []


# ── plan, apply and the removal take one word, aws-kubeadm (S079) ────────────


def with_the_other_directory(tree: Tree) -> Path:
    """The directory of the module this tree does not run, so that a test can
    show the script never reads it."""
    other = SELF_MANAGED if tree.row is MANAGED else MANAGED
    directory = tree.root / "infra" / "terraform" / other.directory
    directory.mkdir()
    (directory / "main.tf").write_text("# a stand-in module\n")
    return directory


@only_the_self_managed_module
@pytest.mark.parametrize("subcommand", REFUSING)
def test_the_word_aws_kubeadm_is_accepted_and_selects_that_modules_places(
    tree: Tree, subcommand: str
) -> None:
    managed = with_the_other_directory(tree)
    with_local_file(tree)
    with_saved_plan(tree)

    assert tree.command(*tree.words(subcommand))[1:] == [subcommand, "aws-kubeadm"]

    done = tree.run(subcommand, terminal=subcommand == "destroy")

    assert done.returncode == 0, everything_printed(done)
    assert tree.calls() != []
    for call in tree.calls():
        assert f"-chdir={tree.module} " in call
        assert f"-chdir={managed} " not in call
    if subcommand == "plan":
        assert f"-out={tree.plan_file}" in tree.terraform_calls("plan")[0]
        assert (tree.module / tree.meta_file).exists()
    if subcommand == "apply":
        assert tree.terraform_calls("apply")[0].split()[-1] == tree.plan_file
    if subcommand != "apply":
        (init,) = tree.terraform_calls("init")
        assert f"-backend-config=path={tree.state_path}" in init


@only_the_managed_module
@pytest.mark.parametrize("subcommand", ["plan", "apply"])
def test_no_word_still_means_the_managed_module_when_the_other_directory_exists(
    tree: Tree, subcommand: str
) -> None:
    other = with_the_other_directory(tree)
    with_local_file(tree)
    with_saved_plan(tree)

    done = run_words(tree, subcommand)

    assert done.returncode == 0, everything_printed(done)
    for call in tree.calls():
        assert f"-chdir={tree.module} " in call
        assert f"-chdir={other} " not in call
    assert list(other.iterdir()) == [other / "main.tf"]  # nothing written there


NEAR_MISSES = [
    "aws",  # the managed module is no word on these commands: it is no word at all
    "aws-kubeadm/",
    "aws-kubeadm ",
    " aws-kubeadm",
    "aws_kubeadm",
    "AWS-KUBEADM",
    "Aws-Kubeadm",
    "aws-kubeadm-",
    "aws-kubeadm*",
    "aws-*",
    "aws-kube*",
    "../aws-kubeadm",
    "infra/terraform/aws-kubeadm",
    "./aws-kubeadm",
    "/nonexistent",
    "kubeadm",
    "gcp-kubeadm/",
    "gcp-kubeadm ",
    "gcp_kubeadm",
    "GCP-KUBEADM",
    "gcp-kubeadm-",
    "gcp-kubeadm*",
    "gcp-*",
    "../gcp-kubeadm",
    "infra/terraform/gcp-kubeadm",
    "azure",
    "foundation",
    "",
    ".",
    "..",
    "-chdir=/nonexistent",
    "aws-kubeadm\n",
]


@only_the_managed_module
@pytest.mark.parametrize("subcommand", REFUSING)
@pytest.mark.parametrize("word", NEAR_MISSES)
def test_a_word_that_is_not_exactly_aws_kubeadm_is_refused_before_a_program_runs(
    tree: Tree, subcommand: str, word: str
) -> None:
    with_a_gcp_directory(tree)
    with_an_aws_kubeadm_directory(tree)
    with_local_file(tree)
    with_saved_plan(tree)

    done = run_words(tree, subcommand, word)

    assert done.returncode == 2, everything_printed(done)
    assert "usage:" in done.stderr
    assert tree.calls() == []
    assert tree.aws_calls() == []


@only_the_managed_module
@pytest.mark.parametrize("subcommand", REFUSING)
@pytest.mark.parametrize(
    "words",
    [
        ["aws-kubeadm", "aws-kubeadm"],
        ["aws-kubeadm", "gcp"],
        ["gcp", "x"],
        ["gcp-kubeadm", "x"],
        ["aws-kubeadm", "gcp-kubeadm"],
    ],
)
def test_a_second_word_is_refused_before_a_program_runs(
    tree: Tree, subcommand: str, words: list[str]
) -> None:
    with_an_aws_kubeadm_directory(tree)
    with_local_file(tree)
    with_saved_plan(tree)

    done = run_words(tree, subcommand, *words)

    assert done.returncode == 2, everything_printed(done)
    assert tree.calls() == []
    assert tree.aws_calls() == []


@only_the_managed_module
@pytest.mark.parametrize("subcommand", REFUSING)
@pytest.mark.parametrize("word", ["gcp", "gcp-kubeadm"])
def test_a_google_cloud_word_is_refused_by_a_sentence_that_says_why_before_a_run(
    tree: Tree, subcommand: str, word: str
) -> None:
    with_a_gcp_directory(tree)
    with_a_gcp_kubeadm_directory(tree)
    with_local_file(tree)
    with_saved_plan(tree)

    done = run_words(tree, subcommand, word)

    assert done.returncode == 2, everything_printed(done)
    printed = everything_printed(done)
    assert "Google Cloud" in printed
    assert "scaffold" in printed
    assert "never planned, applied or removed" in printed
    assert "validate" in printed  # the one command that takes the words
    assert "gcp-kubeadm" in printed
    assert tree.calls() == []
    assert tree.aws_calls() == []
    assert (tree.module / tree.plan_file).exists()  # nothing was dropped either


@only_the_managed_module
def test_the_usage_line_names_the_modules_each_command_takes_and_keeps_its_old_form(
    tree: Tree,
) -> None:
    done = run_words(tree)

    assert done.returncode == 2
    assert "validate|plan|apply|destroy" in done.stderr
    assert "validate [aws|gcp|aws-kubeadm|gcp-kubeadm]" in done.stderr
    assert "<plan|apply|destroy> [aws-kubeadm]" in done.stderr
    assert tree.calls() == []


def test_the_scripts_header_says_which_commands_take_which_module_name() -> None:
    header = (TERRAFORM_DIR / "aws.sh").read_text(encoding="utf-8").split("set +x")[0]

    assert "validate gcp" in header
    assert "validate aws-kubeadm" in header
    assert "validate gcp-kubeadm" in header
    assert "plan aws-kubeadm" in header
    assert "apply aws-kubeadm" in header
    assert "destroy aws-kubeadm" in header
    assert "never planned" in header


@only_the_managed_module
@pytest.mark.parametrize(
    "words",
    [
        ["validate"],
        ["validate", "gcp"],
        ["validate", "aws-kubeadm"],
        ["validate", "gcp-kubeadm"],
    ],
)
def test_validate_runs_terraform_with_no_credential_name_of_any_cloud(
    tree: Tree, words: list[str]
) -> None:
    with_a_gcp_directory(tree)
    with_an_aws_kubeadm_directory(tree)
    with_a_gcp_kubeadm_directory(tree)

    done = run_words(
        tree,
        *words,
        **GOOGLE_CREDENTIALS,
        **CREDENTIALS,
        AWS_REGION="eu-central-1",
        AZURE_CLIENT_ID="00000000-0000-0000-0000-000000000000",
    )

    assert done.returncode == 0, everything_printed(done)
    names = env_names(tree.terraform_env())
    assert {n for n in names if n.startswith(CREDENTIAL_PREFIXES)} == set()
    assert names - SHELL_ADDS <= BASE_NAMES
    assert "PATH" in names  # the run did reach the stub, and it saw a path


@pytest.mark.parametrize("subcommand", REFUSING)
def test_a_missing_local_file_is_a_refusal_that_names_the_file(
    tree: Tree, subcommand: str
) -> None:
    done = run_refusing(tree, subcommand)

    assert_refused(tree, done, f"no {tree.local_file}", subcommand)
    assert "README" in everything_printed(done)
    assert tree.aws_calls() == []


@pytest.mark.parametrize("subcommand", REFUSING)
@pytest.mark.parametrize(
    "key",
    [
        "MERIDIAN_AWS_ACCOUNT_ID",
        "MERIDIAN_AWS_REGION",
        "MERIDIAN_AWS_ENDPOINT_CIDR",
        "MERIDIAN_AWS_BUDGET_EMAIL",
    ],
)
def test_a_missing_value_in_the_local_file_is_a_refusal_that_names_it(
    tree: Tree, subcommand: str, key: str
) -> None:
    kept = [line for line in LOCAL_FILE.splitlines() if not line.startswith(key)]
    with_local_file(tree, "\n".join(kept) + "\n")

    done = run_refusing(tree, subcommand)

    assert_refused(tree, done, f"{key} is not set in", subcommand)
    assert tree.aws_calls() == []


@pytest.mark.parametrize("subcommand", REFUSING)
@pytest.mark.parametrize("account", ["12345", "11111111111a", "1111111111111", ""])
def test_an_account_that_is_not_twelve_digits_is_a_refusal(
    tree: Tree, subcommand: str, account: str
) -> None:
    text = LOCAL_FILE.replace(f"ACCOUNT_ID={PINNED}", f"ACCOUNT_ID={account}")
    with_local_file(tree, text)

    done = run_refusing(tree, subcommand)

    assert done.returncode == 1
    assert "MERIDIAN_AWS_ACCOUNT_ID" in everything_printed(done)
    assert tree.terraform_calls(subcommand) == []
    assert tree.aws_calls() == []
    assert_no_identifier_printed(done)


@pytest.mark.parametrize("subcommand", REFUSING)
def test_a_failed_identity_call_is_a_refusal_that_says_to_sign_in(
    tree: Tree, subcommand: str
) -> None:
    with_local_file(tree)

    done = run_refusing(tree, subcommand, STUB_AWS_STATUS="255")

    assert_refused(tree, done, "cannot read the caller's AWS account", subcommand)
    assert "sign in" in everything_printed(done)
    assert len(tree.aws_calls()) == 1


@pytest.mark.parametrize("subcommand", REFUSING)
def test_another_account_is_a_refusal_that_prints_neither_number(
    tree: Tree, subcommand: str
) -> None:
    with_local_file(tree)

    done = run_refusing(tree, subcommand, STUB_ACCOUNT=OTHER)

    assert_refused(tree, done, "not the one pinned in", subcommand)
    assert str(tree.local_file) in everything_printed(done)


@pytest.mark.parametrize("subcommand", REFUSING)
def test_the_pinned_account_passes_and_the_identity_is_asked_once(
    tree: Tree, subcommand: str
) -> None:
    with_local_file(tree)

    done = run_refusing(tree, subcommand)

    assert done.returncode == 0, everything_printed(done)
    (call,) = tree.aws_calls()
    assert call.startswith("aws sts get-caller-identity")
    assert tree.terraform_calls(subcommand) != []
    assert_no_identifier_printed(done)


# ── the local file is read, not run ──────────────────────────────────────────

# Each of these is something the old script, which sourced the file, would have
# run or exported. The marker name is the one the file would create or print.
HOSTILE_LINES = [
    "touch ran-from-the-local-file",
    "MERIDIAN_AWS_REGION=eu-central-1; touch ran-from-the-local-file",
    "MERIDIAN_AWS_REGION=$(touch ran-from-the-local-file)",
    "MERIDIAN_AWS_REGION=`touch ran-from-the-local-file`",
    "MERIDIAN_AWS_REGION='ran-from-the-local-file'",
    "export TF_CLI_ARGS_plan=-ran-from-the-local-file",
    "TF_CLI_ARGS_plan=-ran-from-the-local-file",
    "TF_CLI_ARGS=-ran-from-the-local-file",
    "TF_LOG=ran-from-the-local-file",
    "AWS_ENDPOINT_URL=ran-from-the-local-file",
    "MERIDIAN_AWS_OTHER=ran-from-the-local-file",
    "MERIDIAN_AWS_REGION =ran-from-the-local-file",
    " MERIDIAN_AWS_REGION=ran-from-the-local-file",
    "2222",
]


@pytest.mark.parametrize("subcommand", REFUSING)
@pytest.mark.parametrize("line", HOSTILE_LINES)
def test_a_line_that_is_not_a_known_key_is_refused_by_its_number_and_runs_nothing(
    tree: Tree, subcommand: str, line: str
) -> None:
    with_local_file(tree, "\n".join([*LOCAL_LINES, line, ""]))

    done = run_refusing(tree, subcommand)

    printed = everything_printed(done)
    assert done.returncode == 1, printed
    assert "line 5" in printed
    assert "ran-from-the-local-file" not in printed
    # The sentence names the file, whose path holds the test's own number.
    assert "2222" not in printed.replace(str(tree.local_file), "")
    assert not (tree.root / "ran-from-the-local-file").exists()
    assert tree.calls() == []
    assert tree.aws_calls() == []


def test_the_refusal_names_the_number_of_the_line_it_is_on(tree: Tree) -> None:
    text = "\n".join(
        [LOCAL_LINES[0], "", LOCAL_LINES[1], "x y z", *LOCAL_LINES[2:], ""]
    )
    with_local_file(tree, text)

    done = run_refusing(tree, "plan")

    assert done.returncode == 1
    assert "line 4" in done.stderr


def test_a_windows_line_ending_is_refused_by_its_number(tree: Tree) -> None:
    with_local_file(tree, LOCAL_FILE.replace("\n", "\r\n"))

    done = run_refusing(tree, "plan")

    assert done.returncode == 1
    assert "line 1" in done.stderr
    assert tree.calls() == []


def test_a_key_that_appears_twice_is_refused_by_the_line_of_the_second(
    tree: Tree,
) -> None:
    with_local_file(tree, "\n".join([*LOCAL_LINES, LOCAL_LINES[1], ""]))

    done = run_refusing(tree, "plan")

    assert done.returncode == 1
    assert "line 5" in done.stderr


def test_blank_lines_and_comments_in_the_local_file_are_accepted(tree: Tree) -> None:
    text = "\n".join(["# the four values", "", *LOCAL_LINES, "", "# end", ""])
    with_local_file(tree, text)

    done = tree.run("plan")

    assert done.returncode == 0, everything_printed(done)


def regions_the_module_allows(tree: Tree) -> list[str]:
    """The list in the module's validation of ``region``, read from the file."""
    block = variable_block(tree.variables_file, "region")
    (listed,) = re.findall(r"contains\(\[([^\]]*)\], var\.region\)", block)
    return re.findall(r'"([^"]+)"', listed)


def regions_the_script_allows() -> list[str]:
    text = (TERRAFORM_DIR / "aws.sh").read_text(encoding="utf-8")
    (listed,) = re.findall(r"^readonly ALLOWED_REGIONS=\(([^)]*)\)$", text, re.M)
    return listed.split()


def with_value(tree: Tree, key: str, value: str) -> Tree:
    lines = [
        f"{key}={value}" if line.startswith(f"{key}=") else line for line in LOCAL_LINES
    ]
    return with_local_file(tree, "\n".join([*lines, ""]))


def test_the_regions_the_script_accepts_are_the_six_each_module_accepts(
    tree: Tree,
) -> None:
    allowed = regions_the_module_allows(tree)

    assert len(allowed) == 6  # the reader found them
    assert sorted(regions_the_script_allows()) == sorted(allowed)


def test_each_region_the_module_allows_goes_to_the_cli_and_terraform(
    tree: Tree,
) -> None:
    allowed = regions_the_module_allows(tree)

    for region in allowed:
        with_value(tree, "MERIDIAN_AWS_REGION", region)
        done = tree.run("plan")
        assert done.returncode == 0, (region, everything_printed(done))

    for region in allowed:
        assert f"AWS_REGION={region}\n" in tree.aws_env()
        assert f"TF_VAR_region={region}\n" in tree.terraform_env()


@pytest.mark.parametrize("subcommand", REFUSING)
@pytest.mark.parametrize(
    "region",
    [
        "us-east-1",
        "eu-west-2",  # London: not an EU member state's Region
        "eu-central-2",  # Zurich
        "EU-CENTRAL-1",
        "eu-central-1a",
        "eu-central",
        "../../etc",
        "eu-central-1/../../x",
        "eu-central-1+eu-west-1",
    ],
)
def test_a_region_the_module_does_not_allow_is_refused_before_the_cli_is_called(
    tree: Tree, subcommand: str, region: str
) -> None:
    with_value(tree, "MERIDIAN_AWS_REGION", region)

    done = run_refusing(tree, subcommand)

    assert_refused(tree, done, "MERIDIAN_AWS_REGION", subcommand)
    assert "variables.tf" in everything_printed(done)  # says where the list is
    assert region not in everything_printed(done)
    assert tree.aws_calls() == []


@pytest.mark.parametrize("subcommand", REFUSING)
@pytest.mark.parametrize("key", [line.split("=")[0] for line in LOCAL_LINES])
def test_a_value_of_two_hundred_kilobytes_is_refused_by_its_line_number(
    tree: Tree, subcommand: str, key: str
) -> None:
    with_value(tree, key, "a" * 200_000)
    number = [line.split("=")[0] for line in LOCAL_LINES].index(key) + 1

    done = run_refusing(tree, subcommand)

    printed = everything_printed(done)
    assert done.returncode == 1, printed[:300]
    assert f"line {number}" in printed
    assert "aaaa" not in printed
    assert "Argument list too long" not in printed
    assert tree.calls() == []
    assert tree.aws_calls() == []


@pytest.mark.parametrize(("length", "accepted"), [(253, True), (254, False)])
def test_a_value_is_bounded_at_two_hundred_and_fifty_three_characters(
    tree: Tree, length: int, accepted: bool
) -> None:
    address = "a" * (length - len("@example.com")) + "@example.com"
    with_value(tree, "MERIDIAN_AWS_BUDGET_EMAIL", address)

    done = tree.run("plan")

    if accepted:
        assert done.returncode == 0, everything_printed(done)
    else:
        assert done.returncode == 1
        assert "line 4" in done.stderr
        assert tree.calls() == []


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any file")
@pytest.mark.parametrize("subcommand", REFUSING)
@pytest.mark.parametrize("mode", [0o000, 0o200])
def test_a_local_file_its_owner_cannot_read_is_a_sentence_not_a_shell_error(
    tree: Tree, subcommand: str, mode: int
) -> None:
    with_local_file(tree, mode=mode)

    done = run_refusing(tree, subcommand)

    assert_refused(tree, done, "cannot read", subcommand)
    assert "chmod 600" in everything_printed(done)
    assert "Permission denied" not in everything_printed(done)
    assert tree.aws_calls() == []


@pytest.mark.parametrize("subcommand", REFUSING)
@pytest.mark.parametrize("mode", [0o640, 0o644, 0o666, 0o604, 0o660, 0o606])
def test_a_local_file_that_group_or_others_can_read_or_write_is_refused(
    tree: Tree, subcommand: str, mode: int
) -> None:
    with_local_file(tree, mode=mode)

    done = run_refusing(tree, subcommand)

    assert_refused(tree, done, "mode", subcommand)
    assert "600" in everything_printed(done)
    assert tree.aws_calls() == []


@pytest.mark.parametrize("mode", [0o600, 0o400])
def test_a_local_file_only_its_owner_can_reach_is_accepted(
    tree: Tree, mode: int
) -> None:
    with_local_file(tree, mode=mode)

    done = tree.run("plan")

    assert done.returncode == 0, everything_printed(done)


# ── what Terraform and the aws CLI are given ─────────────────────────────────

HOSTILE_ENVIRONMENT = {
    "TF_LOG": "trace",
    "TF_LOG_PATH": "/nonexistent/a-log",
    "TF_LOG_CORE": "trace",
    "TF_LOG_PROVIDER": "trace",
    "TF_WORKSPACE": "other",
    "TF_DATA_DIR": "/nonexistent/a-data-dir",
    "TF_CLI_CONFIG_FILE": "/nonexistent/a-config",
    "TF_REATTACH_PROVIDERS": "{}",
    "TF_PLUGIN_CACHE_DIR": "/nonexistent/a-cache",
    "TF_INPUT": "0",
    "TF_CLI_ARGS": "-auto-approve",
    "TF_CLI_ARGS_plan": "-refresh=false",
    "TF_CLI_ARGS_init": "-upgrade",
    "TF_VAR_node_instance_type": "p5.48xlarge",
    "TF_VAR_node_count": "5",
    "TF_VAR_budget_email": "someone-else@example.com",
    "AWS_ENDPOINT_URL": "http://127.0.0.1:1",
    "AWS_ENDPOINT_URL_STS": "http://127.0.0.1:1",
    "AWS_DEFAULT_REGION": "us-east-1",
    "AWS_CA_BUNDLE": "/nonexistent/a-ca-bundle",
    "AWS_ROLE_ARN": "not-a-role",
    "AWS_WEB_IDENTITY_TOKEN_FILE": "/nonexistent/a-token",
    "AWS_EC2_METADATA_SERVICE_ENDPOINT": "http://127.0.0.1:1",
    "SHELLOPTS": "xtrace",
    "BASH_ENV": "/nonexistent/a-startup-file",
    "BASH_XTRACEFD": "2",
    "PS4": "trace ",
    "HTTPS_PROXY": "http://127.0.0.1:1",
    "SOMETHING_ELSE": "x",
}
CREDENTIALS = {
    "AWS_ACCESS_KEY_ID": "AKIAIOSFODNN7EXAMPLE",
    "AWS_SECRET_ACCESS_KEY": "not-a-real-secret",
    "AWS_SESSION_TOKEN": "not-a-real-token",
    "AWS_PROFILE": "a-profile",
    "AWS_CONFIG_FILE": "/nonexistent/a-config-file",
    "AWS_SHARED_CREDENTIALS_FILE": "/nonexistent/a-credentials-file",
    "TERM": "xterm",
    "LANG": "C.UTF-8",
}


def run_with_hostile_environment(tree: Tree, subcommand: str = "plan"):
    with_local_file(tree)
    # SHELLOPTS and BASH_ENV would trace or change the script's own shell: leave
    # them for the test of the trace, and give this one the rest.
    hostile = {
        k: v
        for k, v in HOSTILE_ENVIRONMENT.items()
        if k not in {"SHELLOPTS", "BASH_ENV", "BASH_XTRACEFD", "PS4"}
    }
    return tree.run(subcommand, **hostile, **CREDENTIALS)


def test_terraform_sees_only_the_names_the_script_chose(tree: Tree) -> None:
    done = run_with_hostile_environment(tree)

    assert done.returncode == 0, everything_printed(done)
    seen = env_names(tree.terraform_env())
    assert seen - SHELL_ADDS <= BASE_NAMES | AWS_NAMES | SCRIPT_SETS | TF_VAR_NAMES
    assert seen >= {"PATH", "HOME"} | AWS_NAMES | TF_VAR_NAMES | {"AWS_REGION"}


def test_the_aws_cli_sees_only_the_names_the_script_chose_and_no_terraform_variable(
    tree: Tree,
) -> None:
    done = run_with_hostile_environment(tree)

    assert done.returncode == 0, everything_printed(done)
    seen = env_names(tree.aws_env())
    assert seen - SHELL_ADDS <= BASE_NAMES | AWS_NAMES | SCRIPT_SETS
    assert seen >= {"PATH", "HOME"} | AWS_NAMES | {"AWS_REGION"}
    assert {n for n in seen if n.startswith("TF_")} == set()


@pytest.mark.parametrize(
    "name",
    [
        "TF_LOG",
        "TF_LOG_PATH",
        "TF_LOG_CORE",
        "TF_LOG_PROVIDER",
        "TF_WORKSPACE",
        "TF_DATA_DIR",
        "TF_CLI_CONFIG_FILE",
        "TF_REATTACH_PROVIDERS",
        "TF_PLUGIN_CACHE_DIR",
        "TF_INPUT",
        "TF_CLI_ARGS",
        "TF_CLI_ARGS_plan",
        "TF_CLI_ARGS_init",
        "TF_VAR_node_instance_type",
        "TF_VAR_node_count",
        "AWS_ENDPOINT_URL",
        "AWS_ENDPOINT_URL_STS",
        "AWS_DEFAULT_REGION",
        "AWS_CA_BUNDLE",
        "AWS_ROLE_ARN",
        "AWS_WEB_IDENTITY_TOKEN_FILE",
        "AWS_EC2_METADATA_SERVICE_ENDPOINT",
        "HTTPS_PROXY",
        "SOMETHING_ELSE",
    ],
)
def test_a_name_the_script_did_not_choose_never_reaches_either_program(
    tree: Tree, name: str
) -> None:
    run_with_hostile_environment(tree)

    assert name not in env_names(tree.terraform_env())
    assert name not in env_names(tree.aws_env())


def test_the_variables_the_script_exports_win_over_the_callers(tree: Tree) -> None:
    run_with_hostile_environment(tree)

    seen = tree.terraform_env()
    assert f"TF_VAR_budget_email={BUDGET_EMAIL}" in seen
    assert "someone-else@example.com" not in seen
    assert "AWS_REGION=eu-central-1" in seen


@pytest.mark.parametrize("subcommand", ["apply", "destroy"])
def test_apply_and_removal_get_the_same_chosen_environment(
    tree: Tree, subcommand: str
) -> None:
    with_saved_plan(tree)
    with_local_file(tree)
    hostile = {
        k: v
        for k, v in HOSTILE_ENVIRONMENT.items()
        if k not in {"SHELLOPTS", "BASH_ENV", "BASH_XTRACEFD", "PS4"}
    }

    tree.run(subcommand, terminal=subcommand == "destroy", **hostile, **CREDENTIALS)

    seen = env_names(tree.terraform_env())
    assert seen - SHELL_ADDS <= BASE_NAMES | AWS_NAMES | SCRIPT_SETS | TF_VAR_NAMES
    assert {n for n in seen if n.startswith("TF_") and n not in TF_VAR_NAMES} == set()


def test_terraform_never_sees_an_argument_a_caller_put_in_the_environment(
    tree: Tree,
) -> None:
    with_local_file(tree)

    tree.run(
        "plan",
        TF_CLI_ARGS="-auto-approve",
        TF_CLI_ARGS_plan="-destroy",
        TF_CLI_ARGS_init="-upgrade",
    )

    assert "TF_CLI_ARGS" not in tree.terraform_env()


# ── tracing ──────────────────────────────────────────────────────────────────


def run_traced(tree: Tree, how: str) -> tuple[subprocess.CompletedProcess[str], str]:
    """The plan, run so that the shell would trace it: with -x, with SHELLOPTS
    in the environment, and with the trace sent to a file of its own."""
    with_local_file(tree)
    tree.write_stub_env({})
    trace_file = tree.root / "trace.log"
    env = dict(tree.base_env)
    script = str(tree.script)
    words = tree.words("plan")
    if how == "bash -x":
        command = ["bash", "-x", script, *words]
    elif how == "SHELLOPTS":
        env["SHELLOPTS"] = "xtrace"
        command = [script, *words]
    else:
        command = [
            "bash",
            "-c",
            'exec 9>"$1"; BASH_XTRACEFD=9; export BASH_XTRACEFD; '
            'script="$2"; shift 2; exec bash -x "$script" "$@"',
            "trace",
            str(trace_file),
            script,
            *words,
        ]
    done = subprocess.run(
        command,
        capture_output=True,
        text=True,
        env=env,
        cwd=tree.root,
        stdin=subprocess.DEVNULL,
        timeout=60,
        check=False,
    )
    trace = trace_file.read_text() if trace_file.exists() else ""
    return done, trace


@pytest.mark.parametrize("how", ["bash -x", "SHELLOPTS", "BASH_XTRACEFD"])
def test_a_traced_run_prints_none_of_the_four_values(tree: Tree, how: str) -> None:
    done, trace = run_traced(tree, how)

    assert done.returncode == 0, everything_printed(done)
    printed = everything_printed(done) + trace
    for private in (PINNED, ENDPOINT_CIDR, BUDGET_EMAIL, "eu-central-1"):
        assert private not in printed
    # The trace was on when the script started (else this test proves nothing).
    assert "+ set +x" in printed


def test_the_script_switches_tracing_off_before_it_reads_anything() -> None:
    lines = (TERRAFORM_DIR / "aws.sh").read_text(encoding="utf-8").splitlines()
    code = [line for line in lines if line.strip() and not line.startswith("#")]

    assert code[0] == "set +x"
    assert "set -euo pipefail" in code[:3]
    assert any(line.startswith("umask 077") for line in code[:6])


# ── umask ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("subcommand", ["plan", "apply"])
def test_everything_terraform_writes_is_written_under_a_private_umask(
    tree: Tree, subcommand: str
) -> None:
    with_local_file(tree)
    with_saved_plan(tree)

    done = tree.run(subcommand, caller_umask="022")

    assert done.returncode == 0, everything_printed(done)
    umasks = [line for line in tree.stub_lines() if line.startswith("umask=")]
    assert umasks != []
    assert set(umasks) == {"umask=0077"}


def test_the_removal_runs_terraform_under_a_private_umask_too(tree: Tree) -> None:
    with_local_file(tree)
    with_saved_plan(tree)

    done = tree.run("destroy", terminal=True)

    assert done.returncode == 0, done.stdout
    umasks = [line for line in tree.stub_lines() if line.startswith("umask=")]
    assert umasks != []
    assert set(umasks) == {"umask=0077"}


# ── git runs no code from a configuration file ──────────────────────────────


def with_fsmonitor(tree: Tree, where: str) -> Path:
    """A git setting that runs a program on every `git status`, in the caller's
    own configuration ("global": the stand-in home's .gitconfig) or in the
    repository's ("local"). The program makes a file; the file is the proof."""
    marker = tree.root / "fsmonitor-ran"
    hook = tree.root / "fsmonitor-hook.sh"
    hook.write_text(f'#!/bin/sh\ntouch "{marker}"\n')
    hook.chmod(0o755)
    if where == "global":
        (tree.root / ".gitconfig").write_text(f"[core]\n\tfsmonitor = {hook}\n")
    else:
        git(tree.root, "config", "core.fsmonitor", str(hook))
    return marker


@pytest.mark.parametrize("where", ["global", "local"])
@pytest.mark.parametrize("subcommand", ["plan", "apply"])
def test_a_git_setting_that_runs_a_program_does_not_run_it_for_the_script(
    tree: Tree, subcommand: str, where: str
) -> None:
    with_local_file(tree)
    with_saved_plan(tree)
    marker = with_fsmonitor(tree, where)

    done = tree.run(subcommand)

    assert done.returncode == 0, everything_printed(done)
    assert not marker.exists()
    assert tree.terraform_calls(subcommand) != []


def test_the_scripts_git_still_sees_a_changed_file_with_those_settings_off(
    tree: Tree,
) -> None:
    with_local_file(tree)
    with_saved_plan(tree)
    with_fsmonitor(tree, "global")
    (tree.module / "main.tf").write_text("# changed, not committed\n")

    done = tree.run("apply")

    assert_plan_refused_and_dropped(tree, done, "uncommitted")


# ── the git it needs: 2.32, the first that reads GIT_CONFIG_GLOBAL ───────────

# A stand-in `git` that answers `--version` with the line the test gives and
# passes every other call to the real one, so that an accepted version goes on
# to the script's own git calls. The real git's place is written into the stub's
# settings file by the test, from the path the test process itself has.
STUB_GIT = r"""#!/usr/bin/env bash
. "$(dirname "${BASH_SOURCE[0]}")/stub.env"
if [[ "$1" == --version ]]; then
  printf '%s\n' "${STUB_GIT_VERSION_LINE}"
  exit 0
fi
exec "${STUB_REAL_GIT}" "$@"
"""


def with_git_version(tree: Tree, line: str) -> dict[str, str]:
    stub = tree.root / "stubs" / "git"
    stub.write_text(STUB_GIT)
    stub.chmod(0o755)
    real = shutil.which("git")
    assert real is not None
    return {"STUB_GIT_VERSION_LINE": line, "STUB_REAL_GIT": real}


@pytest.mark.parametrize("subcommand", ["plan", "apply"])
@pytest.mark.parametrize(
    ("line", "found"),
    [
        # The sentence names major and minor: a four-part version would be
        # taken for an address by `redact`, which every refusal passes through.
        ("git version 2.31.9", "git 2.31 "),
        ("git version 2.9.5", "git 2.9 "),  # 9 is below 32: not a string comparison
        ("git version 1.8.3.1", "git 1.8 "),
        ("git version 2.08.1", "git 2.08 "),  # base ten: not an octal error
        ("git version 2.31.9 (Apple Git-130)", "git 2.31 "),
    ],
)
def test_a_git_older_than_2_32_is_refused_with_the_version_it_found(
    tree: Tree, subcommand: str, line: str, found: str
) -> None:
    with_local_file(tree)
    with_saved_plan(tree)
    settings = with_git_version(tree, line)

    done = tree.run(subcommand, **settings)

    printed = everything_printed(done)
    assert done.returncode == 1, printed
    assert found in done.stderr
    assert "2.32" in done.stderr
    assert "GIT_CONFIG_GLOBAL" in done.stderr
    assert tree.calls() == []
    assert tree.aws_calls() == []


@pytest.mark.parametrize("subcommand", ["plan", "apply"])
@pytest.mark.parametrize(
    "line",
    [
        "git version 2.32.0",
        "git version 2.32",
        "git version 3.0.0",
        "git version 2.100.0",
        "git version 2.032.0",  # a leading zero is a decimal 32
        "git version 2.43.0.windows.1",
        "git version 2.39.5 (Apple Git-154)",
    ],
)
def test_a_git_of_2_32_or_newer_is_accepted(
    tree: Tree, subcommand: str, line: str
) -> None:
    with_local_file(tree)
    with_saved_plan(tree)
    settings = with_git_version(tree, line)

    done = tree.run(subcommand, **settings)

    assert done.returncode == 0, everything_printed(done)
    assert tree.terraform_calls(subcommand) != []


@pytest.mark.parametrize("subcommand", ["plan", "apply"])
@pytest.mark.parametrize(
    "line",
    ["git version unknown", "git version", "", "2.40.0", "git version .5", "git 2.40"],
)
def test_a_git_version_the_script_cannot_read_is_refused(
    tree: Tree, subcommand: str, line: str
) -> None:
    with_local_file(tree)
    with_saved_plan(tree)
    settings = with_git_version(tree, line)

    done = tree.run(subcommand, **settings)

    assert done.returncode == 1, everything_printed(done)
    assert "version" in done.stderr
    assert "2.32" in done.stderr
    assert tree.calls() == []
    assert tree.aws_calls() == []


@pytest.mark.parametrize("subcommand", ["validate", "destroy"])
def test_validate_and_removal_do_not_need_a_git_at_all(
    tree: Tree, subcommand: str
) -> None:
    """They make no git call, so an old git is no reason to refuse them."""
    with_local_file(tree)
    settings = with_git_version(tree, "git version 2.0.0")

    done = tree.run(subcommand, terminal=subcommand == "destroy", **settings)

    assert done.returncode == 0, everything_printed(done)


# ── nothing in the module's directory may change the plan unseen ─────────────

VARIABLE_FILES = [
    "terraform.tfvars",
    "terraform.tfvars.json",
    "mine.auto.tfvars",
    "mine.auto.tfvars.json",
]
OVERRIDE_FILES = [
    "override.tf",
    "override.tf.json",
    "mine_override.tf",
    "mine_override.tf.json",
]


@pytest.mark.parametrize("subcommand", REFUSING)
@pytest.mark.parametrize(
    ("name", "kind"),
    [(n, "variable file") for n in VARIABLE_FILES]
    + [(n, "override file") for n in OVERRIDE_FILES],
)
def test_a_variable_file_or_an_override_file_in_the_module_is_refused_by_its_kind(
    tree: Tree, subcommand: str, name: str, kind: str
) -> None:
    with_local_file(tree)
    (tree.module / name).write_text("# changes the plan\n")

    done = run_refusing(tree, subcommand)

    assert_refused(tree, done, kind, subcommand)
    assert tree.aws_calls() == []


# The shell skips a name that begins with a dot, and git ignores these files, so
# a loop over `*` never saw them; Terraform loads each of the first four. The
# rest are names a file system that ignores case would hand to Terraform as the
# real ones. "zzmark" is text of the file's own name: no refusal may repeat it.
HIDDEN_AND_CASED_FILES = [
    (".auto.tfvars", "variable file"),
    (".auto.tfvars.json", "variable file"),
    (".zzmark.auto.tfvars", "variable file"),
    (".zzmark.auto.tfvars.json", "variable file"),
    ("Terraform.tfvars", "variable file"),
    ("TERRAFORM.TFVARS.JSON", "variable file"),
    ("ZZMark.Auto.Tfvars", "variable file"),
    (".ZZMARK.AUTO.TFVARS.JSON", "variable file"),
    ("Override.tf", "override file"),
    ("ZZMARK_OVERRIDE.TF.JSON", "override file"),
    (".zzmark_override.tf", "override file"),
]


@pytest.mark.parametrize("subcommand", REFUSING)
@pytest.mark.parametrize(("name", "kind"), HIDDEN_AND_CASED_FILES)
def test_a_hidden_or_differently_cased_file_of_those_kinds_is_refused_by_its_kind(
    tree: Tree, subcommand: str, name: str, kind: str
) -> None:
    with_local_file(tree)
    (tree.module / name).write_text("# changes the plan\n")

    done = run_refusing(tree, subcommand)

    assert_refused(tree, done, kind, subcommand)
    assert "zzmark" not in everything_printed(done).lower()
    assert tree.aws_calls() == []


def test_the_files_the_module_directory_holds_by_design_are_not_refused(
    tree: Tree,
) -> None:
    """The scan sees every name, dot names included, so the real directory's
    own hidden files must not be mistaken for variable files."""
    with_local_file(tree)
    for name in (".terraform.lock.hcl", ".trivyignore", "README.md", tree.plan_file):
        (tree.module / name).write_text("x\n")
    (tree.module / ".terraform").mkdir()

    done = tree.run("plan")

    assert done.returncode == 0, everything_printed(done)


@pytest.mark.parametrize("name", ["other.tfvars", "notes.txt", "my_override_tf.txt"])
def test_a_file_terraform_does_not_load_by_itself_is_not_refused(
    tree: Tree, name: str
) -> None:
    with_local_file(tree)
    (tree.module / name).write_text("x\n")

    done = tree.run("plan")

    assert done.returncode == 0, everything_printed(done)


@pytest.mark.parametrize(
    "name",
    [
        "override.tf",
        "x_override.tf",
        "override.tf.json",
        "x.tfvars.json",
        ".terraformrc",
        "terraform.rc",
        "tf.log",
        "@plan_file@",
        "@meta_file@",
    ],
)
def test_the_repository_ignores_the_files_that_could_hold_what_must_not_be_committed(
    name: str, module: Module
) -> None:
    name = name.replace("@plan_file@", module.plan_file)
    name = name.replace("@meta_file@", module.meta_file)

    done = subprocess.run(
        ["git", "check-ignore", "-q", f"infra/terraform/{module.directory}/{name}"],
        cwd=REPO_ROOT,
        env=GIT_ENV,
        check=False,
    )

    assert done.returncode == 0, f"{name} is not ignored by .gitignore"


# ── colour ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("subcommand", ["validate", "plan", "apply", "destroy"])
def test_every_terraform_call_is_made_without_colour(
    tree: Tree, subcommand: str
) -> None:
    with_local_file(tree)
    with_saved_plan(tree)

    done = tree.run(subcommand, terminal=subcommand == "destroy")

    assert done.returncode == 0, everything_printed(done)
    assert tree.calls() != []
    for call in tree.calls():
        assert " -no-color" in call, call
