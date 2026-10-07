"""The commands around the AWS module: ``infra/terraform/aws.sh`` (S036).

The script is copied, with ``common.sh``, into a temporary ``infra/terraform``
tree that holds a stand-in module directory and is a git repository of its own,
so it runs against nothing real: its module directory and its local file are
resolved from its own location. Stub ``terraform`` and ``aws`` programs come
first on ``PATH``; they record every call and answer from a file the test
writes beside them (``stub.env``): the script runs both with an environment of
its own choosing, so a variable the test sets in the process would never reach
them. No credential, account, cluster or network is involved. Every account
number, address and host here is made up: twelve identical digits, a
documentation address and ``example.com``.

The stub ``terraform`` prints a line with an ARN and an account number on every
call, so a test that finds neither in the script's output shows ``redact`` at
work on what Terraform says.
"""

import hashlib
import os
import pty
import re
import select
import shlex
import shutil
import stat
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from servicesupport import REPO_ROOT
from terraformsupport import needs_terraform

TERRAFORM_DIR = REPO_ROOT / "infra" / "terraform"
PINNED = "111111111111"
OTHER = "222222222222"
NOISE = "333333333333"  # the account in every line the stub terraform prints
ENDPOINT_CIDR = "203.0.113.7/32"
BUDGET_EMAIL = "owner@example.com"
LOCAL_LINES = [
    f"MERIDIAN_AWS_ACCOUNT_ID={PINNED}",
    "MERIDIAN_AWS_REGION=eu-central-1",
    f"MERIDIAN_AWS_ENDPOINT_CIDR={ENDPOINT_CIDR}",
    f"MERIDIAN_AWS_BUDGET_EMAIL={BUDGET_EMAIL}",
]
LOCAL_FILE = "\n".join([*LOCAL_LINES, ""])
PLAN_FILE = "aws.tfplan"
META_FILE = "aws.tfplan.meta"
QUESTION = "Only 'yes' will be accepted to confirm."
STATE_DIRECTORY = ".local/state/meridian-aws"  # under the caller's home
PLAN_MAX_AGE_SECONDS = 30 * 60

STUB_TERRAFORM = r"""#!/usr/bin/env bash
here="$(dirname "${BASH_SOURCE[0]}")"
. "${here}/stub.env"
printf '%s\n' "terraform $*" >>"${STUB_LOG}"
echo "umask=$(umask)" >>"${STUB_LOG}"
{ env | sort; echo "--"; } >>"${STUB_ENV_LOG}"
chdir=""
args=()
for argument in "$@"; do
  case "${argument}" in
    -chdir=*) chdir="${argument#-chdir=}" ;;
    *) args+=("${argument}") ;;
  esac
done
if [[ "${args[0]}" != state ]]; then
  echo "stub terraform ${args[0]}: arn:aws:iam::333333333333:role/stub"
fi
case "${args[0]}" in
  fmt)
    [[ "${STUB_FMT_STATUS:-0}" == 0 ]] || echo "main.tf"
    exit "${STUB_FMT_STATUS:-0}" ;;
  plan)
    for argument in "${args[@]}"; do
      case "${argument}" in -out=*) : >"${chdir}/${argument#-out=}" ;; esac
    done
    # A change to the module while the plan runs (an editor, a second shell).
    if [[ "${STUB_PLAN_CHANGES_THE_MODULE:-0}" == 1 ]]; then
      echo "# changed during the plan" >>"${chdir}/main.tf"
    fi
    exit "${STUB_PLAN_STATUS:-0}" ;;
  apply) exit "${STUB_APPLY_STATUS:-0}" ;;
  state)
    if [[ "${STUB_STATE_STATUS:-0}" != 0 ]]; then
      echo "Error: Failed to load state: access denied" >&2
      exit "${STUB_STATE_STATUS}"
    fi
    if [[ "${STUB_STATE_MISSING:-0}" == 1 ]]; then
      printf '%s\n' "No state file was found!" "" \
        "State management commands require a state file." >&2
      exit 1
    fi
    [[ -e "${here}/removed" ]] && exit 0
    count="${STUB_STATE_COUNT:-3}"
    for ((n = 1; n <= count; n++)); do echo "aws_stub.resource_${n}"; done
    [[ "${STUB_STATE_DATA:-0}" != 1 ]] || echo "data.aws_stub.caller"
    exit 0 ;;
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
    keep="${STUB_REMOVAL_KEEPS_STATE:-0}"
    [[ "${STUB_DESTROY_STATUS:-0}" == 0 ]] || keep=1
    [[ "${keep}" == 1 ]] || : >"${here}/removed"
    exit "${STUB_DESTROY_STATUS:-0}" ;;
  *) exit 0 ;;
esac
"""

STUB_AWS = r"""#!/usr/bin/env bash
. "$(dirname "${BASH_SOURCE[0]}")/stub.env"
printf '%s\n' "aws $*" >>"${STUB_AWS_LOG}"
{ env | sort; echo "--"; } >>"${STUB_AWS_ENV_LOG}"
if [[ "${STUB_AWS_STATUS:-0}" != 0 ]]; then
  echo "An error occurred (ExpiredToken) for account 111111111111" >&2
  exit "${STUB_AWS_STATUS}"
fi
echo "${STUB_ACCOUNT}"
"""

# What the script lets through to Terraform and to the aws CLI (aws.sh,
# ENV_BASE and ENV_AWS), written out here once more so that a name added to the
# script's list has to be added here too, on purpose.
BASE_NAMES = {
    "PATH",
    "HOME",
    "TMPDIR",
    "TERM",
    "LANG",
    "LANGUAGE",
    "LC_ALL",
    "LC_CTYPE",
    "LC_MESSAGES",
}
AWS_NAMES = {
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_PROFILE",
    "AWS_CONFIG_FILE",
    "AWS_SHARED_CREDENTIALS_FILE",
}
SCRIPT_SETS = {"AWS_REGION", "AWS_PAGER"}
TF_VAR_NAMES = {
    "TF_VAR_region",
    "TF_VAR_api_access_cidr",
    "TF_VAR_budget_email",
    "TF_VAR_expected_account_id",
}
SHELL_ADDS = {"PWD", "OLDPWD", "SHLVL", "_"}  # what bash itself adds to a child

GIT_ENV = {
    **os.environ,
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
}


def git(root: Path, *args: str) -> str:
    done = subprocess.run(
        [
            "git",
            "-c",
            "user.name=Owner",
            "-c",
            "user.email=owner@example.com",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        cwd=root,
        env=GIT_ENV,
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.strip()


def env_names(log: str) -> set[str]:
    """The names in a stub's environment log (one ``NAME=value`` per line)."""
    return {
        line.split("=", 1)[0]
        for line in log.splitlines()
        if "=" in line and line != "--"
    }


@dataclass
class Tree:
    root: Path
    base_env: dict[str, str]
    stub_env: dict[str, str] = field(default_factory=dict)

    @property
    def script(self) -> Path:
        return self.root / "infra" / "terraform" / "aws.sh"

    @property
    def module(self) -> Path:
        return self.root / "infra" / "terraform" / "aws"

    @property
    def local_file(self) -> Path:
        return self.root / "infra" / "terraform" / "local.env-aws"

    @property
    def state_dir(self) -> Path:
        return self.root / STATE_DIRECTORY

    def calls(self) -> list[str]:
        log = self.root / "stub.log"
        return (
            [c for c in log.read_text().splitlines() if c.startswith("terraform ")]
            if log.exists()
            else []
        )

    def stub_lines(self) -> list[str]:
        log = self.root / "stub.log"
        return log.read_text().splitlines() if log.exists() else []

    def aws_calls(self) -> list[str]:
        log = self.root / "stub-aws.log"
        return log.read_text().splitlines() if log.exists() else []

    def terraform_env(self) -> str:
        log = self.root / "stub-env.log"
        return log.read_text() if log.exists() else ""

    def aws_env(self) -> str:
        log = self.root / "stub-aws-env.log"
        return log.read_text() if log.exists() else ""

    def terraform_calls(self, subcommand: str) -> list[str]:
        return [c for c in self.calls() if f" {subcommand}" in c]

    def subcommands(self) -> list[str]:
        return [call.split()[2] for call in self.calls()]

    def write_stub_env(self, extra: dict[str, str]) -> None:
        merged = {**self.stub_env, **extra}
        text = "".join(f"{k}={shlex.quote(v)}\n" for k, v in merged.items())
        (self.root / "stubs" / "stub.env").write_text(text)

    def command(self, *words: str, caller_umask: str | None = None) -> list[str]:
        command = [str(self.script), *words]
        if caller_umask is None:
            return command
        return [
            "bash",
            "-c",
            'umask "$1"; shift; exec "$@"',
            "umask",
            caller_umask,
            *command,
        ]

    def run(
        self,
        subcommand: str,
        *,
        terminal: bool = False,
        answer: str = "yes\n",
        caller_umask: str | None = None,
        **env: str,
    ) -> subprocess.CompletedProcess[str]:
        self.write_stub_env({k: v for k, v in env.items() if k.startswith("STUB_")})
        full = {
            **self.base_env,
            **{k: v for k, v in env.items() if not k.startswith("STUB_")},
        }
        command = self.command(
            *([subcommand] if subcommand else []), caller_umask=caller_umask
        )
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


def make_tree(root: Path) -> Tree:
    """The stand-in tree under ``root`` (which must exist and be empty)."""
    terraform_dir = root / "infra" / "terraform"
    (terraform_dir / "aws").mkdir(parents=True)
    for name in ("aws.sh", "common.sh"):
        shutil.copy2(TERRAFORM_DIR / name, terraform_dir / name)
    # The real ignore file, so that what the script's checks see as "committed"
    # is what the repository's own patterns say.
    shutil.copy2(REPO_ROOT / ".gitignore", root / ".gitignore")
    (terraform_dir / "aws" / "main.tf").write_text("# a stand-in module\n")
    git(root, "init", "-q")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "a stand-in tree")
    stubs = root / "stubs"
    stubs.mkdir()
    for name, text in (("terraform", STUB_TERRAFORM), ("aws", STUB_AWS)):
        (stubs / name).write_text(text)
        (stubs / name).chmod(0o755)
    env = {
        "PATH": f"{stubs}:{os.environ['PATH']}",
        "HOME": str(root),
    }
    stub_env = {
        "STUB_LOG": str(root / "stub.log"),
        "STUB_AWS_LOG": str(root / "stub-aws.log"),
        "STUB_ENV_LOG": str(root / "stub-env.log"),
        "STUB_AWS_ENV_LOG": str(root / "stub-aws-env.log"),
        "STUB_ACCOUNT": PINNED,
    }
    made = Tree(root, env, stub_env)
    made.write_stub_env({})
    return made


@pytest.fixture
def tree(tmp_path: Path) -> Tree:
    return make_tree(tmp_path)


def with_local_file(tree: Tree, text: str = LOCAL_FILE, mode: int = 0o600) -> Tree:
    tree.local_file.write_text(text)
    tree.local_file.chmod(mode)
    return tree


def with_saved_plan(
    tree: Tree,
    *,
    age_seconds: int = 0,
    commit: str | None = None,
    time_text: str | None = None,
) -> Tree:
    """A saved plan with the record ``plan`` leaves beside it: this tree's
    commit (or the one given), the time it was made (or the text given, to
    write a time the script must refuse) and the SHA-256 of the plan file."""
    plan = b"plan"
    (tree.module / PLAN_FILE).write_bytes(plan)
    commit = commit or git(tree.root, "rev-parse", "HEAD")
    made = time_text if time_text is not None else str(int(time.time()) - age_seconds)
    digest = hashlib.sha256(plan).hexdigest()
    (tree.module / META_FILE).write_text(
        f"commit={commit}\ntime={made}\nsha256={digest}\n"
    )
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


def test_validate_with_the_word_aws_checks_the_aws_directory_as_with_no_word(
    tree: Tree,
) -> None:
    with_a_gcp_directory(tree)

    done = run_words(tree, "validate", "aws")

    assert done.returncode == 0, everything_printed(done)
    assert tree.subcommands() == ["fmt", "init", "validate"]
    for call in tree.calls():
        assert f"-chdir={tree.module} " in call


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
        ["validate", "gcp-kubeadm"],
        ["validate", "aws-kubeadm", "aws-kubeadm"],
        ["validate", "aws-kubeadm", "aws"],
        ["validate", "aws", "aws-kubeadm"],
    ],
)
def test_validate_with_any_other_word_or_with_two_is_refused_before_a_program_runs(
    tree: Tree, words: list[str]
) -> None:
    with_a_gcp_directory(tree)
    with_an_aws_kubeadm_directory(tree)

    done = run_words(tree, *words)

    assert done.returncode == 2, everything_printed(done)
    assert "usage:" in done.stderr
    assert tree.calls() == []
    assert tree.aws_calls() == []


@pytest.mark.parametrize("subcommand", ["plan", "apply", "destroy"])
@pytest.mark.parametrize("word", ["gcp", "aws", "aws-kubeadm"])
def test_plan_apply_and_the_removal_take_no_word_and_refuse_one(
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


def test_the_usage_line_names_the_modules_validate_takes_and_keeps_its_old_form(
    tree: Tree,
) -> None:
    done = run_words(tree)

    assert done.returncode == 2
    assert "validate|plan|apply|destroy" in done.stderr
    assert "validate [aws|gcp|aws-kubeadm]" in done.stderr
    assert tree.calls() == []


def test_the_scripts_header_says_validate_takes_a_module_name() -> None:
    header = (TERRAFORM_DIR / "aws.sh").read_text(encoding="utf-8").split("set +x")[0]

    assert "validate gcp" in header
    assert "validate aws-kubeadm" in header
    assert "never planned" in header


@pytest.mark.parametrize(
    "words", [["validate"], ["validate", "gcp"], ["validate", "aws-kubeadm"]]
)
def test_validate_runs_terraform_with_no_credential_name_of_any_cloud(
    tree: Tree, words: list[str]
) -> None:
    with_a_gcp_directory(tree)
    with_an_aws_kubeadm_directory(tree)

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


def regions_the_module_allows() -> list[str]:
    """The list in the module's validation of ``region``, read from the file."""
    block = variable_block("region")
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


def test_the_regions_the_script_accepts_are_the_six_the_module_accepts() -> None:
    allowed = regions_the_module_allows()

    assert len(allowed) == 6  # the reader found them
    assert sorted(regions_the_script_allows()) == sorted(allowed)


def test_each_region_the_module_allows_goes_to_the_cli_and_terraform(
    tree: Tree,
) -> None:
    allowed = regions_the_module_allows()

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
    if how == "bash -x":
        command = ["bash", "-x", script, "plan"]
    elif how == "SHELLOPTS":
        env["SHELLOPTS"] = "xtrace"
        command = [script, "plan"]
    else:
        command = [
            "bash",
            "-c",
            'exec 9>"$1"; BASH_XTRACEFD=9; export BASH_XTRACEFD; '
            'exec bash -x "$2" plan',
            "trace",
            str(trace_file),
            script,
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
    for name in (".terraform.lock.hcl", ".trivyignore", "README.md", "aws.tfplan"):
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
        "aws.tfplan.meta",
    ],
)
def test_the_repository_ignores_the_files_that_could_hold_what_must_not_be_committed(
    name: str,
) -> None:
    done = subprocess.run(
        ["git", "check-ignore", "-q", f"infra/terraform/aws/{name}"],
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


# ── plan ─────────────────────────────────────────────────────────────────────


def test_plan_initialises_then_plans_into_a_saved_file_only_the_owner_can_read(
    tree: Tree,
) -> None:
    with_local_file(tree)

    done = tree.run("plan", caller_umask="022")

    assert done.returncode == 0, everything_printed(done)
    assert tree.subcommands() == ["init", "plan"]
    plan_call = tree.terraform_calls("plan")[0]
    assert f"-out={PLAN_FILE}" in plan_call
    assert "-input=false" in plan_call
    for name in (PLAN_FILE, META_FILE):
        mode = stat.S_IMODE((tree.module / name).stat().st_mode)
        assert mode & 0o077 == 0, name


def test_plan_hands_terraform_the_region_the_address_the_email_and_the_account(
    tree: Tree,
) -> None:
    with_local_file(tree)

    tree.run("plan", AWS_DEFAULT_REGION="us-east-1")

    seen = tree.terraform_env()
    assert "TF_VAR_region=eu-central-1" in seen
    assert f"TF_VAR_api_access_cidr={ENDPOINT_CIDR}" in seen
    assert f"TF_VAR_budget_email={BUDGET_EMAIL}" in seen
    # The module's provider refuses any other account (allowed_account_ids), so
    # the pinned one goes in as a variable, and nowhere else.
    assert f"TF_VAR_expected_account_id={PINNED}" in seen
    assert "AWS_REGION=eu-central-1" in seen
    assert "AWS_DEFAULT_REGION" not in seen
    assert f"={PINNED}" not in seen.replace(f"TF_VAR_expected_account_id={PINNED}", "")


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


def test_a_failed_plan_leaves_no_saved_plan_to_be_applied(tree: Tree) -> None:
    with_local_file(tree)
    with_saved_plan(tree)

    tree.run("plan", STUB_PLAN_STATUS="1")

    assert not (tree.module / PLAN_FILE).exists()
    assert not (tree.module / META_FILE).exists()


def test_plan_records_the_commit_the_time_and_the_plan_files_hash_beside_the_plan(
    tree: Tree,
) -> None:
    with_local_file(tree)
    before = int(time.time())

    done = tree.run("plan")

    assert done.returncode == 0, everything_printed(done)
    lines = (tree.module / META_FILE).read_text().splitlines()
    assert lines[0] == f"commit={git(tree.root, 'rev-parse', 'HEAD')}"
    assert re.fullmatch(r"time=[1-9][0-9]{9}", lines[1])
    assert before <= int(lines[1].removeprefix("time=")) <= int(time.time())
    digest = hashlib.sha256((tree.module / PLAN_FILE).read_bytes()).hexdigest()
    assert lines[2] == f"sha256={digest}"
    assert len(lines) == 3


# ── the state ────────────────────────────────────────────────────────────────


def test_the_state_lives_in_a_directory_under_home_that_only_the_owner_can_enter(
    tree: Tree,
) -> None:
    with_local_file(tree)
    tree.state_dir.mkdir(parents=True, mode=0o755)
    tree.state_dir.chmod(0o755)

    done = tree.run("plan")

    assert done.returncode == 0, everything_printed(done)
    (init,) = tree.terraform_calls("init")
    path = tree.state_dir / "aws.tfstate"
    assert f"-backend-config=path={path}" in init
    assert stat.S_IMODE(tree.state_dir.stat().st_mode) == 0o700
    assert tree.module not in path.parents  # not in a checkout


def test_a_state_directory_that_does_not_exist_is_made_private(tree: Tree) -> None:
    with_local_file(tree)
    assert not tree.state_dir.exists()

    tree.run("plan", caller_umask="022")

    assert stat.S_IMODE(tree.state_dir.stat().st_mode) == 0o700


def test_validate_makes_no_state_directory_and_names_no_backend_path(
    tree: Tree,
) -> None:
    tree.run("validate")

    assert not tree.state_dir.exists()


@pytest.mark.parametrize("subcommand", ["apply", "destroy"])
def test_apply_and_removal_find_the_same_state(tree: Tree, subcommand: str) -> None:
    with_local_file(tree)
    with_saved_plan(tree)

    done = tree.run(subcommand, terminal=subcommand == "destroy")

    assert done.returncode == 0, everything_printed(done)
    inits = tree.terraform_calls("init")
    if subcommand == "destroy":
        (init,) = inits
        assert f"-backend-config=path={tree.state_dir / 'aws.tfstate'}" in init


def test_without_a_home_the_state_has_no_place_and_the_script_refuses(
    tree: Tree,
) -> None:
    with_local_file(tree)
    env = {k: v for k, v in tree.base_env.items() if k != "HOME"}

    done = subprocess.run(
        [str(tree.script), "plan"],
        capture_output=True,
        text=True,
        env=env,
        cwd=tree.root,
        stdin=subprocess.DEVNULL,
        timeout=60,
        check=False,
    )

    assert done.returncode == 1
    assert "HOME" in done.stderr
    assert tree.terraform_calls("plan") == []


@pytest.mark.parametrize("subcommand", ["plan", "destroy"])
def test_init_is_told_to_use_the_scripts_own_state_path_whatever_an_older_init_left(
    tree: Tree, subcommand: str
) -> None:
    """Terraform stops with "Backend configuration changed" when a hand-made init
    named another path, and -reconfigure takes the script's path with no copy of
    a state and no question (read on a scratch directory, with no provider)."""
    with_local_file(tree)

    done = tree.run(subcommand, terminal=subcommand == "destroy")

    assert done.returncode == 0, everything_printed(done)
    (init,) = tree.terraform_calls("init")
    assert "-reconfigure" in init
    assert "-input=false" in init
    assert "-migrate-state" not in init
    assert f"-backend-config=path={tree.state_dir / 'aws.tfstate'}" in init


def test_the_init_of_validate_has_no_backend_and_needs_no_reconfigure(
    tree: Tree,
) -> None:
    tree.run("validate")

    (init,) = tree.terraform_calls("init")
    assert "-backend=false" in init
    assert "-reconfigure" not in init


# What a hand-made workspace leaves: `.terraform/environment` names it, and
# Terraform then keeps the state in terraform.tfstate.d/<name>/ in the module's
# directory (a checkout), not at the path the script gave. Selecting the default
# workspace again leaves the file in place with the word `default` in it.
def with_workspace(tree: Tree, name: str | None) -> Tree:
    (tree.module / ".terraform").mkdir(exist_ok=True)
    if name is not None:
        (tree.module / ".terraform" / "environment").write_text(name)
    return tree


@pytest.mark.parametrize("subcommand", ["plan", "destroy"])
@pytest.mark.parametrize(
    "name",
    [
        "w1",
        "staging",
        "default-2",
        "default\nother",  # the whole content is the name, as Terraform reads it
        "default\nother\n",
        "other\ndefault\n",
        "default default",
        "default\n\nother\n",
    ],
)
def test_another_workspace_is_refused_after_init_and_before_anything_else(
    tree: Tree, subcommand: str, name: str
) -> None:
    with_local_file(tree)
    with_workspace(tree, name)

    done = run_refusing(tree, subcommand)

    printed = everything_printed(done)
    assert done.returncode == 1, printed
    assert "workspace" in printed
    assert "workspace select default" in printed  # how to get back
    assert "terraform.tfstate.d" in printed
    assert tree.subcommands() == ["init"]
    assert tree.terraform_calls(subcommand) == []
    assert_no_identifier_printed(done)


def test_apply_refuses_a_workspace_other_than_the_default_and_keeps_the_plan(
    tree: Tree,
) -> None:
    """apply does no init, so a file left between the plan and the apply would
    send the applied state into the checkout unseen: .terraform/ is ignored."""
    with_local_file(tree)
    with_saved_plan(tree)
    with_workspace(tree, "w1")

    done = tree.run("apply")

    assert done.returncode == 1, everything_printed(done)
    assert "workspace select default" in done.stderr
    assert tree.terraform_calls("apply") == []
    assert (tree.module / PLAN_FILE).exists()  # the plan is still good


@pytest.mark.parametrize(
    "name",
    [
        None,
        "default",
        "default\n",
        # Terraform trims the whole content (leading and trailing space, tabs,
        # carriage returns, newlines), and an empty or blank file is the default
        # workspace: observed on Terraform v1.16.5 with `workspace show`.
        " default",
        "  default \r\n\t\n",
        "\n\ndefault\n\n",
        "",
        "  \n",
    ],
)
@pytest.mark.parametrize("subcommand", ["plan", "apply", "destroy"])
def test_no_workspace_file_or_one_that_says_default_is_not_refused(
    tree: Tree, subcommand: str, name: str | None
) -> None:
    with_local_file(tree)
    with_saved_plan(tree)
    with_workspace(tree, name)

    done = tree.run(subcommand, terminal=subcommand == "destroy")

    assert done.returncode == 0, everything_printed(done)


@pytest.mark.parametrize("subcommand", ["plan", "apply", "destroy"])
def test_a_directory_where_the_workspace_file_should_be_is_the_scripts_sentence_only(
    tree: Tree, subcommand: str
) -> None:
    with_local_file(tree)
    with_saved_plan(tree)
    (tree.module / ".terraform" / "environment").mkdir(parents=True)

    done = tree.run(subcommand, terminal=subcommand == "destroy")

    printed = everything_printed(done)
    assert done.returncode == 1, printed
    assert "workspace select default" in printed
    assert "Is a directory" not in printed
    assert "cat:" not in printed  # nor the failed read's own line
    assert tree.terraform_calls(subcommand) == []


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any file")
@pytest.mark.parametrize("subcommand", ["plan", "apply", "destroy"])
def test_a_workspace_file_that_cannot_be_read_is_the_scripts_sentence_only(
    tree: Tree, subcommand: str
) -> None:
    with_local_file(tree)
    with_saved_plan(tree)
    with_workspace(tree, "default")
    (tree.module / ".terraform" / "environment").chmod(0)

    done = tree.run(subcommand, terminal=subcommand == "destroy")

    printed = everything_printed(done)
    assert done.returncode == 1, printed
    assert "workspace select default" in printed
    assert "Permission denied" not in printed
    assert tree.terraform_calls(subcommand) == []


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
    assert not (tree.module / META_FILE).exists()
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
    assert not (tree.module / META_FILE).exists()


def test_apply_to_another_account_leaves_the_saved_plan_for_the_right_one(
    tree: Tree,
) -> None:
    with_local_file(tree)
    with_saved_plan(tree)

    done = tree.run("apply", STUB_ACCOUNT=OTHER)

    assert done.returncode == 1
    assert (tree.module / PLAN_FILE).exists()


# ── a saved plan is applied only if it is this tree's and fresh ──────────────


def assert_plan_refused_and_dropped(tree: Tree, done, sentence: str) -> None:
    assert done.returncode == 1, everything_printed(done)
    assert sentence in done.stderr
    assert "make aws-plan" in done.stderr
    assert tree.terraform_calls("apply") == []
    # The plan is judged last, so the identity call came first and was made
    # once: that is what pins the order (see the test of the order below).
    assert len(tree.aws_calls()) == 1
    assert not (tree.module / PLAN_FILE).exists()
    assert not (tree.module / META_FILE).exists()


def test_a_plan_with_no_record_beside_it_is_not_applied(tree: Tree) -> None:
    with_local_file(tree)
    with_saved_plan(tree)
    (tree.module / META_FILE).unlink()

    done = tree.run("apply")

    assert_plan_refused_and_dropped(tree, done, "record")


COMMIT_LINE = "commit=0123456789abcdef0123456789abcdef01234567"
TIME_LINE = "time=1791305195"
HASH_LINE = "sha256=" + "0" * 64


@pytest.mark.parametrize(
    "text",
    [
        "",
        f"commit=not-a-commit\n{TIME_LINE}\n{HASH_LINE}\n",
        f"{TIME_LINE}\n{HASH_LINE}\n",
        f"{COMMIT_LINE}\n",
        f"{COMMIT_LINE}\ntime=soon\n{HASH_LINE}\n",
        f"{COMMIT_LINE}\ntime=1\n{HASH_LINE}\n",
        f"{COMMIT_LINE}\n{TIME_LINE}\n",  # the two-line record of an older script
        f"{COMMIT_LINE}\n{TIME_LINE}\nsha256=not-a-hash\n",
        f"{COMMIT_LINE}\n{TIME_LINE}\nsha256={'0' * 63}\n",
        f"{COMMIT_LINE}\n{TIME_LINE}\nsha256={'0' * 65}\n",
        f"{COMMIT_LINE}\n{TIME_LINE}\nsha256={'A' * 64}\n",
        f"{COMMIT_LINE}\n{TIME_LINE}\n{HASH_LINE}\nextra\n",
        f"{COMMIT_LINE}\n{HASH_LINE}\n{TIME_LINE}\n",  # the lines in another order
    ],
)
def test_a_record_that_is_not_a_commit_a_time_and_a_hash_is_not_trusted(
    tree: Tree, text: str
) -> None:
    with_local_file(tree)
    with_saved_plan(tree)
    (tree.module / META_FILE).write_text(text)

    done = tree.run("apply")

    assert_plan_refused_and_dropped(tree, done, "record")


def test_a_plan_file_written_over_the_one_the_script_made_is_not_applied(
    tree: Tree,
) -> None:
    """A `terraform plan -out=aws.tfplan` run by hand, with a -target or a
    variable, after `make aws-plan`: the record is the script's, the bytes are
    not."""
    with_local_file(tree)
    with_saved_plan(tree)
    (tree.module / PLAN_FILE).write_bytes(b"a plan made by hand, over the script's")

    done = tree.run("apply")

    assert_plan_refused_and_dropped(tree, done, "SHA-256")


def test_a_plan_file_that_is_the_one_the_record_names_is_applied(tree: Tree) -> None:
    with_local_file(tree)
    plan = tree.run("plan")
    assert plan.returncode == 0, everything_printed(plan)

    done = tree.run("apply")

    assert done.returncode == 0, everything_printed(done)
    assert tree.terraform_calls("apply") != []


@pytest.mark.parametrize(
    "time_text",
    [
        "08",  # not an octal digit: bash stops with "value too great for base"
        "01791305195",  # the same, with a leading zero
        "0",
        "015261222753",  # the octal spelling of the clock: bash read it as fresh
        "+1791305195",
        " 1791305195",
        "1791305195 ",
        "0x1000",
        "17913051950000000000000",
    ],
)
def test_a_time_that_is_not_a_plain_decimal_is_not_read_in_any_other_base(
    tree: Tree, time_text: str
) -> None:
    with_local_file(tree)
    with_saved_plan(tree, time_text=time_text)

    done = tree.run("apply")

    assert_plan_refused_and_dropped(tree, done, "record")
    assert "value too great" not in done.stderr


def test_the_octal_spelling_of_this_very_second_is_not_read_as_fresh(
    tree: Tree,
) -> None:
    # Built here, in the body: a parameter built from the clock at import would
    # give two xdist workers that import the file in different seconds different
    # test IDs, and the run stops at collection.
    octal_now = f"0{oct(int(time.time()))[2:]}"
    with_local_file(tree)
    with_saved_plan(tree, time_text=octal_now)

    done = tree.run("apply")

    assert_plan_refused_and_dropped(tree, done, "record")
    assert "value too great" not in done.stderr


def test_a_plan_made_at_another_commit_is_not_applied(tree: Tree) -> None:
    with_local_file(tree)
    with_saved_plan(tree)
    (tree.module / "main.tf").write_text("# changed and committed\n")
    git(tree.root, "commit", "-q", "-am", "a change after the plan")

    done = tree.run("apply")

    assert_plan_refused_and_dropped(tree, done, "commit")


def test_a_tracked_file_changed_in_the_module_since_the_plan_is_not_applied(
    tree: Tree,
) -> None:
    with_local_file(tree)
    with_saved_plan(tree)
    (tree.module / "main.tf").write_text("# changed, not committed\n")

    done = tree.run("apply")

    assert_plan_refused_and_dropped(tree, done, "uncommitted")


def test_a_new_file_in_the_module_since_the_plan_is_not_applied(tree: Tree) -> None:
    with_local_file(tree)
    with_saved_plan(tree)
    (tree.module / "extra.tf").write_text("# a new resource\n")

    done = tree.run("apply")

    assert_plan_refused_and_dropped(tree, done, "uncommitted")


def test_a_change_outside_the_module_is_not_the_modules_business(tree: Tree) -> None:
    with_local_file(tree)
    with_saved_plan(tree)
    (tree.root / "notes.md").write_text("not in the module\n")

    done = tree.run("apply")

    assert done.returncode == 0, everything_printed(done)


def test_a_plan_older_than_the_bound_is_not_applied(tree: Tree) -> None:
    with_local_file(tree)
    with_saved_plan(tree, age_seconds=PLAN_MAX_AGE_SECONDS + 60)

    done = tree.run("apply")

    assert_plan_refused_and_dropped(tree, done, "old")


def test_a_plan_a_little_younger_than_the_bound_is_applied(tree: Tree) -> None:
    with_local_file(tree)
    with_saved_plan(tree, age_seconds=PLAN_MAX_AGE_SECONDS - 60)

    done = tree.run("apply")

    assert done.returncode == 0, everything_printed(done)
    assert tree.terraform_calls("apply") != []


def test_a_plan_dated_in_the_future_is_not_applied(tree: Tree) -> None:
    with_local_file(tree)
    with_saved_plan(tree, age_seconds=-3600)

    done = tree.run("apply")

    assert_plan_refused_and_dropped(tree, done, "old")


def test_planning_in_a_changed_module_shows_the_plan_but_writes_no_record(
    tree: Tree,
) -> None:
    with_local_file(tree)
    (tree.module / "main.tf").write_text("# not committed\n")

    done = tree.run("plan")

    assert done.returncode == 0, everything_printed(done)
    assert "uncommitted" in done.stdout
    assert "stub terraform plan" in done.stdout  # reading the plan is free
    assert (tree.module / PLAN_FILE).exists()
    assert not (tree.module / META_FILE).exists()


def test_a_plan_made_from_a_changed_module_is_not_applied_even_after_a_hand_revert(
    tree: Tree,
) -> None:
    """What the warning says becomes true: the plan was made from a tree no
    commit describes, and putting the files back by hand afterwards does not
    make the commit describe it."""
    with_local_file(tree)
    original = (tree.module / "main.tf").read_text()
    (tree.module / "main.tf").write_text("# an edit the plan was made from\n")
    tree.run("plan")
    (tree.module / "main.tf").write_text(original)
    assert git(tree.root, "status", "--porcelain", "--", "infra/terraform/aws") == ""

    done = tree.run("apply")

    assert done.returncode == 1, everything_printed(done)
    assert "record" in done.stderr
    assert "make aws-plan" in done.stderr
    assert tree.terraform_calls("apply") == []
    assert len(tree.aws_calls()) == 2  # the plan's own, and the apply's, first
    assert not (tree.module / PLAN_FILE).exists()


def test_an_untracked_file_in_the_module_at_plan_time_also_writes_no_record(
    tree: Tree,
) -> None:
    with_local_file(tree)
    (tree.module / "extra.tf").write_text("# a new resource\n")

    done = tree.run("plan")

    assert done.returncode == 0, everything_printed(done)
    assert not (tree.module / META_FILE).exists()


def test_a_module_that_changes_while_the_plan_runs_gets_no_record(tree: Tree) -> None:
    with_local_file(tree)

    done = tree.run("plan", STUB_PLAN_CHANGES_THE_MODULE="1")

    assert done.returncode == 0, everything_printed(done)
    assert not (tree.module / META_FILE).exists()


# ── the plan is judged last, and read from standard input ────────────────────


@pytest.mark.parametrize("fault", ["too old", "not the record's file"])
def test_the_saved_plan_is_judged_after_the_sign_in_call_not_before_it(
    tree: Tree, fault: str
) -> None:
    """The order is observable: when the sign-in call fails, a saved plan that
    would have been refused is still there (nothing read it: neither its hash
    nor its age was taken before a call that can hang), and the sentence is the
    sign-in's."""
    with_local_file(tree)
    if fault == "too old":
        with_saved_plan(tree, age_seconds=PLAN_MAX_AGE_SECONDS + 60)
    else:
        with_saved_plan(tree)
        (tree.module / PLAN_FILE).write_bytes(b"a plan made by hand")

    done = tree.run("apply", STUB_AWS_STATUS="1")

    assert done.returncode == 1, everything_printed(done)
    assert "sign in" in done.stderr
    assert "too old" not in done.stderr
    assert "SHA-256" not in done.stderr
    assert len(tree.aws_calls()) == 1
    assert (tree.module / PLAN_FILE).exists()
    assert (tree.module / META_FILE).exists()


def test_a_plan_that_would_be_refused_is_still_refused_once_signed_in(
    tree: Tree,
) -> None:
    with_local_file(tree)
    with_saved_plan(tree, age_seconds=PLAN_MAX_AGE_SECONDS + 60)

    done = tree.run("apply")

    assert_plan_refused_and_dropped(tree, done, "old")


def test_a_module_path_with_a_backslash_does_not_spoil_the_plans_hash(
    tmp_path: Path,
) -> None:
    """`sha256sum PATH` puts a backslash before the digest when the path holds
    one, and the record then failed its own shape at apply. The path is built
    here, in the body, and the hash is read from standard input."""
    tree = make_tree(tmp_path / "with\\a\\backslash")
    with_local_file(tree)

    plan = tree.run("plan")
    assert plan.returncode == 0, everything_printed(plan)
    record = (tree.module / META_FILE).read_text()
    assert re.fullmatch(r"commit=\w{40}\ntime=\d{10}\nsha256=[0-9a-f]{64}\n", record)
    done = tree.run("apply")

    assert done.returncode == 0, everything_printed(done)
    assert tree.terraform_calls("apply") != []


# ── an ignore rule cannot hide a Terraform file from the clean-tree check ───

HIDING_PLACES = [
    "the caller's default ignore file",
    "the repository's info/exclude",
    "a .gitignore in the module directory",
]
# What apply says about a file git's own status does not show (the last two) and
# about one it does, once the script's git has the caller's ignore file off.
APPLY_SENTENCE = {
    "the caller's default ignore file": "uncommitted",
    "the repository's info/exclude": "untracked Terraform",
    "a .gitignore in the module directory": "untracked Terraform",
}


def hide_zz_files(tree: Tree, where: str) -> None:
    """An ignore rule that hides every file whose name begins with zz, in one of
    three places the script's git read before. The module's own .gitignore is
    committed first, so that it is not itself the change that is seen."""
    if where == HIDING_PLACES[0]:
        (tree.root / ".config" / "git").mkdir(parents=True)
        (tree.root / ".config" / "git" / "ignore").write_text("zz*\n")
    elif where == HIDING_PLACES[1]:
        (tree.root / ".git" / "info" / "exclude").write_text("zz*\n")
    else:
        (tree.module / ".gitignore").write_text("zz*\n")
        git(tree.root, "add", "-A")
        git(tree.root, "commit", "-q", "-m", "an ignore file in the module")


def plain_status_as_the_caller(tree: Tree) -> str:
    """`git status` of the module with no override of the script's, run with the
    stand-in home as the home, so that its default ignore file is read."""
    env = {k: v for k, v in GIT_ENV.items() if k != "XDG_CONFIG_HOME"}
    env["HOME"] = str(tree.root)
    done = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all", "--", "."],
        cwd=tree.module,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.strip()


@pytest.mark.parametrize("name", ["zz_extra.tf", "zz_extra.tf.json"])
@pytest.mark.parametrize("where", HIDING_PLACES)
def test_a_terraform_file_that_an_ignore_rule_hides_gets_the_plan_no_record(
    tree: Tree, where: str, name: str
) -> None:
    with_local_file(tree)
    hide_zz_files(tree, where)
    (tree.module / name).write_text("# a resource that changes the plan\n")
    # The premise: a plain `git status` as this home's owner runs it says nothing.
    assert plain_status_as_the_caller(tree) == ""

    done = tree.run("plan")

    assert done.returncode == 0, everything_printed(done)
    assert "stub terraform plan" in done.stdout  # reading the plan is free
    assert not (tree.module / META_FILE).exists()
    assert "zz_extra" not in everything_printed(done)


@pytest.mark.parametrize("where", HIDING_PLACES[1:])
def test_the_plan_names_the_kind_and_the_count_of_the_files_an_ignore_rule_hides(
    tree: Tree, where: str
) -> None:
    with_local_file(tree)
    hide_zz_files(tree, where)
    (tree.module / "zz_one.tf").write_text("# one\n")
    (tree.module / "zz_two.tf.json").write_text("{}\n")

    done = tree.run("plan")

    assert done.returncode == 0, everything_printed(done)
    assert "2 untracked Terraform files" in done.stdout
    assert "zz_one" not in everything_printed(done)
    assert "zz_two" not in everything_printed(done)
    assert not (tree.module / META_FILE).exists()


@pytest.mark.parametrize("name", ["zz_extra.tf", "zz_extra.tf.json"])
@pytest.mark.parametrize("where", HIDING_PLACES)
def test_a_plan_made_before_a_hidden_terraform_file_appeared_is_not_applied(
    tree: Tree, where: str, name: str
) -> None:
    with_local_file(tree)
    hide_zz_files(tree, where)
    with_saved_plan(tree)
    (tree.module / name).write_text("# a resource the plan was not made from\n")

    done = tree.run("apply")

    assert_plan_refused_and_dropped(tree, done, APPLY_SENTENCE[where])
    assert "zz_extra" not in everything_printed(done)


def test_the_sentence_for_a_hidden_terraform_file_says_how_to_get_rid_of_it(
    tree: Tree,
) -> None:
    """`commit it` is no advice for a file git will not take."""
    with_local_file(tree)
    hide_zz_files(tree, HIDING_PLACES[1])
    with_saved_plan(tree)
    (tree.module / "zz_extra.tf").write_text("# not committed\n")

    done = tree.run("apply")

    assert "1 untracked Terraform file" in done.stderr
    assert "ignore" in done.stderr
    assert "remove it" in done.stderr


def test_the_modules_own_ignored_files_that_are_not_terraform_do_not_trip_it(
    tree: Tree,
) -> None:
    """The plan, its record, what init downloads (a provider, and a module's own
    .tf files under .terraform/) are ignored and untracked: none is a file the
    plan reads from this directory."""
    with_local_file(tree)
    with_saved_plan(tree)
    providers = tree.module / ".terraform" / "providers" / "registry" / "hashicorp"
    providers.mkdir(parents=True)
    (providers / "terraform-provider-aws").write_text("x\n")
    downloaded = tree.module / ".terraform" / "modules" / "network"
    downloaded.mkdir(parents=True)
    (downloaded / "main.tf").write_text("# downloaded by init\n")
    (tree.module / "terraform.tfstate").write_text("{}\n")

    done = tree.run("apply")

    assert done.returncode == 0, everything_printed(done)
    plan = tree.run("plan")
    assert plan.returncode == 0, everything_printed(plan)
    assert (tree.module / META_FILE).exists()


def test_a_terraform_file_in_a_subdirectory_is_not_one_the_plan_reads(
    tree: Tree,
) -> None:
    with_local_file(tree)
    hide_zz_files(tree, HIDING_PLACES[1])
    (tree.module / "zzdir").mkdir()
    (tree.module / "zzdir" / "main.tf").write_text("# not loaded\n")

    done = tree.run("plan")

    assert done.returncode == 0, everything_printed(done)
    assert (tree.module / META_FILE).exists()


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
    calls = tree.stub_lines()
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


def test_removal_initialises_first_counts_what_the_state_holds_and_then_removes(
    tree: Tree,
) -> None:
    with_local_file(tree)

    done = tree.run("destroy", terminal=True, STUB_STATE_COUNT="4")

    assert done.returncode == 0, done.stdout
    assert tree.subcommands() == ["init", "state", "destroy", "state"]
    assert "4 resources" in done.stdout
    assert "removed" in done.stdout.splitlines()[-1]


@pytest.mark.parametrize(
    "how", [{"STUB_STATE_COUNT": "0"}, {"STUB_STATE_MISSING": "1"}]
)
def test_removal_over_an_empty_state_refuses_with_a_sentence_and_never_says_removed(
    tree: Tree, how: dict[str, str]
) -> None:
    with_local_file(tree)

    done = tree.run("destroy", terminal=True, **how)

    assert done.returncode == 1, done.stdout
    assert "holds nothing" in done.stdout
    assert "removed." not in done.stdout
    assert "lost" in done.stdout  # says what to do if the state was lost
    assert "README" in done.stdout
    assert tree.terraform_calls("destroy") == []


def test_a_state_that_holds_only_data_sources_holds_nothing_to_remove(
    tree: Tree,
) -> None:
    with_local_file(tree)

    done = tree.run("destroy", terminal=True, STUB_STATE_COUNT="0", STUB_STATE_DATA="1")

    assert done.returncode == 1, done.stdout
    assert "holds nothing" in done.stdout
    assert tree.terraform_calls("destroy") == []


def test_removal_that_cannot_read_the_state_refuses_and_says_so(tree: Tree) -> None:
    with_local_file(tree)

    done = tree.run("destroy", terminal=True, STUB_STATE_STATUS="1")

    assert done.returncode == 1, done.stdout
    assert "cannot read the state" in done.stdout
    assert "Failed to load state" not in done.stdout  # terraform's words stay out
    assert tree.terraform_calls("destroy") == []


def test_removal_does_not_say_removed_while_the_state_still_holds_resources(
    tree: Tree,
) -> None:
    with_local_file(tree)

    done = tree.run("destroy", terminal=True, STUB_REMOVAL_KEEPS_STATE="1")

    assert done.returncode == 1, done.stdout
    assert "removed." not in done.stdout
    assert "still holds" in done.stdout


def test_a_failed_removal_says_what_is_left_and_what_to_do_and_never_says_removed(
    tree: Tree,
) -> None:
    with_local_file(tree)

    done = tree.run("destroy", terminal=True, STUB_DESTROY_STATUS="3")

    assert done.returncode == 1, done.stdout
    assert "removed." not in done.stdout
    assert "failed" in done.stdout
    assert "exit 3" in done.stdout
    assert "still holds what is left" in done.stdout
    assert "state list" in done.stdout  # how to read it
    assert "console" in done.stdout
    assert "make aws-destroy" in done.stdout  # run it again
    assert "Removal" in done.stdout  # the README's section
    assert tree.subcommands() == ["init", "state", "destroy"]  # no second count
    assert_no_identifier_printed(done)


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

    assert needed >= {
        "api_access_cidr",
        "budget_email",
        "expected_account_id",
    }  # the reader found them
    assert needed - variables_the_script_exports() == set()


def test_the_script_exports_no_name_the_module_does_not_declare() -> None:
    exported = variables_the_script_exports()

    assert exported - variables_declared() == set()
    assert exported >= {"region", "budget_email"}  # the reader found them


def test_the_names_the_tests_expect_the_script_to_export_are_the_names_it_does() -> (
    None
):
    assert {f"TF_VAR_{n}" for n in variables_the_script_exports()} == TF_VAR_NAMES


# ── the module's own rules (what the two reviews asked of the .tf files) ─────

MODULE_DIR = TERRAFORM_DIR / "aws"


def module_text() -> str:
    return "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(MODULE_DIR.glob("*.tf"))
    )


def variable_block(name: str) -> str:
    text = MODULE_VARIABLES.read_text(encoding="utf-8")
    declared = re.split(r'^variable "(\w+)" \{$', text, flags=re.MULTILINE)
    return dict(zip(declared[1::2], declared[2::2], strict=True))[name]


def test_no_aws_managed_policy_arn_is_typed_anywhere_in_the_module() -> None:
    assert "arn:aws:iam::aws:policy" not in module_text()


def test_each_managed_policy_is_read_by_name_and_attached_by_the_arn_it_returns() -> (
    None
):
    text = (MODULE_DIR / "cluster.tf").read_text(encoding="utf-8")

    lookups = re.findall(r'^data "aws_iam_policy" "(\w+)"', text, flags=re.MULTILINE)
    attachments = re.findall(
        r'^resource "aws_iam_role_policy_attachment" "\w+" \{\n(.*?)^\}$',
        text,
        flags=re.MULTILINE | re.DOTALL,
    )
    assert sorted(lookups) == ["cluster", "ebs_csi", "node"]
    for name in (
        "AmazonEKSClusterPolicy",
        "AmazonEKSWorkerNodePolicy",
        "AmazonEC2ContainerRegistryPullOnly",
        "AmazonEKS_CNI_Policy",
        "AmazonEBSCSIDriverPolicyV2",
    ):
        assert f'"{name}"' in text
    assert len(attachments) == 3
    for body in attachments:
        assert re.search(r"policy_arn\s*=\s*data\.aws_iam_policy\.", body)


def test_the_provider_refuses_every_account_but_the_expected_one() -> None:
    providers = (MODULE_DIR / "providers.tf").read_text(encoding="utf-8")

    assert re.search(
        r"allowed_account_ids\s*=\s*\[var\.expected_account_id\]", providers
    )
    block = variable_block("expected_account_id")
    assert "default" not in re.sub(r"#.*", "", block).replace("No default", "")
    assert re.search(r"^  sensitive\s*=\s*true", block, re.MULTILINE)
    assert "12" in block  # twelve digits


@pytest.mark.parametrize(
    "name", ["expected_account_id", "api_access_cidr", "budget_email"]
)
def test_a_variable_that_holds_an_account_an_address_or_an_email_is_sensitive(
    name: str,
) -> None:
    assert re.search(r"^  sensitive\s*=\s*true", variable_block(name), re.MULTILINE)


@pytest.mark.parametrize(
    ("name", "default"),
    [
        ("node_instance_type", "t3.large"),
        ("database_instance_class", "db.t4g.small"),
    ],
)
def test_an_instance_type_comes_from_a_short_list_that_holds_its_default(
    name: str, default: str
) -> None:
    block = variable_block(name)

    (listed,) = re.findall(rf"contains\(\[([^\]]*)\], var\.{name}\)", block)
    options = re.findall(r'"([^"]+)"', listed)
    assert default in options
    assert 2 <= len(options) <= 3
    assert f'default     = "{default}"' in block
    assert "cost ceiling" in block
    assert "variables.tf" in block  # says where to widen it


def test_each_pod_identity_role_trusts_one_service_account_of_one_cluster() -> None:
    text = module_text()
    documents = re.findall(
        r'^data "aws_iam_policy_document" "(\w+)" \{\n(.*?)^\}$',
        text,
        flags=re.MULTILINE | re.DOTALL,
    )

    pod_identity = {
        name: body for name, body in documents if "pods.eks.amazonaws.com" in body
    }
    assert sorted(pod_identity) == ["ebs_csi_trust", "workload_trust"]
    for body in pod_identity.values():
        for tag in (
            "aws:RequestTag/kubernetes-namespace",
            "aws:RequestTag/kubernetes-service-account",
            "aws:RequestTag/eks-cluster-name",
        ):
            assert f'variable = "{tag}"' in body
        assert 'test     = "StringEquals"' in body
    assert 'values   = ["kube-system"]' in pod_identity["ebs_csi_trust"]
    assert 'values   = ["ebs-csi-controller-sa"]' in pod_identity["ebs_csi_trust"]
    assert "var.workload_namespace" in pod_identity["workload_trust"]
    assert "var.workload_service_account" in pod_identity["workload_trust"]


def test_the_two_pod_identity_roles_do_not_share_a_trust_document() -> None:
    text = module_text()

    assert "pod_identity_trust" not in text
    assert "ebs_csi_trust.json" in text
    assert "workload_trust.json" in text


def test_the_cluster_role_trusts_the_eks_service_for_assume_role_alone() -> None:
    text = (MODULE_DIR / "cluster.tf").read_text(encoding="utf-8")
    (body,) = re.findall(
        r'^data "aws_iam_policy_document" "cluster_trust" \{\n(.*?)^\}$',
        text,
        flags=re.MULTILINE | re.DOTALL,
    )

    assert 'actions = ["sts:AssumeRole"]' in body
    assert "TagSession" not in body


def test_the_module_keeps_its_state_out_of_its_own_directory() -> None:
    versions = (MODULE_DIR / "versions.tf").read_text(encoding="utf-8")

    assert re.search(r'^  backend "local" \{\}$', versions, re.MULTILINE)
    assert "path" not in re.sub(r"#.*", "", versions)  # the script gives the path


# What `terraform console` says of each value: the validations run when it
# starts, with no provider and no account. Skipped where Terraform is not
# installed, and failed under GITHUB_ACTIONS=true (``terraformsupport``): the
# python workflow installs it.
VALID = {
    "budget_email": BUDGET_EMAIL,
    "api_access_cidr": ENDPOINT_CIDR,
    "expected_account_id": PINNED,
}


def evaluate(tmp_path: Path, name: str, value: str) -> str:
    """Everything `terraform console` prints for var.<name> set to the value,
    the other required variables valid. The scratch copy holds variables.tf and
    nothing else."""
    scratch = tmp_path / "scratch"
    scratch.mkdir(exist_ok=True)
    shutil.copy2(MODULE_VARIABLES, scratch / "variables.tf")
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
        **{f"TF_VAR_{k}": v for k, v in {**VALID, name: value}.items()},
    }
    done = subprocess.run(
        ["terraform", f"-chdir={scratch}", "console", "-no-color"],
        input=f"var.{name}\n",
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )
    return done.stdout + done.stderr


REFUSED = "Invalid value for variable"


@needs_terraform
@pytest.mark.parametrize(
    "value",
    [
        "203.0.113.7/32",
        "100.63.255.255/32",
        "126.255.255.255/32",
        "128.0.0.1/32",
        "169.253.1.1/32",
        "172.15.255.255/32",
        "172.32.0.1/32",
        "192.167.1.1/32",
        "223.255.255.255/32",
        "11.0.0.1/32",
        "8.8.8.8/32",
        "203.0.113.0/32",  # a bare zero is an octet; a leading zero is not
        "1.0.0.1/32",
        "203.0.113.100/32",
    ],
)
def test_a_public_ipv4_address_as_a_slash_32_is_accepted(
    tmp_path: Path, value: str
) -> None:
    assert REFUSED not in evaluate(tmp_path, "api_access_cidr", value)


@needs_terraform
@pytest.mark.parametrize(
    "value",
    [
        "127.0.0.1/32",
        "10.0.0.1/32",
        "10.255.255.255/32",
        "100.64.0.1/32",
        "100.127.255.255/32",
        "169.254.169.254/32",
        "172.16.0.1/32",
        "172.31.255.255/32",
        "192.168.1.1/32",
        "0.0.0.0/32",
        "0.1.2.3/32",
        "224.0.0.1/32",
        "255.255.255.255/32",
        "203.0.113.0/24",
        "203.0.113.7",
        "999.1.1.1/32",
        # An octet has no leading zero: "010" slipped past the private-range
        # pattern, which expects "10", and some programs read it as octal.
        "010.1.1.1/32",
        "010.0.0.1/32",
        "10.01.1.1/32",
        "203.0.113.07/32",
        "203.00.113.7/32",
        "00.1.1.1/32",
        "1.1.1.001/32",
        "0127.0.0.1/32",
        "2001:db8::1/32",
        "not-an-address",
        "",
    ],
)
def test_an_address_that_would_lock_the_owner_out_or_open_the_api_is_refused(
    tmp_path: Path, value: str
) -> None:
    printed = evaluate(tmp_path, "api_access_cidr", value)

    assert REFUSED in printed
    assert "api_access_cidr must be one public IPv4 address" in printed
    assert "Error: Invalid condition" not in printed  # the message is ours


@needs_terraform
@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("node_instance_type", "t3.large"),
        ("node_instance_type", "t3.xlarge"),
        ("database_instance_class", "db.t4g.small"),
        ("database_instance_class", "db.t4g.medium"),
        ("expected_account_id", "123456789012"),
        ("budget_email", "someone@example.com"),
    ],
)
def test_the_values_the_module_allows_are_accepted(
    tmp_path: Path, name: str, value: str
) -> None:
    assert REFUSED not in evaluate(tmp_path, name, value)


@needs_terraform
@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("node_instance_type", "p5.48xlarge"),
        ("node_instance_type", "t3.2xlarge"),
        ("node_instance_type", "t3.small"),
        ("node_instance_type", ""),
        ("database_instance_class", "db.x2iedn.32xlarge"),
        ("database_instance_class", "db.t4g.large"),
        ("database_instance_class", "db.t3.small"),
        ("expected_account_id", "12345678901"),
        ("expected_account_id", "1234567890123"),
        ("expected_account_id", "12345678901a"),
        ("expected_account_id", ""),
        ("budget_email", "owner-at-example"),
        ("budget_email", ""),
    ],
)
def test_a_value_the_module_does_not_allow_is_refused_with_its_own_sentence(
    tmp_path: Path, name: str, value: str
) -> None:
    printed = evaluate(tmp_path, name, value)

    assert REFUSED in printed
    assert f"{name} must" in printed
    if name.endswith(("_type", "_class")):
        assert "cost ceiling" in printed


@needs_terraform
@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("api_access_cidr", "10.1.2.3/32"),
        ("expected_account_id", "98765432101"),
        ("budget_email", "owner-at-example"),
    ],
)
def test_a_refused_value_of_a_sensitive_variable_is_not_printed_back(
    tmp_path: Path, name: str, value: str
) -> None:
    printed = evaluate(tmp_path, name, value)

    assert REFUSED in printed
    assert value not in printed
