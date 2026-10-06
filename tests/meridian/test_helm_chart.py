"""The Meridian Helm chart is what its constraints say it is (S019).

The chart replaced the raw manifests of the six services, the three Jobs and
the sweep's CronJob, and a live cluster adopts its objects with
``--take-ownership``: a selector or a name that changed would not be adopted, it
would be refused or duplicated. These tests render the chart (``helm template``;
tests/meridian/chartsupport.py) and pin that, and that no credential is in a
value. What each object says is pinned in test_kind_manifests.py.
"""

import re
import shlex
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import yaml
from chartsupport import (
    CHART_DIR,
    IDENTITY_ARGUMENTS,
    IMAGE_REPOSITORY,
    JOBS,
    NAME_LABEL,
    NAMESPACE,
    RELEASE,
    SERVICES,
    TEST_DIGEST,
    TEST_TAG,
    VALUES_FILE,
    helm_arguments,
    kinds_of,
    pod_labels,
    pod_spec,
    pod_workloads,
    render,
    rendered_chart,
    run_helm,
)
from servicesupport import REPO_ROOT

from meridian.platform.toolserver.settings import ALLOWED_HOSTS_ENV
from meridian.workloads.claims_triage.settings import RUNTIME_URL_ENV

KIND_DIR = REPO_ROOT / "infra" / "kind"
DEPLOY_SH = (KIND_DIR / "deploy.sh").read_text(encoding="utf-8")
README = (KIND_DIR / "README.md").read_text(encoding="utf-8")
MAKEFILE = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
WORKFLOW = yaml.safe_load(
    (REPO_ROOT / ".github" / "workflows" / "python.yml").read_text(encoding="utf-8")
)
# Keys that would hold a credential if one were put in a value. What they may
# hold is the name of a Secret or the name of a key inside one, never a secret.
CREDENTIAL_KEYS = {"password", "passwd", "uri", "token", "key", "apikey", "secret"}
SECRET_NAMES = re.compile(r"[a-z][a-z0-9-]*-db(-ca)?")
SECRET_KEY_NAMES = {"uri", "ca.crt"}
CONNECTION_STRING = re.compile(r"postgres(ql)?://", re.IGNORECASE)


def test_rendering_without_the_image_tag_fails_and_says_what_to_pass() -> None:
    done = run_helm(helm_arguments(tag=None))

    assert done.returncode != 0
    assert "image.tag is required" in done.stderr


def test_rendering_without_the_image_repository_fails_and_says_what_to_pass() -> None:
    done = run_helm(helm_arguments(repository=None))

    assert done.returncode != 0
    assert "image.repository is required" in done.stderr


def test_a_tag_of_digits_only_is_a_string_in_every_name_and_image() -> None:
    # deploy.sh cuts twelve hex digits from the image ID; twelve digits and no
    # letter happens. deploy.sh passes the tag with --set-string; a tag given
    # with --set reaches the chart as a number, and the chart converts it.
    tag = "123456789012"
    as_string = render(helm_arguments(tag=tag))
    as_number = render(
        [*helm_arguments(tag=None), "--set", f"image.tag={tag}"],
    )

    for documents in (as_string, as_number):
        assert f"meridian-migrate-{tag}" in {
            d["metadata"]["name"] for d in documents if d["kind"] == "Job"
        }
        for document in documents:
            if document["kind"] == "Deployment":
                image = document["spec"]["template"]["spec"]["containers"][0]["image"]
                assert image == f"{IMAGE_REPOSITORY}:{tag}"


def test_a_selector_is_the_name_label_alone_so_helm_can_adopt_the_object() -> None:
    documents = list(rendered_chart())
    deployments = [d for d in documents if d["kind"] == "Deployment"]
    services = [d for d in documents if d["kind"] == "Service"]

    assert {d["metadata"]["name"] for d in deployments} == set(SERVICES)
    assert {d["metadata"]["name"] for d in services} == set(SERVICES)
    for deployment in deployments:
        name = deployment["metadata"]["name"]
        assert deployment["spec"]["selector"] == {
            "matchLabels": {"app.kubernetes.io/name": name}
        }
        # A chart version must not roll every pod: nothing of Helm's in them.
        assert deployment["spec"]["template"]["metadata"]["labels"] == {
            "app.kubernetes.io/name": name,
            "app.kubernetes.io/part-of": "meridian",
        }
    for service in services:
        name = service["metadata"]["name"]
        assert service["spec"]["selector"] == {"app.kubernetes.io/name": name}


def test_the_chart_creates_no_namespace_and_no_helm_hook() -> None:
    documents = list(rendered_chart())

    assert "Namespace" not in {d["kind"] for d in documents}
    assert "helm.sh/hook" not in yaml.dump(documents)
    assert not (CHART_DIR / "crds").exists()


def test_every_object_is_in_the_release_namespace_and_named_without_a_prefix() -> None:
    for document in render(helm_arguments(namespace="elsewhere")):
        assert document["metadata"]["namespace"] == "elsewhere", document["kind"]
    names = {d["metadata"]["name"] for d in rendered_chart()}

    assert set(SERVICES) <= names
    assert not [n for n in names if n.startswith(f"{RELEASE}-{RELEASE}")]


def test_the_in_cluster_addresses_follow_the_release_namespace() -> None:
    documents = render(helm_arguments(namespace="elsewhere"))
    deployments = {
        d["metadata"]["name"]: d for d in documents if d["kind"] == "Deployment"
    }

    def env(name: str) -> dict[str, dict]:
        (container,) = deployments[name]["spec"]["template"]["spec"]["containers"]
        return {item["name"]: item for item in container["env"]}

    runtime = urlsplit(env("claims-api")[RUNTIME_URL_ENV]["value"])
    assert runtime.hostname == "agent-runtime.elsewhere.svc"
    for server in ("policy-mcp", "claims-mcp", "knowledge-mcp"):
        assert env(server)[ALLOWED_HOSTS_ENV]["value"] == f"{server}.elsewhere.svc:8000"
    tool_servers = env("agent-runtime")["MERIDIAN_TOOL_SERVERS"]["value"]
    assert set(re.findall(r"//([\w-]+\.[\w-]+)\.svc", tool_servers)) == {
        f"{server}.elsewhere"
        for server in ("policy-mcp", "claims-mcp", "knowledge-mcp")
    }
    assert "meridian.svc" not in yaml.dump(documents)


def test_a_service_listens_where_its_command_its_port_and_its_service_agree() -> None:
    for document in rendered_chart():
        if document["kind"] != "Deployment":
            continue
        (container,) = document["spec"]["template"]["spec"]["containers"]
        command = container["command"]
        (port,) = container["ports"]

        assert command[command.index("--port") + 1] == str(port["containerPort"])
        (service,) = [
            d
            for d in rendered_chart()
            if d["kind"] == "Service"
            and d["metadata"]["name"] == document["metadata"]["name"]
        ]
        assert [p["port"] for p in service["spec"]["ports"]] == [port["containerPort"]]


@pytest.mark.parametrize("name", JOBS)
def test_show_only_a_jobs_template_yields_the_job_and_its_account_and_nothing_else(
    name: str,
) -> None:
    documents = render(
        [*helm_arguments(jobs=(name,)), "--show-only", f"templates/job-{name}.yaml"]
    )

    # The policy is in the Job's own template, so deploy.sh, which applies this
    # output and nothing else of the chart, never applies a Job without it. It
    # is named after the pod's label, without the tag: a later deploy replaces it.
    assert kinds_of(documents) == ["Job", "NetworkPolicy", "ServiceAccount"]
    assert {d["metadata"]["name"] for d in documents} == {
        f"meridian-{name}-{TEST_TAG}",
        f"meridian-{name}",
    }
    (policy,) = [d for d in documents if d["kind"] == "NetworkPolicy"]
    assert policy["metadata"]["name"] == f"meridian-{name}"
    assert policy["spec"]["podSelector"] == {
        "matchLabels": {NAME_LABEL: f"meridian-{name}"}
    }


def test_a_job_is_not_rendered_unless_its_flag_is_set() -> None:
    for name in JOBS:
        done = run_helm(
            [*helm_arguments(jobs=()), "--show-only", f"templates/job-{name}.yaml"]
        )
        assert done.returncode != 0  # nothing rendered from that template
    assert "Job" not in {d["kind"] for d in render(helm_arguments(jobs=()))}


def test_the_route_renders_only_when_enabled_and_kind_enables_it() -> None:
    without_kind = render(
        [
            "template",
            RELEASE,
            str(CHART_DIR),
            "--namespace",
            NAMESPACE,
            "--set-string",
            f"image.repository={IMAGE_REPOSITORY}",
            "--set-string",
            f"image.tag={TEST_TAG}",
            "--set-string",
            "image.pullPolicy=Never",  # a tag needs it (the image rules below)
            *IDENTITY_ARGUMENTS,
            # kind's values name the peers of the policies; without them the
            # chart refuses to render a policy (tested below).
            "--set",
            "networkPolicy.enabled=false",
        ]
    )

    assert not {"HTTPRoute", "BackendTrafficPolicy"} & {d["kind"] for d in without_kind}
    assert {"HTTPRoute", "BackendTrafficPolicy"} <= {
        d["kind"] for d in rendered_chart()
    }


def credential_findings(node: object, path: str = "") -> list[str]:
    """Where ``node`` (parsed values) holds what looks like a credential."""
    found: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            where = f"{path}.{key}"
            if str(key).lower() in CREDENTIAL_KEYS and not allowed_secret_reference(
                value
            ):
                found.append(f"{where}: a {key} that is not a Secret's or key's name")
            found += credential_findings(value, where)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            found += credential_findings(value, f"{path}[{index}]")
    elif isinstance(node, str) and CONNECTION_STRING.search(node):
        found.append(f"{path}: a connection string")
    return found


def allowed_secret_reference(value: object) -> bool:
    return isinstance(value, dict | list) or (
        isinstance(value, str)
        and (value in SECRET_KEY_NAMES or bool(SECRET_NAMES.fullmatch(value)))
    )


def test_the_credential_check_finds_a_planted_credential_and_a_secrets_name() -> None:
    planted = {
        "database": {"password": "hunter2"},
        "services": {"claims-api": {"env": [{"name": "X", "key": "s3cr3t"}]}},
        "url": "postgresql://owner:pw@host/db",
    }

    assert len(credential_findings(planted)) == 3
    assert not credential_findings({"dbSecret": "claims-api-db", "key": "uri"})
    assert not credential_findings({"secret": "platform-db-ca"})


def test_no_value_looks_like_a_credential_and_no_connection_string_is_in_it() -> None:
    for values_file in (CHART_DIR / "values.yaml", VALUES_FILE):
        values = yaml.safe_load(values_file.read_text(encoding="utf-8"))
        assert values, values_file
        assert not credential_findings(values), values_file
    for path in [*CHART_DIR.rglob("*"), VALUES_FILE]:
        if path.is_file():
            text = path.read_text(encoding="utf-8")
            assert not CONNECTION_STRING.search(text), path
    # What the rendered objects hold is a reference to a Secret, never its value.
    for document in rendered_chart():
        assert not CONNECTION_STRING.search(yaml.dump(document)), document["kind"]


def lint_arguments(chart: Path = CHART_DIR) -> list[str]:
    arguments = helm_arguments()
    return ["lint", "--strict", str(chart), *arguments[3:]]


def test_helm_lint_strict_passes_with_kinds_values_and_every_job_on() -> None:
    done = run_helm(lint_arguments())

    assert done.returncode == 0, done.stdout + done.stderr
    assert "0 chart(s) failed" in done.stdout


def test_helm_lint_strict_fails_without_the_image_so_the_check_can_fail() -> None:
    arguments = lint_arguments()
    index = arguments.index("--set-string")
    without_image = arguments[:index] + [
        a
        for a in arguments[index:]
        if not a.startswith("image.") and a != "--set-string"
    ]
    done = run_helm(without_image)

    assert done.returncode != 0


def chart_flags(text: str) -> list[str]:
    """The words of the ``helmc`` call in deploy.sh's ``helm_chart`` function
    for ``helm template``, with the script's variables (read from the script
    and from common.sh, where the image's repository is shared with images.sh)
    replaced by their values."""
    common = (KIND_DIR / "common.sh").read_text(encoding="utf-8")
    constants = {
        name: value.strip('"')
        for name, value in re.findall(
            r"^readonly (NAMESPACE|IMAGE_REPOSITORY|RELEASE|CHART_DIR|VALUES_FILE)"
            r"=(.*)$",
            text + common,
            re.MULTILINE,
        )
    }
    constants |= {
        "REPO_ROOT": str(REPO_ROOT),
        "KIND_DIR": str(KIND_DIR),
        "tag": TEST_TAG,
        "verb": "template",
    }
    (call,) = re.findall(r"^  helmc .*$", function_body(text, "helm_chart"), re.M)
    words = shlex.split(call.removeprefix("  helmc ").replace('"$@"', ""))
    expanded: list[str] = []
    for word in words:
        for _ in range(3):  # a constant may be written with another one
            word = re.sub(
                r"\$\{(\w+)\}", lambda m: constants.get(m.group(1), m.group(0)), word
            )
        expanded.append(word)
    return expanded


def function_body(text: str, name: str) -> str:
    match = re.search(rf"^{name}\(\) \{{\n(.*?)^\}}", text, re.MULTILINE | re.DOTALL)
    assert match, f"no function {name}"
    return match.group(1)


def test_deploy_passes_helm_the_arguments_the_chart_tests_render_with() -> None:
    shared = helm_arguments(jobs=())

    assert chart_flags(DEPLOY_SH) == shared
    body = function_body(DEPLOY_SH, "render_job")
    assert '--set "jobs.${name}.enabled=true"' in body
    assert '--show-only "templates/job-${name}.yaml"' in body
    # Every Job flag the tests set is one deploy.sh can set, and the three
    # templates are the three files the chart has.
    assert {p.name for p in (CHART_DIR / "templates").glob("job-*.yaml")} == {
        f"job-{name}.yaml" for name in JOBS
    }


def workflow_steps() -> list[dict]:
    return WORKFLOW["jobs"]["python"]["steps"]


def test_the_workflow_installs_the_helm_version_the_readme_documents() -> None:
    (documented,) = re.findall(
        r"^\| helm \| (v\d+\.\d+\.\d+) \|$", README, re.MULTILINE
    )
    (setup,) = [
        s for s in workflow_steps() if s.get("uses", "").startswith("azure/setup-helm@")
    ]

    assert re.fullmatch(r"azure/setup-helm@[0-9a-f]{40}", setup["uses"])
    assert setup["with"]["version"] == documented


def test_the_workflow_lints_the_chart_before_the_tests_run() -> None:
    steps = workflow_steps()
    runs = [s.get("run") for s in steps]
    (setup,) = [
        i
        for i, s in enumerate(steps)
        if s.get("uses", "").startswith("azure/setup-helm@")
    ]

    assert runs.count("make helm-lint") == 1
    assert setup < runs.index("make helm-lint") < runs.index("make pytest")
    # The evaluation gate must stay the step right after the tests.
    names = [s.get("name") for s in steps]
    assert names.index("Evaluation gate") == names.index("Tests") + 1


def test_make_helm_lint_lints_the_chart_strictly_with_kinds_values_and_every_job() -> (
    None
):
    phony = next(line for line in MAKEFILE.splitlines() if line.startswith(".PHONY:"))
    recipe = MAKEFILE.split("\nhelm-lint:\n", 1)[1].split("\n\n", 1)[0]

    assert "helm-lint" in phony.split()
    assert re.search(r"^## helm-lint ", MAKEFILE, re.MULTILINE)
    assert "helm lint --strict infra/helm/meridian" in recipe
    assert "-f infra/kind/values/meridian.yaml" in recipe
    assert "image.repository=" in recipe
    assert "image.tag=" in recipe
    for name in JOBS:
        assert f"jobs.{name}.enabled=true" in recipe


# ---------------------------------------------------------------------------
# Container hardening (S019): a read-only root filesystem with one small
# writable /tmp, a pod-level security context, a PodDisruptionBudget per
# service, a pinned image and the one-replica rule of the gateway.
# ---------------------------------------------------------------------------

USER_ID = 10001  # the Dockerfile's USER; test_kind_manifests.py ties them
TMP_SIZE_LIMIT = "16Mi"
SHA256_HEX_DIGITS = 64
DIGEST_PREFIX_LENGTH = 12
MALFORMED_DIGESTS = {
    "no algorithm": "0123456789abcdef" * 4,
    "another algorithm": "md5:" + "0" * 32,
    "63 digits": "sha256:" + "a" * 63,
    "65 digits": "sha256:" + "a" * 65,
    "upper case": "sha256:" + "A" * 64,
    "not hex": "sha256:" + "g" * 64,
    "a tag in its place": "sha256:latest",
}


def images_of(documents: list[dict]) -> set[str]:
    return {
        container["image"]
        for workload in pod_workloads(documents)
        for container in pod_spec(workload)["containers"]
    }


def test_the_chart_renders_the_six_deployments_the_three_jobs_and_the_sweep() -> None:
    # Pins what the hardening tests below range over: a test that ranged over
    # nothing would pass.
    workloads = pod_workloads(list(rendered_chart()))

    assert sorted(w["kind"] for w in workloads) == (
        ["CronJob"] + ["Deployment"] * len(SERVICES) + ["Job"] * len(JOBS)
    )


def test_every_container_has_a_read_only_root_filesystem() -> None:
    for workload in pod_workloads(list(rendered_chart())):
        for container in pod_spec(workload)["containers"]:
            context = container["securityContext"]
            where = f"{workload['metadata']['name']}/{container['name']}"
            assert context["readOnlyRootFilesystem"] is True, where


def test_every_pod_has_a_writable_tmp_of_16mi_and_no_other_writable_path() -> None:
    for workload in pod_workloads(list(rendered_chart())):
        name = workload["metadata"]["name"]
        pod = pod_spec(workload)
        volumes = {v["name"]: v for v in pod["volumes"]}

        # The one emptyDir, named tmp, on the default medium (a memory-backed
        # one would count against the container's memory limit).
        assert [n for n, v in volumes.items() if "emptyDir" in v] == ["tmp"], name
        assert volumes["tmp"]["emptyDir"] == {"sizeLimit": TMP_SIZE_LIMIT}, name
        # Every other volume is a Secret mounted read-only: no hostPath, no
        # ConfigMap, no second emptyDir.
        for volume_name, volume in volumes.items():
            if volume_name != "tmp":
                assert set(volume) == {"name", "secret"}, (name, volume_name)
        for container in pod["containers"]:
            mounts = {m["name"]: m for m in container["volumeMounts"]}
            assert set(mounts) == set(volumes), name
            assert mounts["tmp"]["mountPath"] == "/tmp", name  # noqa: S108
            assert not mounts["tmp"].get("readOnly"), name
            for mount_name, mount in mounts.items():
                if mount_name != "tmp":
                    assert mount["readOnly"] is True, (name, mount_name)
            writable = [
                m["mountPath"] for m in mounts.values() if not m.get("readOnly")
            ]
            assert writable == ["/tmp"], name  # noqa: S108


def test_every_pod_has_the_pod_level_security_context() -> None:
    for workload in pod_workloads(list(rendered_chart())):
        pod = pod_spec(workload)

        assert pod["securityContext"] == {
            "runAsNonRoot": True,
            "runAsUser": USER_ID,
            "runAsGroup": USER_ID,
            "fsGroup": USER_ID,
            "seccompProfile": {"type": "RuntimeDefault"},
        }, workload["metadata"]["name"]


def test_the_user_and_group_of_both_levels_come_from_one_value() -> None:
    # The pod and its containers must not disagree: one value sets all three.
    for workload in pod_workloads(
        render([*helm_arguments(), "--set", "runAsId=20000"])
    ):
        pod = pod_spec(workload)

        assert pod["securityContext"]["runAsUser"] == 20000
        assert pod["securityContext"]["runAsGroup"] == 20000
        for container in pod["containers"]:
            assert container["securityContext"]["runAsUser"] == 20000


def test_a_pod_disruption_budget_per_service_and_none_for_a_job_or_the_cronjob() -> (
    None
):
    documents = list(rendered_chart())
    deployments = {
        d["metadata"]["name"]: d for d in documents if d["kind"] == "Deployment"
    }
    budgets = [d for d in documents if d["kind"] == "PodDisruptionBudget"]

    assert sorted(b["metadata"]["name"] for b in budgets) == sorted(deployments)
    for budget in budgets:
        name = budget["metadata"]["name"]
        assert budget["apiVersion"] == "policy/v1"
        assert budget["metadata"]["namespace"] == NAMESPACE
        assert budget["spec"]["selector"] == deployments[name]["spec"]["selector"]
        assert budget["spec"]["selector"] == {
            "matchLabels": {"app.kubernetes.io/name": name}
        }
        assert budget["spec"]["maxUnavailable"] == 1
        assert "minAvailable" not in budget["spec"]
        # A budget on a Job's or the CronJob's pods would block their eviction.
        for workload in pod_workloads(documents):
            if workload["kind"] != "Deployment":
                selected = budget["spec"]["selector"]["matchLabels"].items()
                assert not selected <= pod_labels(workload).items(), workload["kind"]


def test_every_service_runs_one_replica_by_default_and_a_value_sets_one_service() -> (
    None
):
    default = {
        d["metadata"]["name"]: d["spec"]["replicas"]
        for d in rendered_chart()
        if d["kind"] == "Deployment"
    }
    scaled = {
        d["metadata"]["name"]: d["spec"]["replicas"]
        for d in render([*helm_arguments(), "--set", "services.claims-api.replicas=2"])
        if d["kind"] == "Deployment"
    }

    assert default == dict.fromkeys(SERVICES, 1)
    assert scaled == default | {"claims-api": 2}


def test_the_gateway_renders_with_one_replica_and_fails_with_two() -> None:
    one = run_helm(
        [*helm_arguments(), "--set", "services.model-gateway.replicas=1"],
    )
    two = run_helm([*helm_arguments(), "--set", "services.model-gateway.replicas=2"])

    assert one.returncode == 0, one.stderr
    assert two.returncode != 0
    assert "model-gateway" in two.stderr
    assert "T-45" in two.stderr
    assert "rate" in two.stderr and "each replica" in two.stderr


def test_every_container_runs_the_digest_when_one_is_given() -> None:
    # The tag is not needed, and not part of the reference.
    for pull_policy in ("IfNotPresent", "Never", "Always"):
        documents = render(
            [
                *helm_arguments(tag=None),
                "--set-string",
                f"image.digest={TEST_DIGEST}",
                "--set-string",
                f"image.pullPolicy={pull_policy}",
            ]
        )

        assert images_of(documents) == {f"{IMAGE_REPOSITORY}@{TEST_DIGEST}"}
        assert len(pod_workloads(documents)) == len(SERVICES) + len(JOBS) + 1


def test_a_digest_wins_over_a_tag_in_the_reference_and_the_tag_names_the_jobs() -> None:
    documents = render(
        [*helm_arguments(), "--set-string", f"image.digest={TEST_DIGEST}"]
    )

    assert images_of(documents) == {f"{IMAGE_REPOSITORY}@{TEST_DIGEST}"}
    assert {d["metadata"]["name"] for d in documents if d["kind"] == "Job"} == {
        f"meridian-{name}-{TEST_TAG}" for name in JOBS
    }


def test_a_job_without_a_tag_is_named_with_the_first_twelve_digits_of_the_digest() -> (
    None
):
    documents = render(
        [*helm_arguments(tag=None), "--set-string", f"image.digest={TEST_DIGEST}"]
    )
    suffix = TEST_DIGEST.removeprefix("sha256:")[:DIGEST_PREFIX_LENGTH]

    assert suffix != TEST_TAG
    assert {d["metadata"]["name"] for d in documents if d["kind"] == "Job"} == {
        f"meridian-{name}-{suffix}" for name in JOBS
    }


@pytest.mark.parametrize("digest", MALFORMED_DIGESTS.values(), ids=MALFORMED_DIGESTS)
def test_a_malformed_digest_fails_and_says_the_format(digest: str) -> None:
    done = run_helm([*helm_arguments(), "--set-string", f"image.digest={digest}"])

    assert done.returncode != 0
    assert "image.digest" in done.stderr
    assert "sha256:" in done.stderr


def test_a_tag_without_a_digest_fails_unless_the_image_is_never_pulled() -> None:
    for pull_policy in ("IfNotPresent", "Always"):
        done = run_helm(
            [*helm_arguments(), "--set-string", f"image.pullPolicy={pull_policy}"]
        )

        assert done.returncode != 0, pull_policy
        assert "never pulled" in done.stderr
        assert "image.digest" in done.stderr
    never = run_helm([*helm_arguments(), "--set-string", "image.pullPolicy=Never"])
    assert never.returncode == 0, never.stderr


@pytest.mark.parametrize(
    "tag", ["latest", "with space", "a/b", "", ".leading-dot", "x" * 129]
)
def test_a_tag_that_is_latest_or_not_a_tag_fails_and_says_the_rule(tag: str) -> None:
    done = run_helm(helm_arguments(tag=tag))

    assert done.returncode != 0, tag
    assert "image.tag" in done.stderr


@pytest.mark.parametrize("tag", ["0123456789ab", "lint", "v1.2_3-rc", "x" * 128])
def test_a_tag_of_twelve_hex_digits_or_lint_or_a_plain_tag_renders(tag: str) -> None:
    documents = render(helm_arguments(tag=tag))

    assert images_of(documents) == {f"{IMAGE_REPOSITORY}:{tag}"}


def test_a_job_named_with_a_tag_fails_on_latest_even_with_a_digest() -> None:
    # The digest keeps the tag out of the image reference; a Job's name still
    # ends in the tag, so only the suffix can reject it.
    done = run_helm(
        [
            *helm_arguments(tag="latest"),
            "--set-string",
            f"image.digest={TEST_DIGEST}",
        ]
    )

    assert done.returncode != 0
    assert "image.tag" in done.stderr


@pytest.mark.parametrize(
    "repository",
    ["meridian:latest", "meridian:v1", "meridian@sha256:" + "ab" * 32, "reg:5000/m:v1"],
)
def test_a_repository_with_its_own_tag_or_digest_fails(repository: str) -> None:
    done = run_helm(helm_arguments(repository=repository))

    assert done.returncode != 0, repository
    assert "image.repository" in done.stderr


def test_a_repository_with_a_registry_port_is_valid() -> None:
    documents = render(helm_arguments(repository="registry:5000/meridian"))

    assert images_of(documents) == {f"registry:5000/meridian:{TEST_TAG}"}


def test_the_security_contexts_are_not_values_so_a_set_cannot_loosen_them() -> None:
    loosened = render(
        [
            *helm_arguments(),
            "--set",
            "securityContext.privileged=true",
            "--set",
            "securityContext.readOnlyRootFilesystem=false",
            "--set",
            "podSecurityContext.runAsNonRoot=false",
        ]
    )

    assert loosened == list(rendered_chart())
    values = yaml.safe_load((CHART_DIR / "values.yaml").read_text(encoding="utf-8"))
    assert "securityContext" not in values
    assert "podSecurityContext" not in values
    for workload in pod_workloads(loosened):
        pod = pod_spec(workload)
        assert pod["securityContext"]["runAsNonRoot"] is True
        for container in pod["containers"]:
            context = container["securityContext"]
            assert not context.get("privileged")
            assert context["readOnlyRootFilesystem"] is True


def test_running_as_root_fails() -> None:
    done = run_helm([*helm_arguments(), "--set", "runAsId=0"])

    assert done.returncode != 0
    assert "runAsId" in done.stderr


def test_the_chart_default_pulls_if_not_present_and_kind_never_pulls() -> None:
    chart = yaml.safe_load((CHART_DIR / "values.yaml").read_text(encoding="utf-8"))
    kind = yaml.safe_load(VALUES_FILE.read_text(encoding="utf-8"))

    assert chart["image"]["pullPolicy"] == "IfNotPresent"
    assert chart["image"]["digest"] == ""
    assert kind["image"]["pullPolicy"] == "Never"
    # kind's values render the loaded image by its tag, and never pull it.
    for workload in pod_workloads(list(rendered_chart())):
        for container in pod_spec(workload)["containers"]:
            assert container["image"] == f"{IMAGE_REPOSITORY}:{TEST_TAG}"
            assert container["imagePullPolicy"] == "Never"
