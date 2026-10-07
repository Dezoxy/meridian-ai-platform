"""What the tests of ``infra/terraform/aws.sh`` share (S036, S079).

The script is copied, with ``common.sh`` and ``planguard.sh``, into a temporary
``infra/terraform`` tree that holds a stand-in module directory and is a git
repository of its own, so it runs against nothing real: its module directory and
its local file are resolved from its own location. Stub ``terraform`` and
``aws`` programs come first on ``PATH``; they record every call and answer from
a file the test writes beside them (``stub.env``): the script runs both with an
environment of its own choosing, so a variable the test sets in the process
would never reach them. No credential, account, cluster or network is
involved. Every account number, address and host here is made up: twelve
identical digits, a documentation address and ``example.com``.

The stub ``terraform`` prints a line with an ARN and an account number, and a
line with the identifiers a plan of instances prints, on every call, so a test
that finds none of them in the script's output shows ``redact`` at work on what
Terraform says.

The script drives two modules with the same commands (``plan``, ``apply`` and
``destroy`` take no word for the managed module and the word ``aws-kubeadm`` for
the self-managed one). ``MODULES`` is the table of what differs, written out
here once more so that a literal changed in the script has to be changed here on
purpose, and the ``module`` fixture runs a test once for each row.
"""

import hashlib
import os
import pty
import re
import select
import shlex
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from servicesupport import REPO_ROOT

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
QUESTION = "Only 'yes' will be accepted to confirm."
PLAN_MAX_AGE_SECONDS = 30 * 60

# What a plan of instances prints and a plan of the managed module did not: the
# stub terraform prints each of these on every call, and no test may find one in
# what the script prints.
INSTANCE_ID = "i-0123456789abcdef0"
IMAGE_ID = "ami-0123456789abcdef0"
NETWORK_IDS = (
    "vpc-0123456789abcdef0",
    "subnet-0123456789abcdef0",
    "sg-0123456789abcdef0",
    "rtb-0123456789abcdef0",
    "igw-0123456789abcdef0",
    "eipalloc-0123456789abcdef0",
    "eipassoc-0123456789abcdef0",
    "eni-0123456789abcdef0",
    "vol-0123456789abcdef0",
)
PRIVATE_HOST = "ip-10-0-1-23.eu-central-1.compute.internal"
PUBLIC_HOST = "ec2-198-51-100-23.eu-central-1.compute.amazonaws.com"
PROFILE_ARN = f"arn:aws:iam::{NOISE}:instance-profile/stub-node"
PARAMETER_ARN = f"arn:aws:ssm:eu-central-1:{NOISE}:parameter/stub/join-command"
INSTANCE_SHAPES = (
    INSTANCE_ID,
    IMAGE_ID,
    *NETWORK_IDS,
    PRIVATE_HOST,
    PUBLIC_HOST,
    "198.51.100.23",
    "10.0.1.23",
    PROFILE_ARN,
    PARAMETER_ARN,
)


@dataclass(frozen=True)
class Module:
    """What the script keeps for one module, as its sentences and files name it."""

    word: str  # after the sub-command; empty for the managed module
    name: str  # the module's name in the saved plan's record
    directory: str  # under infra/terraform
    plan_file: str
    meta_file: str
    state_directory: str  # under the caller's home
    state_file: str
    plan_command: str  # how the script's sentences tell the owner to plan again
    apply_command: str
    destroy_command: str
    readme: str  # the README the sentences on the state and the removal name


MANAGED = Module(
    word="",
    name="aws",
    directory="aws",
    plan_file="aws.tfplan",
    meta_file="aws.tfplan.meta",
    state_directory=".local/state/meridian-aws",
    state_file="aws.tfstate",
    plan_command="make aws-plan",
    apply_command="make aws-apply",
    destroy_command="make aws-destroy",
    readme="infra/terraform/aws/README.md",
)
SELF_MANAGED = Module(
    word="aws-kubeadm",
    name="aws-kubeadm",
    directory="aws-kubeadm",
    plan_file="aws-kubeadm.tfplan",
    meta_file="aws-kubeadm.tfplan.meta",
    state_directory=".local/state/meridian-aws-kubeadm",
    state_file="aws-kubeadm.tfstate",
    plan_command="infra/terraform/aws.sh plan aws-kubeadm",
    apply_command="infra/terraform/aws.sh apply aws-kubeadm",
    destroy_command="infra/terraform/aws.sh destroy aws-kubeadm",
    readme="infra/terraform/aws-kubeadm/README.md",
)
MODULES = (MANAGED, SELF_MANAGED)

# A test that is about one module only (a sentence of the validate door, a text
# of the managed module's own files) runs once, on that module.
only_the_managed_module = pytest.mark.parametrize(
    "module", [MANAGED], ids=[MANAGED.directory]
)

only_the_self_managed_module = pytest.mark.parametrize(
    "module", [SELF_MANAGED], ids=[SELF_MANAGED.directory]
)

STUB_TERRAFORM = (
    r"""#!/usr/bin/env bash
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
  echo "  stub instance shapes: @SHAPES@"
fi
case "${args[0]}" in
  fmt)
    [[ "${STUB_FMT_STATUS:-0}" == 0 ]] || echo "main.tf"
    exit "${STUB_FMT_STATUS:-0}" ;;
  init)
    # What the real init does: it downloads the providers into Terraform's data
    # directory, which is .terraform in the module's directory unless TF_DATA_DIR
    # names another. Only a test that asks for it (STUB_INIT_WRITES_DATA_DIR=1)
    # gets the directory made, so that the other tests see what they always saw.
    if [[ "${STUB_INIT_WRITES_DATA_DIR:-0}" == 1 ]]; then
      mkdir -p "${TF_DATA_DIR:-${chdir}/.terraform}/providers"
    fi
    exit 0 ;;
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
).replace("@SHAPES@", " ".join(INSTANCE_SHAPES))

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
# The four variables each module is given, and the script's local file holds the
# values of: both modules declare exactly these without a default.
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
    row: Module = MANAGED

    @property
    def script(self) -> Path:
        return self.root / "infra" / "terraform" / "aws.sh"

    @property
    def module(self) -> Path:
        return self.root / "infra" / "terraform" / self.row.directory

    @property
    def plan_file(self) -> str:
        return self.row.plan_file

    @property
    def meta_file(self) -> str:
        return self.row.meta_file

    @property
    def plan_command(self) -> str:
        return self.row.plan_command

    @property
    def apply_command(self) -> str:
        return self.row.apply_command

    @property
    def destroy_command(self) -> str:
        return self.row.destroy_command

    @property
    def local_file(self) -> Path:
        return self.root / "infra" / "terraform" / "local.env-aws"

    @property
    def state_dir(self) -> Path:
        return self.root / self.row.state_directory

    @property
    def state_path(self) -> Path:
        return self.state_dir / self.row.state_file

    @property
    def variables_file(self) -> Path:
        return TERRAFORM_DIR / self.row.directory / "variables.tf"

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
        """The script with these words exactly."""
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

    def words(self, subcommand: str) -> list[str]:
        """The sub-command and this tree's module word, if it has one."""
        if not subcommand:
            return []
        return [subcommand, *([self.row.word] if self.row.word else [])]

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
        command = self.command(*self.words(subcommand), caller_umask=caller_umask)
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


def make_tree(root: Path, row: Module = MANAGED) -> Tree:
    """The stand-in tree under ``root`` (which must exist and be empty), with the
    one module's directory the row names."""
    terraform_dir = root / "infra" / "terraform"
    (terraform_dir / row.directory).mkdir(parents=True)
    for name in ("aws.sh", "common.sh", "planguard.sh"):
        shutil.copy2(TERRAFORM_DIR / name, terraform_dir / name)
    # The real ignore file, so that what the script's checks see as "committed"
    # is what the repository's own patterns say.
    shutil.copy2(REPO_ROOT / ".gitignore", root / ".gitignore")
    (terraform_dir / row.directory / "main.tf").write_text("# a stand-in module\n")
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
    made = Tree(root, env, stub_env, row)
    made.write_stub_env({})
    return made


# The two fixtures every test file of the script registers under these names:
#     module_fixture = pytest.fixture(
#         params=MODULES, ids=MODULE_IDS, name="module"
#     )(each_module)
#     tree_fixture = pytest.fixture(name="tree")(tree_of)
# (a fixture imported by its own name is a redefinition for the linter).
MODULE_IDS = [row.directory for row in MODULES]


def each_module(request: pytest.FixtureRequest) -> Module:
    """Each module the script drives, in turn."""
    return request.param


def tree_of(tmp_path: Path, module: Module) -> Tree:
    """The stand-in tree of the module the test runs on."""
    return make_tree(tmp_path, module)


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
    module_name: str | None = None,
) -> Tree:
    """A saved plan with the record ``plan`` leaves beside it: this tree's module
    (or the name given, to write the other module's), this tree's commit (or the
    one given), the time it was made (or the text given, to write a time the
    script must refuse) and the SHA-256 of the plan file."""
    plan = b"plan"
    (tree.module / tree.plan_file).write_bytes(plan)
    commit = commit or git(tree.root, "rev-parse", "HEAD")
    made = time_text if time_text is not None else str(int(time.time()) - age_seconds)
    digest = hashlib.sha256(plan).hexdigest()
    named = module_name if module_name is not None else tree.row.name
    (tree.module / tree.meta_file).write_text(
        f"module={named}\ncommit={commit}\ntime={made}\nsha256={digest}\n"
    )
    return tree


def everything_printed(done: subprocess.CompletedProcess[str]) -> str:
    return done.stdout + done.stderr


def assert_no_identifier_printed(done: subprocess.CompletedProcess[str]) -> None:
    printed = everything_printed(done)
    for private in (
        PINNED,
        OTHER,
        NOISE,
        ENDPOINT_CIDR,
        BUDGET_EMAIL,
        *INSTANCE_SHAPES,
    ):
        assert private not in printed


# What plan, apply and removal refuse, with a saved plan in place and, for the
# removal, a terminal, so that the refusal under test is the one that is reached.
REFUSING = ("plan", "apply", "destroy")


def run_refusing(tree: Tree, subcommand: str, **env: str):
    with_saved_plan(tree)
    return tree.run(subcommand, terminal=subcommand == "destroy", **env)


def assert_refused(tree: Tree, done, sentence: str, subcommand: str) -> None:
    assert done.returncode == 1, everything_printed(done)
    assert sentence in everything_printed(done)
    assert tree.terraform_calls(subcommand) == []
    assert_no_identifier_printed(done)


def assert_plan_refused_and_dropped(tree: Tree, done, sentence: str) -> None:
    assert done.returncode == 1, everything_printed(done)
    assert sentence in done.stderr
    assert tree.plan_command in done.stderr
    assert tree.terraform_calls("apply") == []
    # The plan is judged last, so the identity call came first and was made
    # once: that is what pins the order (see the test of the order below).
    assert len(tree.aws_calls()) == 1
    assert not (tree.module / tree.plan_file).exists()
    assert not (tree.module / tree.meta_file).exists()


def variable_block(variables_file: Path, name: str) -> str:
    """The text of the ``variable`` block of that name in a module's file."""
    text = variables_file.read_text(encoding="utf-8")
    declared = re.split(r'^variable "(\w+)" \{$', text, flags=re.MULTILINE)
    return dict(zip(declared[1::2], declared[2::2], strict=True))[name]
