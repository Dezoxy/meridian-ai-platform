"""Kind turns the rate store on (S066, T-45).

Kind's values set ``rateStore.enabled``; ``deploy.sh`` gives the chart the store's
image from ``infra/kind/pins.env``; ``make up`` makes the Secret (the tests of that
are in ``test_kind_rate_store_secret.py``). What the chart does with the store
when it is on is ``test_helm_rate_store.py``'s, rendered with a made-up image.
Here the rendering is the one the cluster gets, from kind's values and the real
pin, and the store is the seventh workload beside the six services: the checks of
those six leave it out by name (``chartsupport.rendered_services``), and what is
true of the store alone is said here.
"""

import re
import shlex

import pytest
import yaml
from chartsupport import (
    CHART_DIR,
    NAMESPACE,
    RATE_STORE,
    RATE_STORE_IMAGE,
    SERVICES,
    VALUES_FILE,
    helm_arguments,
    pod_spec,
    pod_workloads,
    render,
    rendered_chart,
    run_helm,
)
from kindsupport import function_body
from servicesupport import REPO_ROOT

KIND_DIR = REPO_ROOT / "infra" / "kind"
MAKEFILE = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
PINS = (KIND_DIR / "pins.env").read_text(encoding="utf-8")
README = (KIND_DIR / "README.md").read_text(encoding="utf-8")
SECRET_NAME = "rate-store-credentials"  # noqa: S105 (a Secret name, not a password)
VARIABLE = "MERIDIAN_GATEWAY_RATE_STORE_URL"
GATEWAY = "model-gateway"
DIGEST_REFERENCE = re.compile(r"[a-z0-9][a-z0-9./-]*:[\w.-]+@sha256:[0-9a-f]{64}")
# The image's own account (docker-library/redis, alpine/Dockerfile).
IMAGE_USER, IMAGE_GROUP = 999, 1000


def named(kind: str, name: str = RATE_STORE) -> dict:
    (found,) = [
        d
        for d in rendered_chart()
        if d["kind"] == kind and d["metadata"]["name"] == name
    ]
    return found


def store_pod() -> dict:
    return named("Deployment")["spec"]["template"]["spec"]


def kind_values() -> dict:
    return yaml.safe_load(VALUES_FILE.read_text(encoding="utf-8"))


# ── the image ────────────────────────────────────────────────────────────────


def test_the_pin_is_one_reference_by_digest_in_the_shape_of_the_database_image() -> (
    None
):
    assert DIGEST_REFERENCE.fullmatch(RATE_STORE_IMAGE)
    # One line, which Renovate's reader for `name:tag@digest` takes without a
    # comment (as POSTGRES_IMAGE is).
    assert re.findall(r"^RATE_STORE_IMAGE=(\S+)$", PINS, re.MULTILINE) == [
        RATE_STORE_IMAGE
    ]


def test_the_cluster_runs_the_image_the_tests_and_ci_run_against() -> None:
    (tested,) = re.findall(r"^PYTEST_REDIS_IMAGE\s*:?=\s*(\S+)$", MAKEFILE, re.M)

    assert tested == RATE_STORE_IMAGE


def test_kind_names_no_image_for_the_store_because_deploy_passes_the_pin() -> None:
    block = kind_values()["rateStore"]

    # The pin has one place (pins.env) and one Renovate reader: no literal here.
    assert block == {"enabled": True, "secret": SECRET_NAME}
    assert "sha256:" not in VALUES_FILE.read_text(encoding="utf-8")


def test_the_stores_deployment_runs_the_pinned_image_and_pulls_it_by_digest() -> None:
    (container,) = store_pod()["containers"]

    assert container["image"] == RATE_STORE_IMAGE
    assert container["imagePullPolicy"] == "IfNotPresent"


def test_the_readme_lists_the_image_and_where_the_chart_takes_it() -> None:
    name_and_tag = RATE_STORE_IMAGE.partition("@")[0]

    assert f"`docker.io/library/{name_and_tag}`" in README
    assert "`rateStore.image` (`RATE_STORE_IMAGE`)" in README


# ── helm lint ────────────────────────────────────────────────────────────────


def lint_recipe() -> str:
    (recipe,) = re.findall(r"^helm-lint:\n\t(.+)$", MAKEFILE, re.MULTILINE)
    return recipe


def test_make_helm_lint_renders_the_store_it_is_given_the_image_of() -> None:
    recipe = lint_recipe()

    assert "-f infra/kind/values/meridian.yaml" in recipe
    assert "--set-string rateStore.image=$(PYTEST_REDIS_IMAGE)" in recipe
    # The recipe's own words, with the make variable read: the same image.
    assert RATE_STORE_IMAGE in recipe.replace("$(PYTEST_REDIS_IMAGE)", RATE_STORE_IMAGE)


def test_helm_lint_strict_passes_with_kinds_values_and_the_recipes_arguments() -> None:
    (_, _, arguments) = lint_recipe().partition("helm ")
    words = shlex.split(arguments.replace("$(PYTEST_REDIS_IMAGE)", RATE_STORE_IMAGE))
    words[words.index("infra/helm/meridian")] = str(CHART_DIR)
    words[words.index("infra/kind/values/meridian.yaml")] = str(VALUES_FILE)

    done = run_helm(words)

    assert done.returncode == 0, done.stdout + done.stderr
    # `helm lint` does not fail on the chart's own refusals: it logs them as
    # warnings and renders an empty value (Helm 4's lint mode of `required` and
    # `fail`), so a lint without the image passes too. What shows the store was
    # linted whole is that the warning is not there with the image and is there
    # without it.
    assert "rateStore.image" not in done.stdout + done.stderr
    at = words.index(f"rateStore.image={RATE_STORE_IMAGE}")
    without = run_helm([*words[: at - 1], *words[at + 1 :]])
    assert without.returncode == 0
    assert "rateStore.image is required" in without.stdout + without.stderr


# ── the store is the seventh workload ────────────────────────────────────────


def test_the_chart_holds_the_store_as_one_more_deployment_of_one_replica() -> None:
    deployments = {
        d["metadata"]["name"] for d in rendered_chart() if d["kind"] == "Deployment"
    }

    assert deployments == {*SERVICES, RATE_STORE}
    deployment = named("Deployment")
    assert deployment["spec"]["replicas"] == 1
    # A second store would hold half of each window, and a rolling update would
    # run two for a while.
    assert deployment["spec"]["strategy"] == {"type": "Recreate"}


def test_the_store_runs_as_the_images_own_user_and_group_and_nothing_wider() -> None:
    pod = store_pod()
    (container,) = pod["containers"]

    assert pod["securityContext"] == {
        "runAsNonRoot": True,
        "runAsUser": IMAGE_USER,
        "runAsGroup": IMAGE_GROUP,
        "fsGroup": IMAGE_GROUP,
        "seccompProfile": {"type": "RuntimeDefault"},
    }
    context = container["securityContext"]
    assert context["runAsUser"] == IMAGE_USER
    assert context["runAsNonRoot"] is True
    assert context["readOnlyRootFilesystem"] is True
    assert context["allowPrivilegeEscalation"] is False
    assert context["capabilities"]["drop"] == ["ALL"]
    assert pod["automountServiceAccountToken"] is False


def test_the_store_has_no_tmp_and_nothing_writable() -> None:
    pod = store_pod()
    (container,) = pod["containers"]

    # Redis writes nothing here: no snapshot, no append-only file, no socket.
    assert not [v for v in pod["volumes"] if "emptyDir" in v or "hostPath" in v]
    assert [m for m in container["volumeMounts"] if not m.get("readOnly")] == []
    assert {v["name"] for v in pod["volumes"]} == {"tls", "acl", "config"}


def test_the_store_mounts_its_certificate_the_acl_key_alone_and_its_configuration() -> (
    None
):
    pod = store_pod()
    volumes = {v["name"]: v for v in pod["volumes"]}

    assert volumes["tls"]["secret"]["secretName"] == f"{RATE_STORE}-tls"
    assert volumes["tls"]["secret"]["defaultMode"] == 0o440
    # Only the ACL file of the Secret reaches the pod: not the gateway's address.
    assert volumes["acl"]["secret"]["secretName"] == SECRET_NAME
    assert volumes["acl"]["secret"]["items"] == [
        {"key": "users.acl", "path": "users.acl"}
    ]
    assert volumes["acl"]["secret"]["defaultMode"] == 0o440
    assert volumes["config"]["configMap"]["name"] == RATE_STORE


def test_the_stores_certificate_carries_the_name_the_gateway_checks() -> None:
    certificate = named("Certificate")["spec"]
    kind = kind_values()["identity"]

    assert certificate["secretName"] == f"{RATE_STORE}-tls"
    assert certificate["dnsNames"] == [f"{RATE_STORE}.{NAMESPACE}.svc"]
    assert certificate["uris"] == [
        f"spiffe://{kind['trustDomain']}/ns/{NAMESPACE}/sa/{RATE_STORE}"
    ]
    assert certificate["issuerRef"]["name"] == kind["issuer"]["name"]
    assert certificate["usages"] == ["digital signature", "client auth", "server auth"]


def test_deploys_wait_for_certificates_covers_the_stores_by_its_label() -> None:
    deploy = (KIND_DIR / "deploy.sh").read_text(encoding="utf-8")
    body = function_body(deploy, "wait_for_certificates")
    (selector,) = re.findall(r"-l (\S+)", body)
    key, _, value = selector.partition("=")

    # `make deploy` waits for every Certificate that carries this label to be
    # Ready: the store's is one of them, so the store's pod is not waited for
    # while its Secret does not exist yet.
    assert named("Certificate")["metadata"]["labels"][key] == value


# ── who reads the Secret ─────────────────────────────────────────────────────


def secrets_read(workload: dict) -> set[str]:
    pod = pod_spec(workload)
    names = {v["secret"]["secretName"] for v in pod.get("volumes", []) if "secret" in v}
    names |= {
        item["valueFrom"]["secretKeyRef"]["name"]
        for container in pod["containers"]
        for item in container.get("env", [])
        if "secretKeyRef" in item.get("valueFrom", {})
    }
    return names


def test_the_secret_is_read_by_the_gateway_and_the_store_and_by_no_other_pod() -> None:
    readers = {
        w["metadata"]["name"]
        for w in pod_workloads(list(rendered_chart()))
        if SECRET_NAME in secrets_read(w)
    }

    assert readers == {GATEWAY, RATE_STORE}


def test_the_gateway_reads_the_address_through_a_required_reference() -> None:
    pod = named("Deployment", GATEWAY)["spec"]["template"]["spec"]
    (container,) = pod["containers"]
    (item,) = [i for i in container["env"] if i["name"] == VARIABLE]

    # No literal value, and no `optional`: a missing Secret or key keeps the pod
    # from starting instead of giving it windows of its own.
    assert item == {
        "name": VARIABLE,
        "valueFrom": {"secretKeyRef": {"name": SECRET_NAME, "key": "uri"}},
    }


def test_no_other_workload_is_given_the_address() -> None:
    holders = [
        w["metadata"]["name"]
        for w in pod_workloads(list(rendered_chart()))
        if VARIABLE
        in {
            item["name"]
            for container in pod_spec(w)["containers"]
            for item in container.get("env", [])
        }
    ]

    assert holders == [GATEWAY]


def test_kind_keeps_the_gateway_at_one_replica_and_says_why_in_a_comment() -> None:
    text = VALUES_FILE.read_text(encoding="utf-8")

    assert "replicas" not in kind_values()["services"][GATEWAY]
    assert named("Deployment", GATEWAY)["spec"]["replicas"] == 1
    assert "laptop's" in text and "memory" in text
    assert "T-45" in text


@pytest.mark.parametrize("replicas", [1, 2])
def test_with_the_store_on_the_chart_takes_one_gateway_replica_or_two(
    replicas: int,
) -> None:
    documents = render(
        [*helm_arguments(), "--set", f"services.{GATEWAY}.replicas={replicas}"]
    )

    (gateway,) = [
        d
        for d in documents
        if d["kind"] == "Deployment" and d["metadata"]["name"] == GATEWAY
    ]
    assert gateway["spec"]["replicas"] == replicas
