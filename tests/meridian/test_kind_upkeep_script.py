"""`make gateway-upkeep` runs the gateway's upkeep command on kind as a Job (S066).

``infra/kind/upkeep.sh`` turns the words of ``ARGS`` into the arguments of one
Job of the Meridian chart, applies it outside the release, waits for it and
exits with its verdict. Nothing here touches a cluster: the script runs in a
scratch copy of ``infra/kind/`` with stub ``kubectl`` and ``helm`` that log every
call, and the stub ``helm`` renders the real chart for ``template``, so what the
cluster would be sent is the chart's own Job. The chart's refusals and the Job's
shape are in ``test_helm_upkeep.py``.
"""

import base64
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from servicesupport import REPO_ROOT

KIND_DIR = REPO_ROOT / "infra" / "kind"
CHART_DIR = REPO_ROOT / "infra" / "helm" / "meridian"
UPKEEP_SH = (KIND_DIR / "upkeep.sh").read_text(encoding="utf-8")
COMMON_SH = (KIND_DIR / "common.sh").read_text(encoding="utf-8")
DEPLOY_SH = (KIND_DIR / "deploy.sh").read_text(encoding="utf-8")
MAKEFILE = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
SECONDS = 60
TAG = "0123456789ab"
SECRET = "gateway-upkeep-db"  # noqa: S105 (a Secret name, not a password)
SEPARATOR = "\x1f"
# What the stub Secret holds as the connection string: synthetic, under
# .invalid, and it says what it is. It must never reach any output or any call.
CONNECTION = "postgresql://gateway_upkeep:not-a-credential@db.invalid/meridian"
RELEASE_VALUES = json.dumps({"image": {"repository": "meridian", "tag": TAG}})
SECRET_STATES = {
    "present": json.dumps(
        {"data": {"uri": base64.b64encode(CONNECTION.encode()).decode()}}
    ),
    "empty-key": json.dumps({"data": {"uri": ""}}),
    "no-key": json.dumps({"data": {"other": "eA=="}}),
    "no-data": json.dumps({}),
}
NOT_FOUND = 'echo "Error from server (NotFound): secrets \\"x\\" not found" >&2; exit 1'
OUTPUT = (
    "found 2 open reservations older than 15 minutes\n"
    "attempt 0f8fad5b-d9cb-469f-a165-70867728950e tenant claims-triage\n"
)
REFUSAL = "ERROR GU203 the tenant has no counter for the current period\n"


def write_stub(directory: Path, name: str, body: str) -> None:
    path = directory / name
    path.write_text(f"#!/usr/bin/env bash\n{body}\n", encoding="utf-8")
    path.chmod(0o755)


def make_scratch(tmp_path: Path) -> tuple[Path, Path]:
    """A scratch ``infra/`` with the scripts under test, kind's values and the
    real chart, and a ``bin`` of stubs that are not yet written. A test that runs
    the script twice gets the same scratch both times."""
    kind_dir = tmp_path / "infra" / "kind"
    (kind_dir / "values").mkdir(parents=True, exist_ok=True)
    for name in ("upkeep.sh", "common.sh", "pins.env"):
        shutil.copy(KIND_DIR / name, kind_dir / name)
    shutil.copy(KIND_DIR / "values" / "meridian.yaml", kind_dir / "values")
    (tmp_path / "infra" / "helm").mkdir(exist_ok=True)
    chart = tmp_path / "infra" / "helm" / "meridian"
    if not chart.exists():
        chart.symlink_to(CHART_DIR)
    (kind_dir / "kubeconfig").write_text("stub\n", encoding="utf-8")
    (tmp_path / "bin").mkdir(exist_ok=True)
    return kind_dir, tmp_path / "bin"


def run_upkeep(
    tmp_path: Path,
    args: str | None,
    *,
    role_state: str = "present",
    job: str = "succeeded",
    output: str = OUTPUT,
    release: str | None = RELEASE_VALUES,
) -> subprocess.CompletedProcess[str]:
    """upkeep.sh whole in a scratch copy with ``ARGS`` set to ``args`` (unset
    for ``None``). The stub ``kubectl`` knows the cluster, the role's Secret in
    the state ``role_state`` names (``SECRET_STATES``, or ``missing``) and the Job
    in the state ``job`` (``succeeded``, ``failed`` or ``running``), whose log is
    ``output``;
    it saves what ``apply`` reads to ``applied-N.yaml``. The stub ``helm`` answers
    ``get values`` with ``release`` (an error for ``None``) and runs the real
    chart for ``template``. ``sleep`` only logs, so a wait costs nothing."""
    kind_dir, stubs = make_scratch(tmp_path)
    calls = tmp_path / "calls"
    calls.touch()
    log = f'printf \'%s{SEPARATOR}\' {{name}} "$@" >>"{calls}"; echo >>"{calls}"'
    answer = (
        NOT_FOUND
        if role_state == "missing"
        else f"printf '%s' '{SECRET_STATES[role_state]}'"
    )
    values_answer = "exit 1" if release is None else f"printf '%s' '{release}'"
    job_json = {
        "succeeded": {
            "status": {"conditions": [{"type": "Complete", "status": "True"}]}
        },
        "failed": {"status": {"conditions": [{"type": "Failed", "status": "True"}]}},
        "running": {"status": {}},
    }[job]
    real_helm = shutil.which("helm")
    assert real_helm, "helm is not on PATH; the chart tests need it and never skip"
    (tmp_path / "output.txt").write_text(output, encoding="utf-8")
    write_stub(stubs, "sleep", log.format(name="sleep"))
    write_stub(
        stubs,
        "helm",
        f"{log.format(name='helm')}\n"
        'case "$*" in\n'
        f'  *"get values"*) {values_answer} ;;\n'
        f'  *" template "*) exec "{real_helm}" "$@" ;;\n'
        '  *) echo "stub helm: unexpected $*" >&2; exit 99 ;;\n'
        "esac",
    )
    write_stub(
        stubs,
        "kubectl",
        f"{log.format(name='kubectl')}\n"
        'case "$*" in\n'
        '  *"get nodes"*) ;;\n'
        f'  *"get secret {SECRET} -o json"*) {answer} ;;\n'
        f"  *\"get job meridian-upkeep-\"*) printf '%s' '{json.dumps(job_json)}' ;;\n"
        f'  *"logs job/meridian-upkeep-"*) cat "{tmp_path / "output.txt"}" ;;\n'
        '  *"apply --server-side --force-conflicts -f -"*)\n'
        f'    cat >"{tmp_path}/applied-$(find "{tmp_path}" -maxdepth 1 '
        f'-name "applied-*" | wc -l).yaml" ;;\n'
        '  *) echo "stub kubectl: unexpected $*" >&2; exit 99 ;;\n'
        "esac",
    )
    env = {
        "PATH": f"{stubs}:{os.environ['PATH']}",
        "HOME": str(tmp_path),
    }
    if args is not None:
        env["ARGS"] = args
    return subprocess.run(
        ["bash", str(kind_dir / "upkeep.sh")],
        capture_output=True,
        text=True,
        env=env,
        cwd=tmp_path,
        check=False,
        timeout=SECONDS,
    )


def calls_of(tmp_path: Path) -> list[list[str]]:
    """The calls the stubs logged: each is ``[command, argument, ...]``."""
    text = (tmp_path / "calls").read_text(encoding="utf-8")
    return [line.split(SEPARATOR)[:-1] for line in text.splitlines() if line]


def commands_of(tmp_path: Path) -> list[str]:
    return [" ".join(call) for call in calls_of(tmp_path)]


def applied(tmp_path: Path) -> list[list[dict]]:
    """The manifests the script sent to ``kubectl apply``, one list per run."""
    files = sorted(tmp_path.glob("applied-*.yaml"))
    return [list(yaml.safe_load_all(f.read_text(encoding="utf-8"))) for f in files]


def upkeep_job_of(documents: list[dict]) -> dict:
    (job,) = [d for d in documents if d and d["kind"] == "Job"]
    return job


def nothing_touched_the_cluster(tmp_path: Path) -> None:
    assert calls_of(tmp_path) == []
    assert applied(tmp_path) == []


# ── what is refused before any cluster is asked ──────────────────────────────
def test_with_no_arguments_it_says_what_to_pass_and_touches_nothing(
    tmp_path: Path,
) -> None:
    for args in (None, "", "   ", "\t"):
        done = run_upkeep(tmp_path, args)

        assert done.returncode == 1
        assert "ARGS" in done.stderr
        assert "make gateway-upkeep ARGS=" in done.stderr
        assert done.stderr.strip().splitlines()[-1].startswith("error: ")
        nothing_touched_the_cluster(tmp_path)


@pytest.mark.parametrize(
    "character",
    [
        "'",
        '"',
        "\\",
        "$",
        "`",
        "*",
        "?",
        "[",
        "]",
        "{",
        "}",
        ";",
        "&",
        "|",
        "<",
        ">",
        "(",
        ")",
        "!",
        "~",
        "#",
        "%",
        "^",
        ":",
        "/",
        ",",
        "+",
        "@",
        "é",
        chr(0xFF0E),  # a fullwidth full stop: not an ASCII one
        "\r",
        "\x01",
        "\x1b",
    ],
)
def test_a_word_with_any_character_outside_the_allowed_set_is_refused(
    tmp_path: Path, character: str
) -> None:
    done = run_upkeep(tmp_path, f"credit tenant{character}x --tokens 5")

    assert done.returncode == 1
    assert "ARGS" in done.stderr
    assert "letters, digits" in done.stderr
    nothing_touched_the_cluster(tmp_path)
    # The refusal does not send the hostile byte to the terminal.
    assert "\x1b" not in done.stderr
    assert "\r" not in done.stderr


def test_a_newline_in_the_arguments_is_refused_and_says_so(tmp_path: Path) -> None:
    done = run_upkeep(tmp_path, "reservations --older-than 15\ncredit tenant")

    assert done.returncode == 1
    assert "newline" in done.stderr
    nothing_touched_the_cluster(tmp_path)


@pytest.mark.parametrize(
    "args",
    [
        "close $(touch {mark}) --reason dead-process",
        "close x; touch {mark}",
        "close x ; touch {mark}",
        "close x && touch {mark}",
        "close x | touch {mark}",
        "close `touch {mark}`",
        "close x\ntouch {mark}",
        "close x > {mark}",
        "close ${{IFS}}{mark}",
        "close {mark}*",
        "close \"x\" 'y'",
    ],
)
def test_no_word_is_ever_evaluated_a_planted_command_runs_nowhere(
    tmp_path: Path, args: str
) -> None:
    mark = tmp_path / "planted"

    done = run_upkeep(tmp_path, args.format(mark=mark))

    assert done.returncode == 1
    assert not mark.exists()
    assert not list(tmp_path.glob("planted*"))
    nothing_touched_the_cluster(tmp_path)


def test_a_glob_is_refused_and_not_expanded_into_the_names_of_the_files_there(
    tmp_path: Path,
) -> None:
    # The scratch directory is the script's working directory and holds files
    # (`calls`, `output.txt`) whose names would pass as words if `*` expanded.
    done = run_upkeep(tmp_path, "close *")

    assert done.returncode == 1
    assert "'*'" in done.stderr
    nothing_touched_the_cluster(tmp_path)


def test_the_script_never_asks_a_shell_to_read_the_arguments() -> None:
    code = "\n".join(
        line for line in UPKEEP_SH.splitlines() if not line.lstrip().startswith("#")
    )

    assert not re.search(r"\beval\b", code)
    assert not re.search(r"\b(bash|sh) -c\b", code)
    assert "read -r -a" in code
    # The arguments go to helm as JSON built by jq, and as no other flag.
    assert "--set-json" in code
    assert "jobs.upkeep.args" in code
    assert not re.search(r"--set(-string)? [\"']?jobs\.upkeep\.args", code)


# ── what it sends and what it asks of the cluster ────────────────────────────
def test_the_words_reach_helm_as_one_json_list_and_the_chart_makes_the_job(
    tmp_path: Path,
) -> None:
    done = run_upkeep(tmp_path, "  reservations \t--older-than   15 ")

    assert done.returncode == 0, done.stderr
    (template,) = [
        c for c in calls_of(tmp_path) if c[:1] == ["helm"] and "template" in c
    ]
    assert "--set-json" in template
    assert (
        template[template.index("--set-json") + 1]
        == 'jobs.upkeep.args=["reservations","--older-than","15"]'
    )
    assert template[template.index("--show-only") + 1] == "templates/job-upkeep.yaml"
    assert "jobs.upkeep.enabled=true" in template
    (run,) = applied(tmp_path)
    job = upkeep_job_of(run)
    (container,) = job["spec"]["template"]["spec"]["containers"]
    assert container["command"] == ["meridian", "gateway"]
    assert container["args"] == ["reservations", "--older-than", "15"]
    assert container["image"] == f"meridian:{TAG}"
    assert container["env"][0]["valueFrom"]["secretKeyRef"]["name"] == SECRET


def test_a_flag_with_an_equals_sign_a_date_and_an_id_pass_through_whole(
    tmp_path: Path,
) -> None:
    args = "expire --before=2026-09 --reason=old-months close 0f8fad5b-d9cb-469f-a165"

    done = run_upkeep(tmp_path, args)

    assert done.returncode == 0, done.stderr
    (run,) = applied(tmp_path)
    (container,) = upkeep_job_of(run)["spec"]["template"]["spec"]["containers"]
    assert container["args"] == args.split()


def test_the_job_is_applied_outside_the_release_and_nothing_is_installed_or_deleted(
    tmp_path: Path,
) -> None:
    done = run_upkeep(tmp_path, "reservations --older-than 15")

    assert done.returncode == 0, done.stderr
    commands = commands_of(tmp_path)
    for word in ("install", "upgrade", "uninstall", "delete", "rollback", "create"):
        assert not [c for c in commands if re.search(rf"\b{word}\b", c)], word
    applies = [c for c in commands if "apply" in c]
    assert len(applies) == 1
    assert "--server-side --force-conflicts -f -" in applies[0]
    # The image is the release's, read from it, not built or guessed.
    assert any("get values meridian" in c for c in commands)
    assert not [c for c in commands if c.startswith("docker")]


def test_the_cluster_is_asked_in_order_the_secret_then_the_image_then_the_apply(
    tmp_path: Path,
) -> None:
    done = run_upkeep(tmp_path, "reservations --older-than 15")

    assert done.returncode == 0, done.stderr
    commands = commands_of(tmp_path)
    secret = next(i for i, c in enumerate(commands) if f"get secret {SECRET}" in c)
    values = next(i for i, c in enumerate(commands) if "get values" in c)
    template = next(i for i, c in enumerate(commands) if " template " in c)
    apply = next(i for i, c in enumerate(commands) if " apply " in c)
    status = next(i for i, c in enumerate(commands) if " get job " in c)
    assert secret < values < template < apply < status


def test_a_second_run_makes_a_second_job_and_leaves_the_first_alone(
    tmp_path: Path,
) -> None:
    first = run_upkeep(tmp_path, "reservations --older-than 15")
    second = run_upkeep(tmp_path, "reservations --older-than 15")

    assert first.returncode == second.returncode == 0
    runs = applied(tmp_path)
    assert len(runs) == 2
    names = [upkeep_job_of(run)["metadata"]["name"] for run in runs]
    assert len(set(names)) == 2
    assert all(re.fullmatch(r"meridian-upkeep-[a-z0-9-]{1,32}", n) for n in names)
    assert not [c for c in commands_of(tmp_path) if re.search(r"\bdelete\b", c)]


def test_the_job_name_does_not_end_in_the_images_tag(tmp_path: Path) -> None:
    done = run_upkeep(tmp_path, "reservations --older-than 15")

    assert done.returncode == 0, done.stderr
    (run,) = applied(tmp_path)
    assert not upkeep_job_of(run)["metadata"]["name"].endswith(TAG)


# ── what it requires first ───────────────────────────────────────────────────
@pytest.mark.parametrize("state", ["missing", "empty-key", "no-key", "no-data"])
def test_without_the_roles_secret_or_its_key_it_says_run_make_up_and_applies_nothing(
    tmp_path: Path, state: str
) -> None:
    done = run_upkeep(tmp_path, "reservations --older-than 15", role_state=state)

    assert done.returncode == 1
    assert f"Secret {SECRET}" in done.stderr
    assert "run 'make up'" in done.stderr
    assert done.stderr.strip().splitlines()[-1].startswith("error: ")
    commands = commands_of(tmp_path)
    assert not [c for c in commands if " apply " in c]
    assert not [c for c in commands if " template " in c]


@pytest.mark.parametrize(
    "release",
    [
        None,
        "{}",
        json.dumps({"image": {"repository": "meridian"}}),
        json.dumps({"image": {"repository": "meridian", "tag": "latest"}}),
        json.dumps({"image": {"repository": "meridian", "tag": "$(id)"}}),
        json.dumps({"image": {"repository": "other", "tag": TAG}}),
        json.dumps({"image": {"repository": "meridian", "tag": TAG.upper()}}),
    ],
)
def test_without_a_deployed_image_of_make_deploy_it_says_run_make_deploy(
    tmp_path: Path, release: str | None
) -> None:
    done = run_upkeep(tmp_path, "reservations --older-than 15", release=release)

    assert done.returncode == 1
    assert "run 'make deploy'" in done.stderr
    assert not [c for c in commands_of(tmp_path) if " apply " in c]


# ── the verdict, and what the operator sees ──────────────────────────────────
def test_a_succeeded_job_exits_zero_and_prints_its_output(tmp_path: Path) -> None:
    done = run_upkeep(tmp_path, "reservations --older-than 15", job="succeeded")

    assert done.returncode == 0, done.stderr
    assert OUTPUT in done.stdout
    assert "succeeded" in done.stdout


def test_a_failed_job_exits_non_zero_and_prints_the_refusal(tmp_path: Path) -> None:
    done = run_upkeep(
        tmp_path,
        "credit tenant-a --tokens 5 --reason retry-loop",
        job="failed",
        output=REFUSAL,
    )

    assert done.returncode == 1
    assert REFUSAL in done.stdout
    assert "failed" in done.stderr
    assert done.stderr.strip().splitlines()[-1].startswith("error: ")


def test_a_job_that_never_finishes_ends_the_script_and_prints_what_it_has(
    tmp_path: Path,
) -> None:
    done = run_upkeep(tmp_path, "reservations --older-than 15", job="running")

    assert done.returncode == 1
    assert "did not finish" in done.stderr
    assert OUTPUT in done.stdout
    sleeps = [c for c in calls_of(tmp_path) if c[:1] == ["sleep"]]
    assert 10 < len(sleeps) < 500


def test_the_jobs_output_reaches_the_terminal_through_the_printable_filter(
    tmp_path: Path,
) -> None:
    hostile = f"ok \x1b[31mred\x1b[0m\x07\nconnect {CONNECTION} failed\n"

    done = run_upkeep(tmp_path, "reservations --older-than 15", output=hostile)

    assert done.returncode == 0, done.stderr
    assert "\x1b" not in done.stdout + done.stderr
    assert "\x07" not in done.stdout + done.stderr
    assert "not-a-credential" not in done.stdout + done.stderr
    assert "postgresql://[redacted]" in done.stdout


def test_a_failed_jobs_output_is_filtered_too(tmp_path: Path) -> None:
    hostile = f"\x1b[31mERROR\x1b[0m {CONNECTION}\n"

    done = run_upkeep(tmp_path, "close x", job="failed", output=hostile)

    assert done.returncode == 1
    assert "\x1b" not in done.stdout + done.stderr
    assert "not-a-credential" not in done.stdout + done.stderr


def test_the_connection_string_is_never_printed_or_put_on_a_command_line(
    tmp_path: Path,
) -> None:
    done = run_upkeep(tmp_path, "reservations --older-than 15")

    assert done.returncode == 0, done.stderr
    assert "not-a-credential" not in done.stdout + done.stderr
    assert "db.invalid" not in done.stdout + done.stderr
    everything = (tmp_path / "calls").read_text(encoding="utf-8")
    everything += "".join(
        f.read_text(encoding="utf-8") for f in tmp_path.glob("applied-*")
    )
    assert "not-a-credential" not in everything
    assert base64.b64encode(CONNECTION.encode()).decode() not in everything


def test_the_secret_is_read_as_key_names_only() -> None:
    code = "\n".join(
        line for line in UPKEEP_SH.splitlines() if not line.lstrip().startswith("#")
    )

    assert "jq -r '.data // {} | to_entries[]" in code
    assert "base64" not in code
    assert "-o yaml" not in code
    assert "jsonpath" not in code


# ── what moved into common.sh, and what stayed ───────────────────────────────
def test_the_functions_both_scripts_use_are_in_common_sh_and_defined_once() -> None:
    for name in ("helm_chart", "job_state", "printable_ascii"):
        definition = re.compile(rf"^{name}\(\) \{{$", re.MULTILINE)
        assert definition.search(COMMON_SH), name
        assert not definition.search(DEPLOY_SH), name
        assert not definition.search(UPKEEP_SH), name
    for name in ("REPO_ROOT", "CHART_DIR", "VALUES_FILE", "RELEASE"):
        assert re.search(rf"^(readonly )?{name}=", COMMON_SH, re.MULTILINE), name
        assert not re.search(rf"^(readonly )?{name}=", DEPLOY_SH, re.MULTILINE), name
        assert not re.search(rf"^(readonly )?{name}=", UPKEEP_SH, re.MULTILINE), name


def test_the_scripts_use_the_same_chart_arguments_and_the_same_namespace() -> None:
    namespace = re.findall(r"^readonly NAMESPACE=(\S+)$", DEPLOY_SH, re.MULTILINE)

    assert namespace == re.findall(
        r"^readonly NAMESPACE=(\S+)$", UPKEEP_SH, re.MULTILINE
    )
    assert "helm_chart template" in UPKEEP_SH
    assert "helm_chart" in DEPLOY_SH


def test_deploy_sh_never_runs_the_upkeep_job() -> None:
    assert "upkeep" not in DEPLOY_SH.replace("gateway_upkeep", "")
    assert "jobs.upkeep" not in DEPLOY_SH


# ── the make target ──────────────────────────────────────────────────────────
def test_make_gateway_upkeep_runs_the_script_and_passes_no_text_through_a_shell() -> (
    None
):
    phony = next(line for line in MAKEFILE.splitlines() if line.startswith(".PHONY:"))
    recipe = MAKEFILE.split("\ngateway-upkeep:\n", 1)[1].split("\n\n", 1)[0]

    assert "gateway-upkeep" in phony.split()
    assert re.search(r"^## gateway-upkeep +\S", MAKEFILE, re.MULTILINE)
    # The command line's ARGS reaches the script in its environment, never as
    # text in a shell line.
    assert recipe.strip().splitlines() == ["infra/kind/upkeep.sh"]
    assert "$(ARGS)" not in recipe
    assert "ARGS" not in recipe


def test_the_help_line_of_the_target_names_the_variable_and_the_four_subcommands() -> (
    None
):
    (line,) = re.findall(r"^## gateway-upkeep +(.*)$", MAKEFILE, re.MULTILINE)

    assert 'ARGS="' in line
    for word in ("reservations", "close", "credit", "expire"):
        assert word in line


def test_make_dry_run_shows_the_recipe_and_nothing_else() -> None:
    done = subprocess.run(
        # --no-print-directory: under a parent make (make pytest) a nested make
        # prints "Entering directory" lines, which are not the recipe.
        [
            "make",
            "--no-print-directory",
            "-n",
            "gateway-upkeep",
            "ARGS=reservations --older-than 15",
        ],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=False,
        timeout=SECONDS,
    )

    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "infra/kind/upkeep.sh"
