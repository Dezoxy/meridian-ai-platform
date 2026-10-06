"""``make images`` lists the ``meridian:*`` images no workload uses (S062).

``infra/kind/images.sh`` looks at the Docker engine, at the kind node's
containerd and at the pod templates of the ``meridian`` namespace, marks every
image ``in use`` or ``unused`` and prints the commands that would remove the
unused ones. It removes nothing. Nothing touches a real engine or cluster here:
the script runs in a scratch copy of ``infra/kind/`` against stub ``docker`` and
``kubectl`` that log every call and answer in the shapes the real ones print
(``docker image ls --format`` rows and ``crictl images``'s table).
"""

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from certscriptsupport import KIND_DIR, SECONDS
from servicesupport import REPO_ROOT
from test_certificate_deploy import write_stub

IMAGES_SH = KIND_DIR / "images.sh"
MAKEFILE = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
NODE = "meridian-control-plane"
FIRST, SECOND, THIRD, FOURTH = (letter * 12 for letter in "abcd")

# What `docker image ls --format '{{.Repository}}:{{.Tag}} {{.ID}} {{.Size}}'
# meridian` printed on the real engine, with three images.
ENGINE = (
    f"meridian:{FIRST} {FIRST} 426MB\n"
    f"meridian:{SECOND} {SECOND} 425MB\n"
    f"meridian:{THIRD} {THIRD} 424MB\n"
)
# What `crictl images` printed in the real node: a header, then one row per
# image, the repository under docker.io/library and the tag and ID apart.
NODE_HEADER = (
    "IMAGE                                   TAG                  IMAGE ID"
    "            SIZE\n"
)
NODE_OTHERS = (
    "docker.io/grafana/grafana               13.2.3-distroless    72997a53cbee9"
    "       464MB\n"
    "registry.k8s.io/pause                   3.10                 873ed75102791"
    "       320kB\n"
)


def node_row(tag: str, size: str = "91.9MB") -> str:
    return (
        f"docker.io/library/meridian                {tag}"
        f"         5aabd7adeb0e8       {size}\n"
    )


NODE_TABLE = NODE_HEADER + node_row(FIRST) + node_row(FOURTH, "91.8MB") + NODE_OTHERS


def pod_spec(*images: str, init: str | None = None) -> dict:
    containers = [{"name": f"c{n}", "image": i} for n, i in enumerate(images)]
    spec: dict = {"containers": containers}
    if init:
        spec["initContainers"] = [{"name": "init", "image": init}]
    return {"spec": spec}


def workloads(
    *,
    deployment: list[str],
    cronjob: list[str],
    job: list[str] | None = None,
    init: str | None = None,
) -> str:
    """What ``kubectl get deployments,cronjobs,jobs -o json`` answers; ``init``
    is the image of the Deployment's init container."""
    items = [
        {"kind": "Deployment", "spec": {"template": pod_spec(*deployment, init=init)}},
        {
            "kind": "CronJob",
            "spec": {"jobTemplate": {"spec": {"template": pod_spec(*cronjob)}}},
        },
    ]
    if job:
        items.append({"kind": "Job", "spec": {"template": pod_spec(*job)}})
    return json.dumps({"kind": "List", "items": items})


USED = workloads(
    deployment=[f"meridian:{FIRST}", "ghcr.io/other/tool:1"],
    cronjob=[f"meridian:{SECOND}"],
)


def run_images(
    tmp_path: Path,
    *,
    engine: str = ENGINE,
    node: str = NODE_TABLE,
    in_use: str = USED,
    cluster: bool = True,
    reachable: bool = True,
    engine_fails: bool = False,
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    """images.sh in a scratch copy of ``infra/kind/`` against stub ``docker``
    and ``kubectl`` that log every call and answer with ``engine`` (the rows of
    ``docker image ls``), ``node`` (``crictl images``'s table) and ``in_use``
    (the workloads' JSON). With ``cluster`` false there is no credentials file;
    with ``reachable`` false the cluster does not answer. Returns the process
    and the calls the stubs saw, one per line."""
    kind_dir, stubs = tmp_path / "infra" / "kind", tmp_path / "bin"
    kind_dir.mkdir(parents=True)
    stubs.mkdir()
    for name in ("images.sh", "common.sh", "pins.env"):
        shutil.copy(KIND_DIR / name, kind_dir / name)
    if cluster:
        (kind_dir / "kubeconfig").write_text("stub\n", encoding="utf-8")
    (tmp_path / "engine").write_text(engine, encoding="utf-8")
    (tmp_path / "node").write_text(node, encoding="utf-8")
    (tmp_path / "workloads").write_text(in_use, encoding="utf-8")
    calls = tmp_path / "calls"
    calls.touch()
    log = f'echo "{{name}} $*" >>"{calls}"'
    engine_answer = (
        'echo "stub: the daemon is gone" >&2; exit 1'
        if engine_fails
        else f"cat '{tmp_path / 'engine'}'"
    )
    write_stub(
        stubs,
        "docker",
        f"{log.format(name='docker')}\n"
        'case "$*" in\n'
        f'  "image ls --format "*) {engine_answer} ;;\n'
        f"  \"exec {NODE} crictl images\") cat '{tmp_path / 'node'}' ;;\n"
        '  *) echo "stub docker: unexpected $*" >&2; exit 99 ;;\n'
        "esac",
    )
    nodes_answer = "" if reachable else 'echo "connection refused" >&2; exit 1'
    write_stub(
        stubs,
        "kubectl",
        f"{log.format(name='kubectl')}\n"
        'case "$*" in\n'
        f'  *" get nodes"*) {nodes_answer or "true"} ;;\n'
        '  *" -n meridian get deployments,cronjobs,jobs -o json")'
        f" cat '{tmp_path / 'workloads'}' ;;\n"
        '  *) echo "stub kubectl: unexpected $*" >&2; exit 99 ;;\n'
        "esac",
    )
    done = subprocess.run(
        ["bash", str(kind_dir / "images.sh")],
        capture_output=True,
        text=True,
        env={"PATH": f"{stubs}:{os.environ['PATH']}", "HOME": str(tmp_path)},
        check=False,
        timeout=SECONDS,
    )
    return done, calls.read_text(encoding="utf-8").splitlines()


def marks(output: str) -> dict[str, str]:
    """Each image the listing names, with the mark in front of it."""
    return {
        match[2]: match[1]
        for match in re.finditer(
            r"^  (in use|unused)\s+(\S+)\s+\S+$", output, re.MULTILINE
        )
    }


def removal_commands(output: str) -> list[str]:
    """The lines of the output that are commands: they start with a tool."""
    return [
        line
        for line in output.splitlines()
        if line.startswith(("docker ", "kubectl ", "crictl "))
    ]


def test_each_image_is_marked_by_the_workloads_that_use_it_in_both_places(
    tmp_path: Path,
) -> None:
    done, _ = run_images(tmp_path)

    assert done.returncode == 0, done.stderr
    # The engine has three images: the Deployment's, the CronJob's, a third.
    engine_part, node_part = done.stdout.split(f"kind node {NODE}")
    assert marks(engine_part) == {
        f"meridian:{FIRST}": "in use",
        f"meridian:{SECOND}": "in use",
        f"meridian:{THIRD}": "unused",
    }
    # The node has two: the Deployment's, and one nothing uses.
    assert marks(node_part) == {
        f"docker.io/library/meridian:{FIRST}": "in use",
        f"docker.io/library/meridian:{FOURTH}": "unused",
    }


def test_the_counts_and_the_engines_size_of_the_unused_are_printed(
    tmp_path: Path,
) -> None:
    done, _ = run_images(tmp_path)

    engine_part, node_part = done.stdout.split(f"kind node {NODE}")
    assert "images: 3, in use: 2, unused: 1" in engine_part
    assert "images: 2, in use: 1, unused: 1" in node_part
    # The size Docker reports is on the unused image's line.
    assert re.search(rf"unused\s+meridian:{THIRD}\s+424MB", engine_part)


def test_the_printed_removal_commands_name_exactly_the_unused_tags_of_each_place(
    tmp_path: Path,
) -> None:
    done, _ = run_images(tmp_path)

    assert removal_commands(done.stdout) == [
        f"docker image rm meridian:{THIRD}",
        f"docker exec {NODE} crictl rmi docker.io/library/meridian:{FOURTH}",
    ]
    # The listing says whose command it is, and that it ran nothing.
    assert "owner's command" in done.stdout


def test_several_unused_tags_of_a_place_share_one_removal_line(
    tmp_path: Path,
) -> None:
    engine = ENGINE + f"meridian:{FOURTH} {FOURTH} 400MB\n"

    done, _ = run_images(tmp_path, engine=engine)

    assert removal_commands(done.stdout)[0] == (
        f"docker image rm meridian:{THIRD} meridian:{FOURTH}"
    )


def test_a_job_template_and_an_init_container_count_as_a_use(tmp_path: Path) -> None:
    in_use = workloads(
        deployment=["ghcr.io/other/tool:1"],
        cronjob=["ghcr.io/other/tool:2"],
        job=[f"meridian:{THIRD}"],
        init=f"meridian:{SECOND}",
    )

    done, _ = run_images(tmp_path, in_use=in_use)

    shown = marks(done.stdout.split(f"kind node {NODE}")[0])
    assert shown[f"meridian:{SECOND}"] == "in use"
    assert shown[f"meridian:{THIRD}"] == "in use"
    assert shown[f"meridian:{FIRST}"] == "unused"


def test_an_image_with_no_tag_is_not_listed_as_a_meridian_tag(tmp_path: Path) -> None:
    engine = ENGINE + "meridian:<none> 0123456789ab 100MB\n"
    node = NODE_TABLE + node_row("<none>")

    done, _ = run_images(tmp_path, engine=engine, node=node)

    assert "<none>" not in done.stdout
    assert "images: 3, in use: 2, unused: 1" in done.stdout


# ── it removes nothing ───────────────────────────────────────────────────────
FORBIDDEN_CALLS = (
    "docker image rm",
    "docker rmi",
    "docker image prune",
    "docker system prune",
    "rmi",
    "ctr ",
    " delete ",
    " apply ",
    " rm ",
)


def test_the_script_runs_no_command_that_removes_or_changes_anything(
    tmp_path: Path,
) -> None:
    done, calls = run_images(tmp_path)

    assert done.returncode == 0, done.stderr
    # It did look: the engine, the nodes' reachability, the workloads, the node.
    assert any(call.startswith("docker image ls ") for call in calls)
    assert any(call.endswith(" get nodes") for call in calls)
    assert any(" get deployments,cronjobs,jobs " in call for call in calls)
    assert f"docker exec {NODE} crictl images" in calls
    for call in calls:
        assert not any(word in call for word in FORBIDDEN_CALLS), call
    # Every call is one of the four reads.
    assert len(calls) == 4, calls


def test_the_script_names_a_removing_command_only_in_the_suggestions_printf() -> None:
    text = IMAGES_SH.read_text(encoding="utf-8")
    words = (
        "image rm",
        "rmi",
        "prune",
        "delete",
        "apply",
        r"\bctr\b",
        r"\brm\b",
    )

    for word in words:
        for line in text.splitlines():
            if re.search(word, line):
                assert "printf" in line, f"{word!r} outside a printf: {line}"
    assert "image rm" in text
    assert "crictl rmi" in text


# ── no cluster, nothing unused, failures ────────────────────────────────────
def test_with_no_cluster_the_engines_images_are_listed_all_unused_and_it_exits_0(
    tmp_path: Path,
) -> None:
    done, calls = run_images(tmp_path, cluster=False)

    assert done.returncode == 0, done.stderr
    assert marks(done.stdout) == {
        f"meridian:{FIRST}": "unused",
        f"meridian:{SECOND}": "unused",
        f"meridian:{THIRD}": "unused",
    }
    assert "images: 3, in use: 0, unused: 3" in done.stdout
    assert "unused by definition" in done.stdout
    assert removal_commands(done.stdout) == [
        f"docker image rm meridian:{FIRST} meridian:{SECOND} meridian:{THIRD}"
    ]
    # No cluster to ask and no node to look into.
    assert not any(call.startswith("kubectl") for call in calls)
    assert not any("crictl" in call for call in calls)


def test_when_nothing_is_unused_it_says_so_and_prints_no_removal_command(
    tmp_path: Path,
) -> None:
    engine = f"meridian:{FIRST} {FIRST} 426MB\n"
    node = NODE_HEADER + node_row(FIRST) + NODE_OTHERS
    in_use = workloads(deployment=[f"meridian:{FIRST}"], cronjob=["ghcr.io/o/t:1"])

    done, _ = run_images(tmp_path, engine=engine, node=node, in_use=in_use)

    assert done.returncode == 0, done.stderr
    assert "no unused meridian:* image" in done.stdout
    assert removal_commands(done.stdout) == []
    assert "image rm" not in done.stdout
    assert "crictl rmi" not in done.stdout


def test_an_engine_without_meridian_images_lists_none_and_exits_0(
    tmp_path: Path,
) -> None:
    done, _ = run_images(tmp_path, engine="", node=NODE_HEADER + NODE_OTHERS)

    assert done.returncode == 0, done.stderr
    assert marks(done.stdout) == {}
    assert "images: 0, in use: 0, unused: 0" in done.stdout
    assert removal_commands(done.stdout) == []


def test_a_cluster_that_does_not_answer_is_an_error_not_an_all_unused_listing(
    tmp_path: Path,
) -> None:
    done, _ = run_images(tmp_path, reachable=False)

    assert done.returncode != 0
    assert done.stderr.strip().splitlines()[-1].startswith("error: ")
    assert "make up" in done.stderr
    # Taking every image for unused here would suggest removing one in use.
    assert removal_commands(done.stdout) == []


def test_an_engine_that_fails_is_an_error_and_prints_no_removal_command(
    tmp_path: Path,
) -> None:
    done, _ = run_images(tmp_path, engine_fails=True)

    assert done.returncode != 0
    assert done.stderr.strip().splitlines()[-1].startswith("error: ")
    assert removal_commands(done.stdout) == []


# ── the target ───────────────────────────────────────────────────────────────
def test_the_makefile_has_the_images_target_in_phony_and_make_help_lists_it() -> None:
    phony = next(
        line for line in MAKEFILE.splitlines() if line.startswith(".PHONY:")
    ).split()
    recipe = MAKEFILE.split("\nimages:\n", 1)[1].split("\n\n", 1)[0]

    assert "images" in phony
    assert recipe.strip() == "infra/kind/images.sh"
    assert os.access(IMAGES_SH, os.X_OK)
    helped = subprocess.run(
        ["make", "-s", "-C", str(REPO_ROOT), "help"],
        capture_output=True,
        text=True,
        check=False,
        timeout=SECONDS,
    )
    assert re.search(r"^  images\s+\S", helped.stdout, re.MULTILINE)
