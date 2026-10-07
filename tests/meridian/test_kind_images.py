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
from collections.abc import Sequence
from pathlib import Path

import pytest
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


def rollout(*, replicasets: Sequence[str] = (), pods: Sequence[str] = ()) -> str:
    """What ``kubectl get replicasets,pods -o json`` answers: one ReplicaSet and
    one Pod per image given (an old ReplicaSet is one that only ``replicasets``
    names)."""
    items = [
        {"kind": "ReplicaSet", "spec": {"template": pod_spec(image)}}
        for image in replicasets
    ] + [{"kind": "Pod", **pod_spec(image)} for image in pods]
    return json.dumps({"kind": "List", "items": items})


def docker_stub(
    tmp_path: Path, log: str, *, engine_fails: bool, crictl_fails: bool
) -> str:
    """The stub ``docker``: ``docker image ls`` answers the file ``engine`` (or
    fails), ``docker exec NODE crictl images`` the file ``node`` (or fails)."""
    engine = (
        'echo "stub: the daemon is gone" >&2; exit 1'
        if engine_fails
        else f"cat '{tmp_path / 'engine'}'"
    )
    crictl = (
        'echo "stub: crictl cannot reach containerd" >&2; exit 1'
        if crictl_fails
        else f"cat '{tmp_path / 'node'}'"
    )
    return (
        f"{log.format(name='docker')}\n"
        'case "$*" in\n'
        f'  "image ls --format "*) {engine} ;;\n'
        f'  "exec {NODE} crictl images") {crictl} ;;\n'
        '  *) echo "stub docker: unexpected $*" >&2; exit 99 ;;\n'
        "esac"
    )


def kubectl_stub(tmp_path: Path, log: str, failing: set[str]) -> str:
    """The stub ``kubectl``: it answers ``get nodes``, the workloads' JSON (file
    ``workloads``) and the ReplicaSets' and Pods' (file ``rollout``). Each name
    in ``failing`` (``nodes``, ``workloads``, ``rollout``) makes that one call
    fail with a message, the others unchanged."""

    def answer(name: str, otherwise: str) -> str:
        if name in failing:
            return f'echo "stub: {name} failed" >&2; exit 1'
        return otherwise

    nodes = answer("nodes", "true")
    workloads_json = answer("workloads", f"cat '{tmp_path / 'workloads'}'")
    rollout_json = answer("rollout", f"cat '{tmp_path / 'rollout'}'")
    return (
        f"{log.format(name='kubectl')}\n"
        'case "$*" in\n'
        f'  *" get nodes"*) {nodes} ;;\n'
        '  *" -n meridian get deployments,cronjobs,jobs -o json")'
        f" {workloads_json} ;;\n"
        f'  *" -n meridian get replicasets,pods -o json") {rollout_json} ;;\n'
        '  *) echo "stub kubectl: unexpected $*" >&2; exit 99 ;;\n'
        "esac"
    )


def run_images(
    tmp_path: Path,
    *,
    engine: str = ENGINE,
    node: str = NODE_TABLE,
    in_use: str = USED,
    running: str = rollout(),
    cluster: bool = True,
    kind_clusters: str = "No kind clusters found.",
    kind_fails: bool = False,
    reachable: bool = True,
    engine_fails: bool = False,
    workloads_fail: bool = False,
    rollout_fails: bool = False,
    crictl_fails: bool = False,
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    """images.sh in a scratch copy of ``infra/kind/`` against stub ``docker``,
    ``kubectl`` and ``kind`` that log every call and answer with ``engine`` (the
    rows of ``docker image ls``), ``node`` (``crictl images``'s table),
    ``in_use`` (the Deployments', CronJobs' and Jobs' JSON) and ``running`` (the
    ReplicaSets' and Pods'). With ``cluster`` false there is no credentials
    file, and ``kind get clusters`` prints ``kind_clusters`` (or fails with
    ``kind_fails``); with ``reachable`` false the cluster does not answer. Each
    ``*_fails`` makes that one call fail alone: ``docker image ls``, the
    workloads' listing, the ReplicaSets' and Pods' listing, ``crictl images``.
    Returns the process and the calls the stubs saw, one per line."""
    kind_dir, stubs = tmp_path / "infra" / "kind", tmp_path / "bin"
    kind_dir.mkdir(parents=True)
    stubs.mkdir()
    for name in ("images.sh", "common.sh", "pins.env"):
        shutil.copy(KIND_DIR / name, kind_dir / name)
    if cluster:
        (kind_dir / "kubeconfig").write_text("stub\n", encoding="utf-8")
    for name, text in (
        ("engine", engine),
        ("node", node),
        ("workloads", in_use),
        ("rollout", running),
    ):
        (tmp_path / name).write_text(text, encoding="utf-8")
    calls = tmp_path / "calls"
    calls.touch()
    log = f'echo "{{name}} $*" >>"{calls}"'
    write_stub(
        stubs,
        "docker",
        docker_stub(
            tmp_path, log, engine_fails=engine_fails, crictl_fails=crictl_fails
        ),
    )
    failing = {
        name
        for name, fails in (
            ("nodes", not reachable),
            ("workloads", workloads_fail),
            ("rollout", rollout_fails),
        )
        if fails
    }
    write_stub(stubs, "kubectl", kubectl_stub(tmp_path, log, failing))
    kind_answer = (
        'echo "stub: kind cannot list" >&2; exit 1'
        if kind_fails
        else f"cat '{tmp_path / 'clusters'}'"
    )
    (tmp_path / "clusters").write_text(kind_clusters + "\n", encoding="utf-8")
    unexpected = 'echo "stub kind: unexpected $*" >&2; exit 99'
    write_stub(
        stubs,
        "kind",
        f"{log.format(name='kind')}\n"
        f'[[ "$*" == "get clusters" ]] || {{ {unexpected}; }}\n'
        f"{kind_answer}",
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
            r"^  (in use|unused|rollback)\s+(\S+)\s+\S+$", output, re.MULTILINE
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


# ── what an old ReplicaSet or a running Pod names ───────────────────────────
def test_an_image_only_an_old_replicaset_names_is_a_rollbacks_target_not_unused(
    tmp_path: Path,
) -> None:
    # THIRD is in no pod template now, but a ReplicaSet that a rollback would
    # scale up again still names it (kind never pulls: pullPolicy is Never).
    running = rollout(replicasets=[f"meridian:{FIRST}", f"meridian:{THIRD}"])

    done, _ = run_images(tmp_path, running=running)

    assert done.returncode == 0, done.stderr
    engine_part, node_part = done.stdout.split(f"kind node {NODE}")
    assert marks(engine_part) == {
        f"meridian:{FIRST}": "in use",
        f"meridian:{SECOND}": "in use",
        f"meridian:{THIRD}": "rollback",
    }
    assert "images: 3, in use: 2, unused: 0, rollback target: 1" in engine_part
    # The node holds FIRST (in use) and FOURTH, which nothing names.
    assert marks(node_part)[f"docker.io/library/meridian:{FOURTH}"] == "unused"
    assert "a rollback's target" in done.stdout


def test_no_removal_command_names_a_rollbacks_target(tmp_path: Path) -> None:
    node = NODE_HEADER + node_row(FIRST) + node_row(THIRD) + NODE_OTHERS
    running = rollout(replicasets=[f"meridian:{THIRD}"])

    done, _ = run_images(tmp_path, node=node, running=running)

    assert done.returncode == 0, done.stderr
    # Nothing is unused in either place, so there is nothing to remove, and the
    # listing says why THIRD is kept.
    assert removal_commands(done.stdout) == []
    assert "image rm" not in done.stdout
    assert "crictl rmi" not in done.stdout
    assert "no unused meridian:* image" in done.stdout
    (kept,) = [
        line for line in done.stdout.splitlines() if line.startswith("==> kept, with")
    ]
    assert f"meridian:{THIRD}" in kept
    assert f"docker.io/library/meridian:{THIRD}" in kept
    assert "a rollback's target" in kept


def test_a_rollbacks_target_beside_an_unused_image_is_left_out_of_the_command(
    tmp_path: Path,
) -> None:
    engine = ENGINE + f"meridian:{FOURTH} {FOURTH} 400MB\n"
    running = rollout(replicasets=[f"meridian:{THIRD}"])

    done, _ = run_images(tmp_path, engine=engine, running=running)

    assert removal_commands(done.stdout)[0] == f"docker image rm meridian:{FOURTH}"
    assert f"meridian:{THIRD}" not in "\n".join(removal_commands(done.stdout))


def test_the_note_on_a_kept_image_names_the_charts_bound_on_old_replica_sets(
    tmp_path: Path,
) -> None:
    running = rollout(replicasets=[f"meridian:{THIRD}"])

    done, _ = run_images(tmp_path, running=running)

    (kept,) = [
        line for line in done.stdout.splitlines() if line.startswith("==> kept, with")
    ]
    assert "revisionHistoryLimit" in kept
    assert "no command" in kept


def in_use_by(*images: str) -> str:
    """The workloads' JSON with one Deployment that runs ``images``."""
    return workloads(deployment=list(images), cronjob=[f"meridian:{SECOND}"])


@pytest.mark.parametrize(
    "image",
    [
        "registry.example/meridian:{tag}",
        "registry.example:5000/meridian:{tag}",
        "localhost:5000/team/meridian:{tag}",
        "meridian-other:{tag}",
    ],
)
def test_a_reference_to_another_repository_is_not_the_local_image_in_use(
    tmp_path: Path, image: str
) -> None:
    # THIRD is nothing's tag here: a lookalike of another registry (or with a
    # port in its host) must not keep it from being listed, and must not break
    # the read of the colon before the port.
    in_use = in_use_by(f"meridian:{FIRST}", image.format(tag=THIRD))

    done, _ = run_images(tmp_path, in_use=in_use)

    assert done.returncode == 0, done.stderr
    assert marks(done.stdout.split(f"kind node {NODE}")[0])[f"meridian:{THIRD}"] == (
        "unused"
    )


@pytest.mark.parametrize(
    "image",
    [
        "docker.io/library/meridian:{tag}",
        "meridian:{tag}@sha256:" + "0123456789abcdef" * 4,
        "docker.io/library/meridian:{tag}@sha256:" + "0123456789abcdef" * 4,
    ],
)
def test_a_reference_to_the_local_image_is_read_with_its_registry_or_digest(
    tmp_path: Path, image: str
) -> None:
    in_use = in_use_by(f"meridian:{FIRST}", image.format(tag=THIRD))

    done, _ = run_images(tmp_path, in_use=in_use)

    assert done.returncode == 0, done.stderr
    assert marks(done.stdout.split(f"kind node {NODE}")[0])[f"meridian:{THIRD}"] == (
        "in use"
    )


def test_a_reference_with_no_tag_is_the_latest_tag(tmp_path: Path) -> None:
    engine = ENGINE + "meridian:latest 0123456789ab 100MB\n"

    done, _ = run_images(
        tmp_path, engine=engine, in_use=in_use_by(f"meridian:{FIRST}", "meridian")
    )

    shown = marks(done.stdout.split(f"kind node {NODE}")[0])
    assert shown["meridian:latest"] == "in use"


@pytest.mark.parametrize("where", ["a template", "a replica set"])
def test_a_digest_only_reference_stops_the_listing_and_prints_no_command(
    tmp_path: Path, where: str
) -> None:
    # `meridian@sha256:...` names no tag, so which tag it runs cannot be told:
    # a listing that guessed could print a command for an image in use.
    digest = "meridian@sha256:" + "fedcba9876543210" * 4
    if where == "a template":
        done, _ = run_images(tmp_path, in_use=in_use_by(digest))
    else:
        done, _ = run_images(tmp_path, running=rollout(replicasets=[digest]))

    assert done.returncode != 0
    assert removal_commands(done.stdout) == []
    assert "no tag" in done.stderr
    assert "fedcba98" not in done.stdout + done.stderr


def test_a_pod_with_no_template_names_its_image_in_use_and_so_does_a_rollout(
    tmp_path: Path,
) -> None:
    # A bare Pod, or a Pod of the new ReplicaSet while the Deployment's template
    # is not read yet: THIRD runs, so it is in use even though an old
    # ReplicaSet names it too.
    running = rollout(replicasets=[f"meridian:{THIRD}"], pods=[f"meridian:{THIRD}"])

    done, _ = run_images(tmp_path, running=running)

    shown = marks(done.stdout.split(f"kind node {NODE}")[0])
    assert shown[f"meridian:{THIRD}"] == "in use"
    assert "rollback target" not in done.stdout


def test_a_pods_init_container_image_counts_as_in_use(tmp_path: Path) -> None:
    pod = {"kind": "Pod", **pod_spec("ghcr.io/other/tool:1", init=f"meridian:{THIRD}")}
    running = json.dumps({"kind": "List", "items": [pod]})

    done, _ = run_images(tmp_path, running=running)

    shown = marks(done.stdout.split(f"kind node {NODE}")[0])
    assert shown[f"meridian:{THIRD}"] == "in use"


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
    # It did look: the engine, the nodes' reachability, the workloads, the
    # ReplicaSets and Pods, the node.
    assert any(call.startswith("docker image ls ") for call in calls)
    assert any(call.endswith(" get nodes") for call in calls)
    assert any(" get deployments,cronjobs,jobs " in call for call in calls)
    assert any(" get replicasets,pods " in call for call in calls)
    assert f"docker exec {NODE} crictl images" in calls
    for call in calls:
        assert not any(word in call for word in FORBIDDEN_CALLS), call
    # Every call is one of the five reads, and none asks `kind`: the
    # credentials file is there, so the cluster is known to exist.
    assert len(calls) == 5, calls


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
    # The cluster was asked for by name, and there is none: no cluster to ask
    # about workloads and no node to look into.
    assert "kind get clusters" in calls
    assert not any(call.startswith("kubectl") for call in calls)
    assert not any("crictl" in call for call in calls)


def test_with_no_credentials_file_but_a_cluster_it_cannot_tell_and_names_no_command(
    tmp_path: Path,
) -> None:
    # A second checkout while the cluster belongs to another: kind lists it.
    done, calls = run_images(tmp_path, cluster=False, kind_clusters="meridian")

    assert done.returncode != 0
    last = done.stderr.strip().splitlines()[-1]
    assert last.startswith("error: ")
    assert "cannot tell which images are in use" in last
    assert "kubeconfig" in last
    # The old verdict must not appear, and no removal command is printed.
    assert "unused by definition" not in done.stdout + done.stderr
    assert removal_commands(done.stdout) == []
    assert "image rm" not in done.stdout
    assert not any(call.startswith("kubectl") for call in calls)


def test_a_kind_that_fails_is_not_read_as_no_cluster(tmp_path: Path) -> None:
    done, _ = run_images(tmp_path, cluster=False, kind_fails=True)

    assert done.returncode != 0
    assert done.stderr.strip().splitlines()[-1].startswith("error: ")
    assert "unused by definition" not in done.stdout
    assert removal_commands(done.stdout) == []


def test_another_clusters_name_is_not_this_clusters(tmp_path: Path) -> None:
    done, _ = run_images(tmp_path, cluster=False, kind_clusters="other\nmeridian-2")

    assert done.returncode == 0, done.stderr
    assert "unused by definition" in done.stdout


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


@pytest.mark.parametrize(
    ("failing", "said"),
    [
        ({"workloads_fail": True}, "cannot read the workloads"),
        ({"rollout_fails": True}, "cannot read the replica sets and pods"),
        ({"crictl_fails": True}, "crictl images failed"),
    ],
)
def test_a_listing_that_fails_alone_is_an_error_and_prints_no_removal_command(
    tmp_path: Path, failing: dict[str, bool], said: str
) -> None:
    # Each of the three reads fails with the others answering: reading its
    # silence as "nothing in use" (or "no images") would print the commands that
    # remove an image a workload runs on.
    done, _ = run_images(tmp_path, **failing)

    assert done.returncode != 0
    last = done.stderr.strip().splitlines()[-1]
    assert last.startswith("error: ")
    assert said in last
    assert removal_commands(done.stdout) == []
    assert "image rm" not in done.stdout
    assert "unused by definition" not in done.stdout


@pytest.mark.parametrize(
    "answers",
    [
        {"in_use": "this is not json"},
        {"in_use": "{}"},
        {"in_use": ""},
        {"in_use": "\n"},
        {"running": "this is not json"},
        {"running": "{}"},
        {"running": ""},
        {"running": "\n"},
    ],
    ids=[
        "workloads-not-json",
        "workloads-no-items",
        "workloads-empty-answer",
        "workloads-blank-answer",
        "rollout-not-json",
        "rollout-no-items",
        "rollout-empty-answer",
        "rollout-blank-answer",
    ],
)
def test_a_read_of_the_tags_in_use_that_cannot_be_made_stops_with_a_sentence(
    tmp_path: Path, answers: dict[str, str]
) -> None:
    # The cluster answered, but not with a list the script can read. The first
    # of the two reads in "used" ended in an empty list that no check saw, and
    # every image printed as unused with its removal command.
    done, _ = run_images(tmp_path, **answers)

    assert done.returncode != 0
    last = done.stderr.strip().splitlines()[-1]
    assert last.startswith("error: ")
    assert "cannot read the tags in use" in last
    assert removal_commands(done.stdout) == []
    assert "image rm" not in done.stdout
    assert marks(done.stdout) == {}


def test_a_list_with_no_workload_that_names_the_image_is_still_a_valid_read(
    tmp_path: Path,
) -> None:
    # The guard must not turn a true empty answer into an error: a namespace
    # whose workloads name no meridian image reads as nothing in use.
    done, _ = run_images(
        tmp_path,
        in_use=json.dumps({"kind": "List", "items": []}),
        running=json.dumps({"kind": "List", "items": []}),
    )

    assert done.returncode == 0, done.stderr
    assert set(marks(done.stdout).values()) == {"unused"}


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
