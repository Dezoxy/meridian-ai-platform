"""The kind cluster records who holds it (S075, M2).

One plan step owns the cluster at a time, and until now that was a sentence.
``make up``, ``make deploy`` and ``make down`` now read the ConfigMap
``meridian-cluster-holder`` in ``kube-system`` (``holder``, ``commit`` and
``time``): another holder is named and the command stops before it changes
anything, unless ``TAKE_CLUSTER=1`` is given; ``up`` and ``deploy`` write the
record when they end well; ``make cluster-holder`` prints it. A notice for an
honest mistake, not a lock.

Nothing here touches a cluster. The functions of ``common.sh`` run in bash
against a stub ``kctl`` (and, for the holder's name, a real ``git`` in a scratch
repository); ``up.sh``, ``deploy.sh``, ``down.sh`` and ``holder.sh`` run whole in
a scratch copy of ``infra/kind/`` against stub ``kubectl``, ``kind``, ``docker``,
``helm`` and ``git`` that log every call.
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from certscriptsupport import KIND_DIR, SECONDS
from servicesupport import REPO_ROOT
from test_certificate_deploy import write_stub

MAKEFILE = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
COMMON_SH = (KIND_DIR / "common.sh").read_text(encoding="utf-8")
UP_SH = (KIND_DIR / "up.sh").read_text(encoding="utf-8")
DEPLOY_SH = (KIND_DIR / "deploy.sh").read_text(encoding="utf-8")
KIND_README = (KIND_DIR / "README.md").read_text(encoding="utf-8")
DEVELOPMENT_ENVIRONMENT = (REPO_ROOT / "docs" / "development-environment.md").read_text(
    encoding="utf-8"
)
ROLLBACK_RUNBOOK = (
    REPO_ROOT / "docs" / "operations" / "runbooks" / "rollback.md"
).read_text(encoding="utf-8")
SCRIPTS = ("common.sh", "pins.env", "up.sh", "deploy.sh", "down.sh", "holder.sh")


def smoke_parts() -> list[Path]:
    """Every file under ``smoke.d/``, the parts ``smoke.sh`` sources (S074). A part
    is not run on its own, but it is the text of a script that is: a rule about
    what a script says, or never says, holds for it as it held for ``smoke.sh``
    before the split. A rule about what a script does when it runs (``SCRIPTS``,
    the ``need_tools`` line) is about the scripts alone."""
    return sorted(path for path in (KIND_DIR / "smoke.d").rglob("*") if path.is_file())


def scripts_and_parts() -> list[Path]:
    """Every ``*.sh`` of infra/kind/ and every part of ``smoke.sh``."""
    return [*sorted(KIND_DIR.glob("*.sh")), *smoke_parts()]


ME = "s075-m2"
OTHER = "s075-f1"
TIME = "2026-10-06T12:00:00Z"
COMMIT = "abc1234"
RECORD = f"{OTHER}|{COMMIT}|{TIME}"
CONFIGMAP = "meridian-cluster-holder"
TIME_FORMAT = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
# What the state `changing` means, in the one wording every place shares.
CHANGING_MEANS = "a make up or make deploy is running, or the last one did not end well"

# ── the functions of common.sh, in bash ──────────────────────────────────────

GIT_ENV = {
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "x",
    "GIT_AUTHOR_EMAIL": "x@example.invalid",
    "GIT_COMMITTER_NAME": "x",
    "GIT_COMMITTER_EMAIL": "x@example.invalid",
}
KCTL = r"""
kctl() {
  printf '%s\n' "${*//$'\n'/ }" >>"${CALLS}"
  case "$*" in
    *"get configmap meridian-cluster-holder"*)
      [[ "${RECORD_STATUS}" == 0 ]] || { echo "Error from server: boom" >&2; return 1; }
      printf '%s' "${RECORD}" ;;
    *"create configmap meridian-cluster-holder"*)
      printf '%s' '{"metadata":{"name":"x","creationTimestamp":null}}' ;;
    "-n kube-system apply "*)
      [[ "${APPLY_STATUS}" == 0 ]] || { echo "Error from server: no" >&2; return 1; }
      cat >"${APPLIED}" ;;
  esac
}
"""


def git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
        env={"PATH": os.environ["PATH"], **GIT_ENV},
    )
    return done.stdout.strip()


def scratch_repo(
    tmp_path: Path, branch: str | None = "main", *, detached: bool = False
) -> tuple[Path, str]:
    """A scratch ``repo/infra/kind`` with the scripts, a git repository there (none
    for ``branch`` None) and the short hash of its one commit ('' with no repo)."""
    repo = tmp_path / "repo"
    kind_dir = repo / "infra" / "kind"
    kind_dir.mkdir(parents=True)
    for name in SCRIPTS:
        shutil.copy(KIND_DIR / name, kind_dir / name)
    if branch is None:
        return kind_dir, ""
    git(repo, "init", "-q", "-b", branch)
    git(repo, "commit", "-q", "--allow-empty", "-m", "x")
    short = git(repo, "rev-parse", "--short", "HEAD")
    if detached:
        git(repo, "checkout", "-q", "--detach")
    return kind_dir, short


def run_functions(
    tmp_path: Path,
    body: str,
    *,
    branch: str | None = ME,
    detached: bool = False,
    environment: dict[str, str] | None = None,
    record: str = "",
    record_status: int = 0,
    apply_status: int = 0,
) -> tuple[subprocess.CompletedProcess[str], list[str], str, str]:
    """``body`` in bash after ``common.sh`` of a scratch repository on ``branch``
    and a stub ``kctl`` that answers the record with ``record`` (the way ``kubectl
    --ignore-not-found`` does: nothing when there is none) or fails. Returns the
    process, the ``kctl`` calls, what ``apply`` was given and the commit's short
    hash."""
    kind_dir, short = scratch_repo(tmp_path, branch, detached=detached)
    calls, applied = tmp_path / "calls", tmp_path / "applied.json"
    calls.touch()
    script = "\n".join(
        [
            "set -euo pipefail",
            f'. "{kind_dir}/common.sh"',
            KCTL,
            body,
        ]
    )
    done = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env={
            "PATH": os.environ["PATH"],
            "HOME": str(tmp_path),
            "GIT_CEILING_DIRECTORIES": str(tmp_path.parent),
            "CALLS": str(calls),
            "APPLIED": str(applied),
            "RECORD": record,
            "RECORD_STATUS": str(record_status),
            "APPLY_STATUS": str(apply_status),
            **GIT_ENV,
            **(environment or {}),
        },
        check=False,
        timeout=SECONDS,
    )
    text = applied.read_text(encoding="utf-8") if applied.exists() else ""
    return done, calls.read_text(encoding="utf-8").splitlines(), text, short


# ── the holder's name ────────────────────────────────────────────────────────


def test_the_holder_is_the_current_branch(tmp_path: Path) -> None:
    done, _, _, _ = run_functions(tmp_path, "own_holder_name", branch="s075-m2")

    assert done.returncode == 0, done.stderr
    assert done.stdout == "s075-m2"


def test_a_detached_checkout_is_named_detached_at_its_short_commit(
    tmp_path: Path,
) -> None:
    done, _, _, short = run_functions(tmp_path, "own_holder_name", detached=True)

    assert done.returncode == 0, done.stderr
    assert done.stdout == f"detached@{short}"


def test_cluster_holder_in_the_environment_names_the_holder_before_the_branch(
    tmp_path: Path,
) -> None:
    done, _, _, _ = run_functions(
        tmp_path,
        "own_holder_name",
        environment={"CLUSTER_HOLDER": "main-session"},
    )

    assert done.returncode == 0, done.stderr
    assert done.stdout == "main-session"


@pytest.mark.parametrize(
    "name", ["a", "s075/m2_x.y-z", "A9", "x" * 100], ids=["one", "all", "mixed", "100"]
)
def test_a_cluster_holder_of_the_allowed_characters_and_length_is_accepted(
    tmp_path: Path, name: str
) -> None:
    done, _, _, _ = run_functions(
        tmp_path, "own_holder_name", environment={"CLUSTER_HOLDER": name}
    )

    assert done.returncode == 0, done.stderr
    assert done.stdout == name


@pytest.mark.parametrize(
    "name",
    ["x" * 101, "has space", "a:b", "semi;colon", "tab\there", "line\nbreak", ""],
    ids=["101", "space", "colon", "semicolon", "tab", "newline", "empty"],
)
def test_a_cluster_holder_outside_the_characters_or_length_stops_the_command(
    tmp_path: Path, name: str
) -> None:
    done, _, _, _ = run_functions(
        tmp_path, "own_holder_name", environment={"CLUSTER_HOLDER": name}
    )

    assert done.returncode != 0
    assert done.stdout == ""
    assert "CLUSTER_HOLDER" in done.stderr
    assert "100" in done.stderr


def test_a_branch_name_outside_the_characters_asks_for_a_cluster_holder(
    tmp_path: Path,
) -> None:
    done, _, _, _ = run_functions(tmp_path, "own_holder_name", branch="feat+x")

    assert done.returncode != 0
    assert "CLUSTER_HOLDER" in done.stderr


def test_a_checkout_that_is_not_a_git_repository_asks_for_a_cluster_holder(
    tmp_path: Path,
) -> None:
    done, _, _, _ = run_functions(tmp_path, "own_holder_name", branch=None)

    assert done.returncode != 0
    assert "CLUSTER_HOLDER" in done.stderr


# ── reading the record and deciding ──────────────────────────────────────────


def test_no_record_is_said_in_one_line_and_the_command_goes_on(
    tmp_path: Path,
) -> None:
    done, calls, _, _ = run_functions(tmp_path, 'check_cluster_holder "make up"')

    assert done.returncode == 0, done.stderr
    lines = done.stdout.splitlines()
    assert len(lines) == 1
    assert "no record" in lines[0]
    assert len(calls) == 1
    assert f"-n kube-system get configmap {CONFIGMAP} " in calls[0] + " "


def test_the_same_holder_goes_on_whatever_commit_the_record_has(
    tmp_path: Path,
) -> None:
    done, _, _, _ = run_functions(
        tmp_path,
        'check_cluster_holder "make deploy"',
        record=f"{ME}|0000000|2020-01-01T00:00:00Z",
    )

    assert done.returncode == 0, done.stderr
    assert len(done.stdout.splitlines()) == 1


def test_another_holder_is_named_with_its_commit_and_time_and_the_command_stops(
    tmp_path: Path,
) -> None:
    done, _, _, _ = run_functions(
        tmp_path, 'check_cluster_holder "make deploy"', record=RECORD
    )

    assert done.returncode == 1
    assert done.stdout == ""
    assert done.stderr.count("\n") == 1
    assert done.stderr == (
        f"error: the cluster is held by {OTHER} (commit {COMMIT}, since {TIME}), "
        f"not by {ME}; nothing was changed, and TAKE_CLUSTER=1 in front of the "
        "same command (TAKE_CLUSTER=1 make deploy) takes it\n"
    )


def test_take_cluster_one_takes_it_and_says_whose_cluster_it_is(
    tmp_path: Path,
) -> None:
    done, _, _, _ = run_functions(
        tmp_path,
        'check_cluster_holder "make deploy"',
        record=RECORD,
        environment={"TAKE_CLUSTER": "1"},
    )

    assert done.returncode == 0, done.stderr
    (line,) = done.stdout.splitlines()
    assert OTHER in line
    assert COMMIT in line
    assert TIME in line
    assert "taking" in line


@pytest.mark.parametrize("value", ["0", "", "yes", "true", "11", "1 "])
def test_only_exactly_one_takes_the_cluster(tmp_path: Path, value: str) -> None:
    done, _, _, _ = run_functions(
        tmp_path,
        'check_cluster_holder "make up"',
        record=RECORD,
        environment={"TAKE_CLUSTER": value},
    )

    assert done.returncode == 1
    assert "held by" in done.stderr


def test_a_record_that_cannot_be_read_stops_the_command_and_is_not_no_record(
    tmp_path: Path,
) -> None:
    done, _, _, _ = run_functions(
        tmp_path, 'check_cluster_holder "make up"', record_status=1
    )

    assert done.returncode == 1
    assert "no record" not in done.stdout
    assert "could not read who holds the cluster" in done.stderr
    assert "nothing was changed" in done.stderr
    assert "boom" in done.stderr


def test_a_record_edited_by_hand_cannot_put_control_characters_or_length_on_screen(
    tmp_path: Path,
) -> None:
    hostile = "\x1b[31mred" + "x" * 300
    done, _, _, _ = run_functions(
        tmp_path,
        'check_cluster_holder "make up"',
        record=f"{hostile}|{hostile}|{hostile}",
    )

    assert done.returncode == 1
    assert "\x1b" not in done.stderr
    assert "x" * 101 not in done.stderr


def test_a_record_with_no_holder_is_another_holder_not_no_record(
    tmp_path: Path,
) -> None:
    done, _, _, _ = run_functions(
        tmp_path, 'check_cluster_holder "make up"', record="||"
    )

    assert done.returncode == 1
    assert "held by" in done.stderr


# ── the state of the last run ────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("record", "state"),
    [
        (RECORD, "ok"),
        (f"{RECORD}|", "ok"),
        (f"{RECORD}|ok", "ok"),
        (f"{RECORD}|changing", "changing"),
        (f"{RECORD}|bogus", "changing"),
        (f"{RECORD}|\x1b[31mok", "changing"),
    ],
)
def test_the_state_of_the_record_reads_ok_when_it_is_ok_or_missing_else_changing(
    tmp_path: Path, record: str, state: str
) -> None:
    done, _, _, _ = run_functions(
        tmp_path, 'read_cluster_holder; echo "${holder_last_run}"', record=record
    )

    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == [state]


def test_the_same_holder_goes_on_while_changing_and_is_told_in_one_line(
    tmp_path: Path,
) -> None:
    done, _, _, _ = run_functions(
        tmp_path,
        'check_cluster_holder "make deploy"',
        record=f"{ME}|{COMMIT}|{TIME}|changing",
    )

    assert done.returncode == 0, done.stderr
    (line,) = done.stdout.splitlines()
    assert line == (
        f"==> the cluster is held by {ME}, and the record says changing "
        f"({CHANGING_MEANS}); going on"
    )


def test_another_holder_is_told_a_run_is_going_or_failed_and_is_stopped(
    tmp_path: Path,
) -> None:
    done, _, _, _ = run_functions(
        tmp_path,
        'check_cluster_holder "make down"',
        record=f"{RECORD}|changing",
    )

    assert done.returncode == 1
    assert done.stdout == ""
    assert done.stderr == (
        f"error: the cluster is held by {OTHER} (commit {COMMIT}, since {TIME}), "
        f"whose record says changing ({CHANGING_MEANS}): wait for it, or look at "
        f"what failed before anything is deleted; it is not held by {ME}, nothing "
        "was changed, and TAKE_CLUSTER=1 in front of the same command "
        "(TAKE_CLUSTER=1 make down) takes it\n"
    )


def test_take_cluster_one_takes_a_cluster_that_is_changing_and_says_what_that_means(
    tmp_path: Path,
) -> None:
    done, _, _, _ = run_functions(
        tmp_path,
        'check_cluster_holder "make down"',
        record=f"{RECORD}|changing",
        environment={"TAKE_CLUSTER": "1"},
    )

    assert done.returncode == 0, done.stderr
    (line,) = done.stdout.splitlines()
    assert line == (
        f"==> taking the cluster from {OTHER} (commit {COMMIT}, since {TIME}), "
        f"whose record says changing ({CHANGING_MEANS})"
    )


def test_the_four_places_that_speak_of_changing_share_one_wording(
    tmp_path: Path,
) -> None:
    same, _, _, _ = run_functions(
        tmp_path / "same",
        'check_cluster_holder "make deploy"',
        record=f"{ME}|{COMMIT}|{TIME}|changing",
    )
    other, _, _, _ = run_functions(
        tmp_path / "other",
        'check_cluster_holder "make deploy"',
        record=f"{RECORD}|changing",
    )
    taken, _, _, _ = run_functions(
        tmp_path / "taken",
        'check_cluster_holder "make deploy"',
        record=f"{RECORD}|changing",
        environment={"TAKE_CLUSTER": "1"},
    )
    holder, _ = run_script(
        tmp_path / "holder", "holder.sh", record=f"{RECORD}|changing"
    )

    assert CHANGING_MEANS in same.stdout
    assert CHANGING_MEANS in other.stderr
    assert CHANGING_MEANS in taken.stdout
    assert CHANGING_MEANS in holder.stdout
    # Defined once, in common.sh; the other scripts use it and do not restate it.
    for script in scripts_and_parts():
        name = script.relative_to(KIND_DIR).as_posix()
        count = script.read_text(encoding="utf-8").count("or the last one did not end")
        assert count == (1 if name == "common.sh" else 0), name


def test_a_holder_whose_last_run_ended_well_is_not_said_to_have_failed(
    tmp_path: Path,
) -> None:
    done, _, _, _ = run_functions(
        tmp_path, 'check_cluster_holder "make up"', record=f"{RECORD}|ok"
    )

    assert done.returncode == 1
    assert "did not end well" not in done.stderr


# ── writing the record ───────────────────────────────────────────────────────


def literals_of(create: str) -> dict[str, str]:
    return dict(
        literal.split("=", 1) for literal in re.findall(r"--from-literal=(\S+)", create)
    )


def test_the_record_is_four_values_and_nothing_else(tmp_path: Path) -> None:
    done, calls, applied, short = run_functions(
        tmp_path,
        "record_cluster_holder ok",
        environment={"CLUSTER_HOLDER": "s075-m2"},
    )

    assert done.returncode == 0, done.stderr
    (create,) = [call for call in calls if " create configmap " in f" {call} "]
    keys = re.findall(r"--from-literal=([^=\s]+)=", create)
    values = literals_of(create)
    assert keys == ["holder", "commit", "time", "state"]
    assert values["holder"] == "s075-m2"
    assert values["commit"] == short
    assert values["state"] == "ok"
    assert TIME_FORMAT.match(values["time"])
    assert f"-n kube-system create configmap {CONFIGMAP} " in create + " "
    for forbidden in (str(tmp_path), "@", "http", "ssh"):
        assert forbidden not in create.replace(f"detached@{short}", "")
    assert "creationTimestamp" not in applied
    assert '"metadata"' in applied


def test_the_record_is_applied_to_kube_system_server_side(tmp_path: Path) -> None:
    done, calls, _, _ = run_functions(tmp_path, "record_cluster_holder ok")

    assert done.returncode == 0, done.stderr
    # The two ends of a pipeline log in either order: look at all the calls.
    assert "-n kube-system apply --server-side --force-conflicts -f -" in calls
    assert ME in done.stdout


def test_the_record_written_at_the_start_says_changing_and_says_so(
    tmp_path: Path,
) -> None:
    done, calls, _, _ = run_functions(tmp_path, "record_cluster_holder changing")

    assert done.returncode == 0, done.stderr
    (create,) = [call for call in calls if " create configmap " in f" {call} "]
    assert literals_of(create)["state"] == "changing"
    (line,) = done.stdout.splitlines()
    assert ME in line
    assert "changing" in line


@pytest.mark.parametrize("state", ["", "done", "OK", "changing "])
def test_the_record_is_written_only_with_one_of_its_two_states(
    tmp_path: Path, state: str
) -> None:
    done, calls, _, _ = run_functions(tmp_path, f"record_cluster_holder '{state}'")

    assert done.returncode == 1
    assert "changing or ok" in done.stderr
    assert calls == []


def test_a_record_that_cannot_be_written_stops_the_command(tmp_path: Path) -> None:
    done, _, _, _ = run_functions(tmp_path, "record_cluster_holder ok", apply_status=1)

    assert done.returncode == 1
    assert "could not record" in done.stderr
    assert "the cluster was changed" in done.stderr


def test_a_record_that_cannot_be_written_at_the_start_stops_the_run_there(
    tmp_path: Path,
) -> None:
    done, _, _, _ = run_functions(
        tmp_path, "record_cluster_holder changing", apply_status=1
    )

    assert done.returncode == 1
    assert "could not record" in done.stderr
    assert "this run went no further" in done.stderr
    assert "the cluster was changed" not in done.stderr


# ── the scripts, whole, against stubs ────────────────────────────────────────


def run_script(
    tmp_path: Path,
    script: str,
    *,
    record: str | None = RECORD,
    record_fails: bool = False,
    clusters: str = "meridian",
    branch: str = ME,
    environment: dict[str, str] | None = None,
    kubeconfig: bool = True,
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    """``script`` (``up.sh``, ``deploy.sh``, ``down.sh`` or ``holder.sh``) whole in
    a scratch copy of ``infra/kind/``. The stub ``git`` is on ``branch``; ``kind
    get clusters`` prints ``clusters``; the stub ``kubectl`` knows the cluster and
    answers the record with ``record`` (nothing for None) or fails. Everything
    else a script might change answers 99, so a call that was not meant to
    happen stops the run. Returns the process and the calls, one per line."""
    kind_dir, stubs = tmp_path / "infra" / "kind", tmp_path / "bin"
    kind_dir.mkdir(parents=True)
    stubs.mkdir()
    for name in (*SCRIPTS, "gateways.sh"):  # up.sh sources gateways.sh
        shutil.copy(KIND_DIR / name, kind_dir / name)
    if kubeconfig:
        (kind_dir / "kubeconfig").write_text("stub\n", encoding="utf-8")
    calls = tmp_path / "calls"
    calls.touch()
    log = f'echo "{{name}} $*" >>"{calls}"'
    answer = (
        'echo "Error from server: connection refused" >&2; exit 1'
        if record_fails
        else f"printf '%s' '{record or ''}'"
    )
    write_stub(stubs, "sleep", log.format(name="sleep"))
    write_stub(stubs, "helm", log.format(name="helm"))
    write_stub(
        stubs,
        "git",
        f"{log.format(name='git')}\n"
        'case "$*" in\n'
        f"  *\"rev-parse --abbrev-ref HEAD\"*) echo '{branch}' ;;\n"
        f'  *"rev-parse --short HEAD"*) echo "{COMMIT}" ;;\n'
        '  *) echo "stub git: unexpected $*" >&2; exit 99 ;;\n'
        "esac",
    )
    write_stub(
        stubs,
        "kind",
        f"{log.format(name='kind')}\n"
        'case "$*" in\n'
        f"  \"get clusters\") echo '{clusters}' ;;\n"
        '  "version") echo "kind v0.33.0 go1.25 linux/amd64" ;;\n'
        '  "export kubeconfig"*|"create cluster"*|"delete cluster"*) ;;\n'
        '  *) echo "stub kind: unexpected $*" >&2; exit 99 ;;\n'
        "esac",
    )
    write_stub(
        stubs,
        "docker",
        f"{log.format(name='docker')}\n"
        '[[ "$1" == info ]] || { echo "stub: docker $1 must not run" >&2; exit 99; }',
    )
    write_stub(
        stubs,
        "kubectl",
        f"{log.format(name='kubectl')}\n"
        'case "$*" in\n'
        '  *"get nodes"*) ;;\n'
        # up.sh's read for a cluster made before the operator moved (contract
        # F2): nothing printed is a namespace that is not there.
        '  *"get namespace"*) ;;\n'
        f'  *"get configmap {CONFIGMAP}"*) {answer} ;;\n'
        # Writing the record (S075): `create --dry-run=client | jq | apply`.
        f'  *"create configmap {CONFIGMAP}"*) '
        'echo \'{"metadata":{"name":"x"}}\' ;;\n'
        '  *"-n kube-system apply --server-side"*) cat >/dev/null ;;\n'
        '  *) echo "stub kubectl: unexpected $*" >&2; exit 99 ;;\n'
        "esac",
    )
    done = subprocess.run(
        ["bash", str(kind_dir / script)],
        capture_output=True,
        text=True,
        env={
            "PATH": f"{stubs}:{os.environ['PATH']}",
            "DOCKER_HOST": "unix:///stub.sock",
            "HOME": str(tmp_path),
            **(environment or {}),
        },
        check=False,
        timeout=SECONDS,
    )
    return done, calls.read_text(encoding="utf-8").splitlines()


def changed_something(calls: list[str]) -> bool:
    """True when a call changes a release, the cluster, the engine or the kind
    node (``kind export kubeconfig`` rewrites a gitignored file and is not one)."""
    for call in calls:
        tool, _, rest = call.partition(" ")
        words = set(rest.split())
        if tool == "helm":
            return True
        if tool == "kubectl" and words & {"apply", "create", "delete"}:
            return True
        if tool == "docker" and "build" in words:
            return True
        if tool == "kind" and words & {"create", "delete"}:
            return True
    return False


def record_writes(calls: list[str]) -> list[tuple[int, str]]:
    """The calls that write the record, as (index, state)."""
    return [
        (index, literals_of(call)["state"])
        for index, call in enumerate(calls)
        if call.startswith("kubectl") and f"create configmap {CONFIGMAP} " in call + " "
    ]


def is_record_call(call: str) -> bool:
    return f"create configmap {CONFIGMAP} " in call + " " or (
        "-n kube-system apply --server-side --force-conflicts -f -" in call
    )


def test_deploy_writes_changing_after_the_check_and_before_anything_else(
    tmp_path: Path,
) -> None:
    done, calls = run_script(tmp_path, "deploy.sh", environment={"TAKE_CLUSTER": "1"})

    ((index, state),) = record_writes(calls)
    assert state == "changing"
    # The stub stops the run at its first read of the database: it failed after
    # the record was written, so the record is what is left.
    assert done.returncode != 0
    assert not changed_something([c for c in calls[:index] if not is_record_call(c)])
    assert any("get configmap" in call for call in calls[:index])
    assert any("get database" in call for call in calls[index:])
    assert "ok" not in [state for _, state in record_writes(calls)]


def test_deploy_that_another_holder_refuses_writes_no_record(tmp_path: Path) -> None:
    done, calls = run_script(tmp_path, "deploy.sh")

    assert done.returncode == 1
    assert record_writes(calls) == []


def test_up_writes_changing_after_the_check_and_before_the_first_change(
    tmp_path: Path,
) -> None:
    done, calls = run_script(tmp_path, "up.sh", environment={"TAKE_CLUSTER": "1"})

    ((index, state),) = record_writes(calls)
    assert state == "changing"
    assert done.returncode != 0
    before = [c for c in calls[:index] if not is_record_call(c)]
    assert not changed_something(before)
    assert any("get configmap" in call for call in calls[:index])
    after = [c for c in calls[index + 1 :] if not is_record_call(c)]
    assert any("apply" in call for call in after)


def test_up_that_another_holder_refuses_writes_no_record(tmp_path: Path) -> None:
    done, calls = run_script(tmp_path, "up.sh")

    assert done.returncode == 1
    assert record_writes(calls) == []


def test_up_on_a_machine_with_no_cluster_writes_changing_as_soon_as_it_exists(
    tmp_path: Path,
) -> None:
    _, calls = run_script(
        tmp_path, "up.sh", clusters="No kind clusters found.", kubeconfig=False
    )

    ((index, state),) = record_writes(calls)
    created = next(i for i, c in enumerate(calls) if c.startswith("kind create"))
    assert state == "changing"
    assert created < index
    assert not changed_something(
        [c for c in calls[created + 1 : index] if not is_record_call(c)]
    )


def test_deploy_stops_before_it_changes_anything_when_another_holder_has_the_cluster(
    tmp_path: Path,
) -> None:
    done, calls = run_script(tmp_path, "deploy.sh")

    assert done.returncode == 1
    assert (
        f"the cluster is held by {OTHER} (commit {COMMIT}, since {TIME})" in done.stderr
    )
    assert "TAKE_CLUSTER=1 make deploy" in done.stderr
    assert not changed_something(calls)
    assert not any("get database" in call for call in calls)
    assert any(f"get configmap {CONFIGMAP}" in call for call in calls)


def test_deploy_goes_on_when_the_record_is_taken(tmp_path: Path) -> None:
    done, calls = run_script(tmp_path, "deploy.sh", environment={"TAKE_CLUSTER": "1"})

    assert f"taking the cluster from {OTHER}" in done.stdout
    assert any("get database" in call for call in calls)


def test_deploy_goes_on_when_there_is_no_record(tmp_path: Path) -> None:
    done, calls = run_script(tmp_path, "deploy.sh", record=None)

    assert "no record of who holds the cluster" in done.stdout
    assert any("get database" in call for call in calls)


def test_deploy_goes_on_when_the_checkout_already_holds_the_cluster(
    tmp_path: Path,
) -> None:
    done, calls = run_script(tmp_path, "deploy.sh", record=f"{ME}|{COMMIT}|{TIME}")

    assert f"held by {ME}" in done.stdout
    assert any("get database" in call for call in calls)


def test_deploy_names_the_holder_the_session_chose_not_the_branch(
    tmp_path: Path,
) -> None:
    done, calls = run_script(
        tmp_path,
        "deploy.sh",
        record=f"main-session|{COMMIT}|{TIME}",
        branch="detached-thing",
        environment={"CLUSTER_HOLDER": "main-session"},
    )

    assert "held by main-session" in done.stdout
    assert any("get database" in call for call in calls)
    # The branch is not read for the name; the record's commit is (rev-parse --short).
    assert not any("--abbrev-ref" in call for call in calls if call.startswith("git "))
    ((_, state),) = record_writes(calls)
    assert state == "changing"
    (create,) = [c for c in calls if f"create configmap {CONFIGMAP} " in c + " "]
    assert literals_of(create)["holder"] == "main-session"


def test_up_stops_before_it_changes_anything_when_another_holder_has_the_cluster(
    tmp_path: Path,
) -> None:
    done, calls = run_script(tmp_path, "up.sh")

    assert done.returncode == 1
    assert (
        f"the cluster is held by {OTHER} (commit {COMMIT}, since {TIME})" in done.stderr
    )
    assert "TAKE_CLUSTER=1 make up" in done.stderr
    assert not changed_something(calls)
    assert not any("kind create" in call for call in calls)


def test_up_takes_the_cluster_with_take_cluster_and_goes_on_to_the_namespaces(
    tmp_path: Path,
) -> None:
    done, calls = run_script(tmp_path, "up.sh", environment={"TAKE_CLUSTER": "1"})

    assert f"taking the cluster from {OTHER}" in done.stdout
    assert any("kubectl" in call and "apply" in call for call in calls)


def test_up_on_a_machine_with_no_cluster_creates_it_without_reading_a_record(
    tmp_path: Path,
) -> None:
    _, calls = run_script(
        tmp_path, "up.sh", clusters="No kind clusters found.", kubeconfig=False
    )

    assert any(call.startswith("kind create cluster") for call in calls)
    assert not any(f"get configmap {CONFIGMAP}" in call for call in calls)


def test_down_stops_before_it_deletes_anything_when_another_holder_has_the_cluster(
    tmp_path: Path,
) -> None:
    done, calls = run_script(tmp_path, "down.sh")

    assert done.returncode == 1
    assert (
        f"the cluster is held by {OTHER} (commit {COMMIT}, since {TIME})" in done.stderr
    )
    assert "TAKE_CLUSTER=1 make down" in done.stderr
    assert not any(call.startswith("kind delete") for call in calls)
    assert (tmp_path / "infra" / "kind" / "kubeconfig").exists()


def test_a_refused_down_leaves_no_credentials_file_it_made_itself(
    tmp_path: Path,
) -> None:
    done, calls = run_script(tmp_path, "down.sh", kubeconfig=False)

    assert done.returncode == 1
    assert any(call.startswith("kind export kubeconfig") for call in calls)
    assert not (tmp_path / "infra" / "kind" / "kubeconfig").exists()


def test_down_deletes_the_cluster_when_the_record_is_taken(tmp_path: Path) -> None:
    done, calls = run_script(tmp_path, "down.sh", environment={"TAKE_CLUSTER": "1"})

    assert done.returncode == 0, done.stderr
    assert f"taking the cluster from {OTHER}" in done.stdout
    assert any(call.startswith("kind delete cluster --name meridian") for call in calls)
    assert not (tmp_path / "infra" / "kind" / "kubeconfig").exists()


def test_down_deletes_the_cluster_of_its_own_holder_and_of_no_record(
    tmp_path: Path,
) -> None:
    own, own_calls = run_script(
        tmp_path / "own", "down.sh", record=f"{ME}|{COMMIT}|{TIME}"
    )
    none, none_calls = run_script(tmp_path / "none", "down.sh", record=None)

    assert own.returncode == 0, own.stderr
    assert none.returncode == 0, none.stderr
    assert any(call.startswith("kind delete cluster") for call in own_calls)
    assert any(call.startswith("kind delete cluster") for call in none_calls)


def test_down_stops_at_the_cluster_of_a_run_that_did_not_end_well_of_another_holder(
    tmp_path: Path,
) -> None:
    done, calls = run_script(tmp_path, "down.sh", record=f"{RECORD}|changing")

    assert done.returncode == 1
    assert f"whose record says changing ({CHANGING_MEANS}): wait for it" in done.stderr
    assert "or look at what failed before anything is deleted" in done.stderr
    assert "TAKE_CLUSTER=1 make down" in done.stderr
    assert not any(call.startswith("kind delete") for call in calls)


def test_down_goes_on_for_its_own_holder_after_a_run_that_did_not_end_well(
    tmp_path: Path,
) -> None:
    done, calls = run_script(
        tmp_path, "down.sh", record=f"{ME}|{COMMIT}|{TIME}|changing"
    )

    assert done.returncode == 0, done.stderr
    assert f"the record says changing ({CHANGING_MEANS})" in done.stdout
    assert any(call.startswith("kind delete cluster") for call in calls)
    assert record_writes(calls) == []


def test_down_deletes_the_cluster_of_a_record_that_has_no_state_as_before(
    tmp_path: Path,
) -> None:
    done, calls = run_script(tmp_path, "down.sh", record=f"{ME}|{COMMIT}|{TIME}")

    assert done.returncode == 0, done.stderr
    assert "did not end well" not in done.stdout
    assert any(call.startswith("kind delete cluster") for call in calls)


def test_down_stops_at_a_cluster_that_does_not_answer_as_it_cannot_tell_who_holds_it(
    tmp_path: Path,
) -> None:
    done, calls = run_script(tmp_path, "down.sh", record_fails=True)

    assert done.returncode == 1
    assert "could not read who holds the cluster" in done.stderr
    assert "TAKE_CLUSTER=1 make down" in done.stderr
    assert not any(call.startswith("kind delete") for call in calls)
    assert (tmp_path / "infra" / "kind" / "kubeconfig").exists()


def test_down_removes_a_cluster_that_does_not_answer_when_it_is_taken_and_says_so(
    tmp_path: Path,
) -> None:
    done, calls = run_script(
        tmp_path, "down.sh", record_fails=True, environment={"TAKE_CLUSTER": "1"}
    )

    assert done.returncode == 0, done.stderr
    assert "cannot be read" in done.stdout
    assert any(call.startswith("kind delete cluster") for call in calls)


def test_down_with_no_cluster_reads_no_record(tmp_path: Path) -> None:
    done, calls = run_script(
        tmp_path, "down.sh", clusters="No kind clusters found.", kubeconfig=False
    )

    assert done.returncode == 0, done.stderr
    assert not any(call.startswith("kubectl") for call in calls)
    assert not any(call.startswith("kind delete") for call in calls)


# ── make cluster-holder ──────────────────────────────────────────────────────


def test_cluster_holder_prints_the_four_values_and_a_record_without_state_is_ok(
    tmp_path: Path,
) -> None:
    done, calls = run_script(tmp_path, "holder.sh")

    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == [
        f"holder: {OTHER}",
        f"commit: {COMMIT}",
        f"time:   {TIME}",
        "state:  ok",
    ]
    assert not changed_something(calls)


def test_cluster_holder_says_in_a_sentence_when_the_last_run_did_not_end_well(
    tmp_path: Path,
) -> None:
    done, calls = run_script(tmp_path, "holder.sh", record=f"{RECORD}|changing")

    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == [
        f"holder: {OTHER}",
        f"commit: {COMMIT}",
        f"time:   {TIME}",
        f"state:  changing ({CHANGING_MEANS}; look at what failed before anything "
        "is deleted)",
    ]
    assert not changed_something(calls)


def test_cluster_holder_says_when_there_is_no_record(tmp_path: Path) -> None:
    done, _ = run_script(tmp_path, "holder.sh", record=None)

    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == ["no record of who holds the cluster"]


def test_cluster_holder_says_when_there_is_no_cluster(tmp_path: Path) -> None:
    done, calls = run_script(
        tmp_path, "holder.sh", clusters="No kind clusters found.", kubeconfig=False
    )

    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == ["no kind cluster meridian"]
    assert not any(call.startswith("kubectl") for call in calls)


def test_cluster_holder_fails_when_the_record_cannot_be_read(tmp_path: Path) -> None:
    done, _ = run_script(tmp_path, "holder.sh", record_fails=True)

    assert done.returncode == 1
    assert "could not read who holds the cluster" in done.stderr


def test_cluster_holder_is_a_phony_target_with_a_help_line() -> None:
    phony = next(
        line for line in MAKEFILE.splitlines() if line.startswith(".PHONY:")
    ).split()

    assert "cluster-holder" in phony
    assert re.search(r"^## cluster-holder +\S", MAKEFILE, re.MULTILINE)
    assert re.search(r"^cluster-holder:\n\tinfra/kind/holder\.sh$", MAKEFILE, re.M)


# ── where the scripts call it ────────────────────────────────────────────────


def position(text: str, needle: str) -> int:
    assert text.count(needle) == 1, f"{needle!r} should appear once"
    return text.index(needle)


def test_up_checks_the_record_once_the_cluster_exists_and_before_it_changes_one() -> (
    None
):
    exists = UP_SH[position(UP_SH, "create_cluster() {") :]
    exists = exists[: exists.index("\n}\n")]

    assert exists.index("kind export kubeconfig") < exists.index(
        'check_cluster_holder "make up"'
    )
    assert exists.index('check_cluster_holder "make up"') < exists.index("return")
    assert exists.index("kind create cluster") > exists.index("return")
    assert 'check_cluster_holder "make up"' not in UP_SH.replace(exists, "")


def test_up_writes_the_record_last_after_every_wait_that_can_fail() -> None:
    waits = UP_SH.index("wait --for=condition=Available deployment")
    # S021 (Y2c): the add-on's block follows the record, so a refusal leaves `ok`.
    after = UP_SH.split("\nrecord_cluster_holder ok\n")[1].rstrip().splitlines()
    code = [line for line in after if line and line[0] != "#"]
    assert UP_SH.count("record_cluster_holder ok") == 1
    assert waits < UP_SH.index("record_cluster_holder ok")
    assert code[0] == "if identity_on; then" and code[-1].startswith('log "done')


def test_up_writes_changing_in_both_branches_of_create_cluster() -> None:
    body = UP_SH[position(UP_SH, "create_cluster() {") :]
    body = body[: body.index("\n}\n")]

    assert UP_SH.count("record_cluster_holder changing") == 2
    assert body.count("record_cluster_holder changing") == 2
    # The cluster that exists: after the check, before the return.
    assert body.index('check_cluster_holder "make up"') < body.index(
        "record_cluster_holder changing"
    )
    assert body.index("record_cluster_holder changing") < body.index("return")
    # The cluster made here: once kind create has returned, which waits for it.
    assert body.index("kind create cluster") < body.rindex(
        "record_cluster_holder changing"
    )


def test_deploy_checks_the_record_after_the_cluster_is_known_and_before_anything_else(
    tmp_path: Path,
) -> None:
    known = position(DEPLOY_SH, "need_cluster\n")

    assert known < position(DEPLOY_SH, 'check_cluster_holder "make deploy"')
    assert position(DEPLOY_SH, 'check_cluster_holder "make deploy"') < position(
        DEPLOY_SH, "\nrequire_database\n"
    )


def test_deploy_writes_the_record_after_the_last_wait_and_before_it_says_done() -> None:
    tail = DEPLOY_SH.rstrip().splitlines()[-3:]

    assert tail[0] == "wait_for_token_window"
    assert tail[1] == "record_cluster_holder ok"
    assert tail[2] == 'log "done. Next: make demo"'
    assert DEPLOY_SH.count("record_cluster_holder ok") == 1


def test_deploy_writes_changing_right_after_the_check_and_before_a_prerequisite() -> (
    None
):
    check = position(DEPLOY_SH, 'check_cluster_holder "make deploy"')

    assert DEPLOY_SH.count("record_cluster_holder changing") == 1
    assert check < DEPLOY_SH.index("record_cluster_holder changing")
    assert DEPLOY_SH.index("record_cluster_holder changing") < DEPLOY_SH.index(
        "\nrequire_database\n"
    )


def test_the_record_is_named_in_common_sh_alone_and_its_functions_are_there_once() -> (
    None
):
    for name in (
        "check_cluster_holder",
        "record_cluster_holder",
        "read_cluster_holder",
    ):
        assert COMMON_SH.count(f"\n{name}() {{") == 1
    for script in scripts_and_parts():
        code = [
            line
            for line in script.read_text(encoding="utf-8").splitlines()
            if not line.lstrip().startswith("#")
        ]
        reads = any(CONFIGMAP in line for line in code)
        assert not reads or script.relative_to(KIND_DIR).as_posix() == "common.sh"


# ── the documents ────────────────────────────────────────────────────────────


def section(text: str, heading: str) -> str:
    start = text.index(heading)
    end = text.find("\n## ", start + len(heading))
    return text[start : end if end != -1 else len(text)]


def test_the_readme_has_a_section_that_says_it_is_a_notice_and_not_a_lock() -> None:
    body = " ".join(section(KIND_README, "\n## Who holds the cluster").split())

    assert "not a lock" in body
    assert "same second" in body
    assert "TAKE_CLUSTER=1" in body
    assert "CLUSTER_HOLDER" in body
    assert CONFIGMAP in body
    assert "make demo" in body
    assert "make smoke" in body
    assert "make cluster-holder" in body


def test_the_readme_says_what_the_record_shows_after_a_run_that_failed() -> None:
    body = " ".join(section(KIND_README, "\n## Who holds the cluster").split())

    assert "changing" in body
    assert CHANGING_MEANS in body
    assert "wait for it, or look at what failed before anything is deleted" in body
    # The one case nothing protects: a cluster from before the record.
    assert "nothing protects" in body
    assert "no record" in body


def test_the_runbook_for_a_failed_deploy_says_to_look_before_deleting() -> None:
    runbook = " ".join(ROLLBACK_RUNBOOK.split())

    assert "make cluster-holder" in runbook
    assert "changing" in runbook
    assert "before deleting" in runbook


def test_the_development_environment_says_how_a_session_names_itself() -> None:
    assert "CLUSTER_HOLDER" in DEVELOPMENT_ENVIRONMENT
    assert "TAKE_CLUSTER=1" in DEVELOPMENT_ENVIRONMENT
    assert "make cluster-holder" in DEVELOPMENT_ENVIRONMENT
    assert "changing" in DEVELOPMENT_ENVIRONMENT
