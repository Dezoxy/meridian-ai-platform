"""The commands around the AWS module: ``infra/terraform/aws.sh`` (S036).

The script is copied, with ``common.sh``, into a temporary ``infra/terraform``
tree that holds a stand-in module directory, so it runs against nothing real:
its module directory and its local file are resolved from its own location.
Stub ``terraform`` and ``aws`` programs come first on ``PATH``; they record
every call and answer from variables the test sets. No credential, account,
cluster or network is involved. Every account number, address and host here is
made up: twelve identical digits, a documentation address and ``example.com``.

The stub ``terraform`` prints a line with an ARN and an account number on every
call, so a test that finds neither in the script's output shows ``redact`` at
work on what Terraform says.
"""

import os
import pty
import re
import select
import shutil
import stat
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import pytest
from servicesupport import REPO_ROOT

TERRAFORM_DIR = REPO_ROOT / "infra" / "terraform"
PINNED = "111111111111"
OTHER = "222222222222"
NOISE = "333333333333"  # the account in every line the stub terraform prints
ENDPOINT_CIDR = "203.0.113.7/32"
BUDGET_EMAIL = "owner@example.com"
LOCAL_FILE = "\n".join(
    [
        f"MERIDIAN_AWS_ACCOUNT_ID={PINNED}",
        "MERIDIAN_AWS_REGION=eu-central-1",
        f"MERIDIAN_AWS_ENDPOINT_CIDR={ENDPOINT_CIDR}",
        f"MERIDIAN_AWS_BUDGET_EMAIL={BUDGET_EMAIL}",
        "",
    ]
)
PLAN_FILE = "aws.tfplan"
QUESTION = "Only 'yes' will be accepted to confirm."

STUB_TERRAFORM = r"""#!/usr/bin/env bash
printf '%s\n' "terraform $*" >>"${STUB_LOG}"
{ env | grep -E '^(TF_|AWS_)' | sort; echo "--"; } >>"${STUB_ENV_LOG}"
chdir=""
args=()
for argument in "$@"; do
  case "${argument}" in
    -chdir=*) chdir="${argument#-chdir=}" ;;
    *) args+=("${argument}") ;;
  esac
done
echo "stub terraform ${args[0]}: arn:aws:iam::333333333333:role/stub"
case "${args[0]}" in
  fmt)
    [[ "${STUB_FMT_STATUS:-0}" == 0 ]] || echo "main.tf"
    exit "${STUB_FMT_STATUS:-0}" ;;
  plan)
    for argument in "${args[@]}"; do
      case "${argument}" in -out=*) : >"${chdir}/${argument#-out=}" ;; esac
    done
    exit "${STUB_PLAN_STATUS:-0}" ;;
  apply) exit "${STUB_APPLY_STATUS:-0}" ;;
  destroy)
    if [[ -t 0 ]]; then
      echo "tty_stdin=yes" >>"${STUB_LOG}"
    else
      echo "tty_stdin=no" >>"${STUB_LOG}"
    fi
    printf '%s\n' "Do you really want to destroy all resources?" \
      "  Terraform will destroy all your managed infrastructure, as shown above." \
      "  There is no undo. Only 'yes' will be accepted to confirm." ""
    printf '  Enter a value: '
    read -r answer
    echo "answer=${answer}" >>"${STUB_LOG}"
    exit 0 ;;
  *) exit 0 ;;
esac
"""

STUB_AWS = r"""#!/usr/bin/env bash
printf '%s\n' "aws $*" >>"${STUB_AWS_LOG}"
if [[ "${STUB_AWS_STATUS:-0}" != 0 ]]; then
  echo "An error occurred (ExpiredToken) for account 111111111111" >&2
  exit "${STUB_AWS_STATUS}"
fi
echo "${STUB_ACCOUNT}"
"""


@dataclass
class Tree:
    root: Path
    base_env: dict[str, str]

    @property
    def script(self) -> Path:
        return self.root / "infra" / "terraform" / "aws.sh"

    @property
    def module(self) -> Path:
        return self.root / "infra" / "terraform" / "aws"

    @property
    def local_file(self) -> Path:
        return self.root / "infra" / "terraform" / "local.env-aws"

    def calls(self) -> list[str]:
        log = self.root / "stub.log"
        return log.read_text().splitlines() if log.exists() else []

    def aws_calls(self) -> list[str]:
        log = self.root / "stub-aws.log"
        return log.read_text().splitlines() if log.exists() else []

    def terraform_env(self) -> str:
        log = self.root / "stub-env.log"
        return log.read_text() if log.exists() else ""

    def terraform_calls(self, subcommand: str) -> list[str]:
        return [c for c in self.calls() if f" {subcommand}" in c and "terraform" in c]

    def run(
        self,
        subcommand: str,
        *,
        terminal: bool = False,
        answer: str = "yes\n",
        **env: str,
    ) -> subprocess.CompletedProcess[str]:
        full = {**self.base_env, **env}
        command = [str(self.script), *([subcommand] if subcommand else [])]
        if not terminal:
            return subprocess.run(
                command,
                capture_output=True,
                text=True,
                env=full,
                cwd=self.root,
                stdin=subprocess.DEVNULL,
                timeout=60,
                check=False,
            )
        return run_in_a_terminal(command, full, self.root, answer)


def run_in_a_terminal(
    command: list[str], env: dict[str, str], cwd: Path, answer: str
) -> subprocess.CompletedProcess[str]:
    """Run with a pseudo-terminal as standard input, output and error. The
    answer is typed once the question has reached the terminal, so a question
    that never arrives fails the test by timeout instead of passing."""
    master, slave = pty.openpty()
    process = subprocess.Popen(
        command, stdin=slave, stdout=slave, stderr=slave, env=env, cwd=cwd
    )
    os.close(slave)
    output = b""
    answered = False
    deadline = time.monotonic() + 30
    try:
        while time.monotonic() < deadline:
            ready, _, _ = select.select([master], [], [], 0.2)
            if not ready:
                if process.poll() is not None:
                    break
                continue
            try:
                chunk = os.read(master, 4096)
            except OSError:
                break
            if not chunk:
                break
            output += chunk
            if not answered and QUESTION in output.decode(errors="replace"):
                os.write(master, answer.encode())
                answered = True
        else:
            process.kill()
            process.wait()
            pytest.fail(f"no end after 30 s; output so far: {output!r}")
    finally:
        os.close(master)
    status = process.wait(timeout=10)
    return subprocess.CompletedProcess(
        command, status, output.decode(errors="replace"), ""
    )


@pytest.fixture
def tree(tmp_path: Path) -> Tree:
    terraform_dir = tmp_path / "infra" / "terraform"
    (terraform_dir / "aws").mkdir(parents=True)
    for name in ("aws.sh", "common.sh"):
        shutil.copy2(TERRAFORM_DIR / name, terraform_dir / name)
    (terraform_dir / "aws" / "main.tf").write_text("# a stand-in module\n")
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    for name, text in (("terraform", STUB_TERRAFORM), ("aws", STUB_AWS)):
        (stubs / name).write_text(text)
        (stubs / name).chmod(0o755)
    env = {
        "PATH": f"{stubs}:{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "STUB_LOG": str(tmp_path / "stub.log"),
        "STUB_AWS_LOG": str(tmp_path / "stub-aws.log"),
        "STUB_ENV_LOG": str(tmp_path / "stub-env.log"),
        "STUB_ACCOUNT": PINNED,
    }
    return Tree(tmp_path, env)


def with_local_file(tree: Tree, text: str = LOCAL_FILE) -> Tree:
    tree.local_file.write_text(text)
    return tree


def with_saved_plan(tree: Tree) -> Tree:
    (tree.module / PLAN_FILE).write_text("plan")
    return tree


def everything_printed(done: subprocess.CompletedProcess[str]) -> str:
    return done.stdout + done.stderr


def assert_no_identifier_printed(done: subprocess.CompletedProcess[str]) -> None:
    printed = everything_printed(done)
    for private in (PINNED, OTHER, NOISE, ENDPOINT_CIDR, BUDGET_EMAIL):
        assert private not in printed


# ── usage ────────────────────────────────────────────────────────────────────


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
    subcommands = [call.split()[2] for call in tree.calls()]
    assert subcommands == ["fmt", "init", "validate"]
    fmt, init, _ = tree.calls()
    assert f"-chdir={tree.module}" in fmt
    assert "-check" in fmt
    assert "-backend=false" in init
    assert "-input=false" in init


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
    assert [call.split()[2] for call in tree.calls()] == ["fmt"]


def test_what_terraform_prints_passes_through_redact(tree: Tree) -> None:
    done = tree.run("validate")

    assert "stub terraform validate: <arn>" in done.stdout
    assert NOISE not in everything_printed(done)


# ── the pin: what plan, apply and removal refuse ─────────────────────────────

REFUSING = ("plan", "apply", "destroy")


def run_refusing(tree: Tree, subcommand: str, **env: str):
    """The sub-command with a saved plan in place and, for the removal, a
    terminal, so that the refusal under test is the one that is reached."""
    with_saved_plan(tree)
    return tree.run(subcommand, terminal=subcommand == "destroy", **env)


def assert_refused(tree: Tree, done, sentence: str, subcommand: str) -> None:
    assert done.returncode == 1, everything_printed(done)
    assert sentence in everything_printed(done)
    assert tree.terraform_calls(subcommand) == []
    assert_no_identifier_printed(done)


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


# ── plan ─────────────────────────────────────────────────────────────────────


def test_plan_initialises_then_plans_into_a_saved_file_only_the_owner_can_read(
    tree: Tree,
) -> None:
    with_local_file(tree)

    done = tree.run("plan")

    assert done.returncode == 0, everything_printed(done)
    subcommands = [call.split()[2] for call in tree.calls()]
    assert subcommands == ["init", "plan"]
    plan_call = tree.terraform_calls("plan")[0]
    assert f"-out={PLAN_FILE}" in plan_call
    assert "-input=false" in plan_call
    mode = stat.S_IMODE((tree.module / PLAN_FILE).stat().st_mode)
    assert mode & 0o077 == 0


def test_plan_hands_terraform_the_region_the_address_and_the_email_not_the_account(
    tree: Tree,
) -> None:
    with_local_file(tree)

    tree.run("plan", AWS_DEFAULT_REGION="us-east-1")

    seen = tree.terraform_env()
    assert "TF_VAR_region=eu-central-1" in seen
    assert f"TF_VAR_api_access_cidr={ENDPOINT_CIDR}" in seen
    assert f"TF_VAR_budget_email={BUDGET_EMAIL}" in seen
    assert "AWS_REGION=eu-central-1" in seen
    assert "AWS_DEFAULT_REGION" not in seen
    assert PINNED not in seen


def test_plan_prints_none_of_the_four_values_nor_what_terraform_would_leak(
    tree: Tree,
) -> None:
    with_local_file(tree)

    done = tree.run("plan")

    assert_no_identifier_printed(done)


def test_a_failed_plan_fails_the_command(tree: Tree) -> None:
    with_local_file(tree)

    done = tree.run("plan", STUB_PLAN_STATUS="1")

    assert done.returncode == 1


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


# ── apply ────────────────────────────────────────────────────────────────────


def test_apply_without_a_saved_plan_refuses_before_any_call(tree: Tree) -> None:
    with_local_file(tree)

    done = tree.run("apply")

    assert done.returncode == 1
    assert "make aws-plan" in done.stderr
    assert tree.calls() == []
    assert tree.aws_calls() == []


def test_apply_applies_exactly_the_saved_plan_and_removes_it(tree: Tree) -> None:
    with_local_file(tree)
    with_saved_plan(tree)

    done = tree.run("apply")

    assert done.returncode == 0, everything_printed(done)
    (call,) = tree.terraform_calls("apply")
    assert call.split()[-1] == PLAN_FILE
    assert "-auto-approve" not in call
    assert not (tree.module / PLAN_FILE).exists()
    assert_no_identifier_printed(done)


def test_apply_removes_the_plan_when_it_fails_and_says_to_plan_again(
    tree: Tree,
) -> None:
    with_local_file(tree)
    with_saved_plan(tree)

    done = tree.run("apply", STUB_APPLY_STATUS="1")

    assert done.returncode == 1
    assert "make aws-plan" in done.stderr
    assert not (tree.module / PLAN_FILE).exists()


def test_apply_to_another_account_leaves_the_saved_plan_for_the_right_one(
    tree: Tree,
) -> None:
    with_local_file(tree)
    with_saved_plan(tree)

    done = tree.run("apply", STUB_ACCOUNT=OTHER)

    assert done.returncode == 1
    assert (tree.module / PLAN_FILE).exists()


# ── removal ──────────────────────────────────────────────────────────────────


def test_removal_without_a_terminal_refuses_before_reading_anything(
    tree: Tree,
) -> None:
    with_local_file(tree)

    done = tree.run("destroy")

    assert done.returncode == 1
    assert "needs a terminal" in done.stderr
    assert tree.calls() == []
    assert tree.aws_calls() == []


def test_no_environment_variable_lets_a_removal_run_without_a_terminal(
    tree: Tree,
) -> None:
    with_local_file(tree)

    done = tree.run(
        "destroy",
        TF_CLI_ARGS_destroy="-auto-approve",
        TF_CLI_ARGS="-auto-approve",
        TF_INPUT="0",
        CI="true",
    )

    assert done.returncode == 1
    assert "needs a terminal" in done.stderr
    assert tree.calls() == []


def test_removal_with_a_terminal_lets_terraform_ask_its_own_question(
    tree: Tree,
) -> None:
    with_local_file(tree)

    done = tree.run(
        "destroy",
        terminal=True,
        TF_CLI_ARGS_destroy="-auto-approve",
        TF_CLI_ARGS="-auto-approve",
    )

    assert done.returncode == 0, done.stdout
    # The question reached the terminal (the test types its answer only then),
    # and what was typed reached Terraform on a standard input that is one.
    assert QUESTION in done.stdout
    calls = tree.calls()
    assert "tty_stdin=yes" in calls
    assert "answer=yes" in calls
    (call,) = tree.terraform_calls("destroy")
    assert "-auto-approve" not in call
    assert "-input=false" not in call
    assert "TF_CLI_ARGS" not in tree.terraform_env()
    assert_no_identifier_printed(done)


def test_removal_with_a_terminal_still_checks_the_account_first(tree: Tree) -> None:
    with_local_file(tree)

    done = tree.run("destroy", terminal=True, STUB_ACCOUNT=OTHER)

    assert done.returncode == 1
    assert "not the one pinned in" in done.stdout
    assert tree.terraform_calls("destroy") == []


# ── the script and the module agree on the variables' names ─────────────────

MODULE_VARIABLES = TERRAFORM_DIR / "aws" / "variables.tf"


def variables_without_a_default() -> set[str]:
    """The module's variables that have no ``default`` line of their own: the
    ones a plan stops to ask for. Each ``variable`` block is read on its own, so
    a description that says "no default" or a comment about a default minor
    version is not mistaken for one."""
    text = MODULE_VARIABLES.read_text(encoding="utf-8")
    declared = re.split(r'^variable "(\w+)" \{$', text, flags=re.MULTILINE)
    blocks = dict(zip(declared[1::2], declared[2::2], strict=True))
    return {
        name
        for name, body in blocks.items()
        if not re.search(r"^  default\s*=", body, re.MULTILINE)
    }


def variables_declared() -> set[str]:
    text = MODULE_VARIABLES.read_text(encoding="utf-8")
    return set(re.findall(r'^variable "(\w+)" \{$', text, flags=re.MULTILINE))


def variables_the_script_exports() -> set[str]:
    text = (TERRAFORM_DIR / "aws.sh").read_text(encoding="utf-8")
    return set(re.findall(r"^\s*export TF_VAR_(\w+)=", text, flags=re.MULTILINE))


def test_the_script_exports_every_module_variable_that_has_no_default() -> None:
    needed = variables_without_a_default()

    assert needed >= {"api_access_cidr", "budget_email"}  # the reader found them
    assert needed - variables_the_script_exports() == set()


def test_the_script_exports_no_name_the_module_does_not_declare() -> None:
    exported = variables_the_script_exports()

    assert exported - variables_declared() == set()
    assert exported >= {"region", "budget_email"}  # the reader found them
