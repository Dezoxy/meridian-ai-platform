"""``init_with_backend`` of ``infra/terraform/planguard.sh`` (S020).

A wrapper whose module keeps its state in a remote backend gives init the
backend block's settings, one ``key=value`` each, where the wrapper of the local
state gives the path of a file. The function is run here by a tiny wrapper of
this file's own, which sources ``common.sh`` and ``planguard.sh`` and sets only
what the function reads: the module's directory and README, and the words of the
refusal of another workspace. It sets none of the globals of the local state
(``LOCAL_STATE_GLOBALS``), and runs under ``set -u``, so a read of one of them
would end the run. A stand-in ``terraform`` records its arguments, one to a
NUL-ended record, and answers as a test says.

What the local state's wrapper does with the same function is held by
``test_planguard_script.py`` (the whole argument list ``aws.sh`` gives) and
``test_aws_script.py``. Every host here is made up, under ``example``.
"""

import os
import subprocess
from pathlib import Path

import pytest
from awsscriptsupport import TERRAFORM_DIR

LOCAL_STATE_GLOBALS = ("STATE_DIR_UNDER_HOME", "STATE_FILE_NAME", "ENVIRONMENT_WORDS")
WORDS = "Terraform would keep that workspace's state in a place the test names"
NOISE_HOST = "stexample.blob.core.windows.net"
END_OF_CALL = "--end-of-call--"

STUB_TERRAFORM = f"""#!/usr/bin/env bash
here="$(dirname "${{BASH_SOURCE[0]}}")"
printf '%s\\0' "$@" "{END_OF_CALL}" >>"${{here}}/arguments"
echo "stub init: reaching {NOISE_HOST}:443"
[[ ! -e "${{here}}/init-fails" ]] || exit 3
exit 0
"""

WRAPPER = f"""set -euo pipefail
. "{TERRAFORM_DIR}/common.sh"
. "{TERRAFORM_DIR}/planguard.sh"
MODULE_DIR="$1"
MODULE_REL=infra/terraform/example
MODULE_README=infra/terraform/example/README.md
WORKSPACE_WORDS="{WORDS}"
shift
run_clean() {{
  local kind="$1"
  shift
  env -i "PATH=${{PATH}}" "HOME=${{HOME}}" "$@"
}}
init_with_backend "$@"
"""


class Rig:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.module = root / "module"
        self.stubs = root / "stubs"
        self.module.mkdir()
        self.stubs.mkdir()
        (self.stubs / "terraform").write_text(STUB_TERRAFORM)
        (self.stubs / "terraform").chmod(0o755)
        (root / "wrapper.sh").write_text(WRAPPER)

    def run(self, *settings: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(self.root / "wrapper.sh"), str(self.module), *settings],
            capture_output=True,
            text=True,
            env={"PATH": f"{self.stubs}:{os.environ['PATH']}", "HOME": str(self.root)},
            cwd=self.root,
            stdin=subprocess.DEVNULL,
            timeout=60,
            check=False,
        )

    def init_calls(self) -> list[list[str]]:
        """The arguments of each terraform call, in order (the stub ends each
        call's record with ``END_OF_CALL``)."""
        path = self.stubs / "arguments"
        if not path.exists():
            return []
        words = path.read_text().split("\0")[:-1]
        calls: list[list[str]] = [[]]
        for word in words:
            if word == END_OF_CALL:
                calls.append([])
            else:
                calls[-1].append(word)
        return calls[:-1]

    def with_workspace(self, text: str) -> None:
        (self.module / ".terraform").mkdir(exist_ok=True)
        (self.module / ".terraform" / "environment").write_text(text)


@pytest.fixture(name="rig")
def rig_of(tmp_path: Path) -> Rig:
    return Rig(tmp_path)


def test_the_rig_wrapper_sets_none_of_the_local_state_globals() -> None:
    assert [name for name in LOCAL_STATE_GLOBALS if name in WRAPPER] == []


def test_no_setting_is_an_internal_error_and_no_program_runs(rig: Rig) -> None:
    done = rig.run()

    assert done.returncode == 1
    assert done.stderr.splitlines()[-1].startswith("error: internal error: ")
    assert rig.init_calls() == []


def test_two_settings_arrive_as_two_backend_config_arguments_in_order(
    rig: Rig,
) -> None:
    done = rig.run("first_key=one", "second_key=two")

    assert done.returncode == 0, done.stdout + done.stderr
    assert rig.init_calls() == [
        [
            f"-chdir={rig.module}",
            "init",
            "-no-color",
            "-input=false",
            "-reconfigure",
            "-lockfile=readonly",
            "-backend-config=first_key=one",
            "-backend-config=second_key=two",
        ]
    ]


def test_a_setting_with_a_space_or_a_glob_character_stays_one_argument(
    rig: Rig,
) -> None:
    done = rig.run("key=a b*", "other=")

    assert done.returncode == 0, done.stdout + done.stderr
    (arguments,) = rig.init_calls()
    assert arguments[-2:] == ["-backend-config=key=a b*", "-backend-config=other="]


def test_a_failing_init_dies_with_the_sentence_and_does_not_look_at_the_workspace(
    rig: Rig,
) -> None:
    (rig.stubs / "init-fails").write_text("")
    rig.with_workspace("w1")

    done = rig.run("key=value")

    assert done.returncode == 1
    assert done.stderr.splitlines()[-1] == "error: terraform init failed"
    assert "workspace" not in done.stderr


def test_another_workspace_is_refused_after_a_successful_init(rig: Rig) -> None:
    rig.with_workspace("w1")

    done = rig.run("key=value")

    assert done.returncode == 1
    assert len(rig.init_calls()) == 1  # the init ran; the check came after it
    assert done.stderr.splitlines()[-1] == (
        "error: the module's directory is not on Terraform's default workspace, "
        f"or .terraform/environment cannot be read: {WORDS}, and a removal would "
        "find it empty. Get back with: terraform -chdir=infra/terraform/example "
        "workspace select default (infra/terraform/example/README.md, State)"
    )


@pytest.mark.parametrize("text", [None, "default", " default \n"])
def test_the_default_workspace_passes(rig: Rig, text: str | None) -> None:
    if text is not None:
        rig.with_workspace(text)

    done = rig.run("key=value")

    assert done.returncode == 0, done.stdout + done.stderr
    assert done.stdout.splitlines()[0] == "==> terraform init"


def test_what_init_prints_passes_through_redact(rig: Rig) -> None:
    done = rig.run("key=value")

    assert done.returncode == 0, done.stdout + done.stderr
    assert "stub init: reaching <host>" in done.stdout
    assert NOISE_HOST not in done.stdout + done.stderr
