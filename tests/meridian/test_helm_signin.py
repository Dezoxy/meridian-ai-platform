"""The chart can turn the staff sign-in on for the Claims API, and it stays off
unless asked (S021, Y4b).

``signin.mode`` is ``off`` (the default) or ``staff``; anything else fails the
render. With ``staff`` the Claims API's container, and no other, is given the
switch ``MERIDIAN_SIGNIN``, each ``MERIDIAN_SIGNIN_STAFF_*`` variable the modules
read, the plain-HTTP-at-the-edge flag, and the two secret variables from the keys
of one Secret (a reference, never a value); its pod gets a pod annotation (so a
new Secret rolls it) and a ``dnsConfig``. Off, nothing of this is rendered.

What the render cannot show: that the pod starts with these settings under the
cluster's DNS, or that the issuer answers. The settings are built here from the
rendered environment by the code's own ``from_env`` functions, so a name or a
format that drifts fails; the resolver is a model of glibc's rule, not glibc.
"""

import base64
import functools
import re
from pathlib import Path

import pytest
import yaml
from chartsupport import CHART_DIR, REPO_ROOT, helm_arguments, render, run_helm

from meridian.platform.common.signin import SigninSettings
from meridian.platform.common.signinflow import FlowSettings
from meridian.platform.common.signinsession import SessionSettings

KIND_DIR = REPO_ROOT / "infra" / "kind"
SIGNIN_FILE = KIND_DIR / "values" / "signin.yaml"
GENERATION = "0123456789abcdef"
GENERATION_ANNOTATION = "meridian.local/signin-generation"
CLAIMS_API = "claims-api"
SECRET_NAME = "claims-api-signin"  # noqa: S105 (a Secret's name)
SESSION_KEY_NAME = "session-key"
CREDENTIAL_NAME = "client-credential"
STAFF = "MERIDIAN_SIGNIN_STAFF_"
# The variable of each value under signin.staff, as the modules read them.
STAFF_VARIABLES = {
    "issuer": "ISSUER",
    "audience": "AUDIENCE",
    "keysUrl": "KEYS_URL",
    "clientId": "CLIENT_ID",
    "authorizationUrl": "AUTHORIZATION_URL",
    "tokenUrl": "TOKEN_URL",
    "endSessionUrl": "END_SESSION_URL",
    "appOrigin": "APP_ORIGIN",
    "redirectUri": "REDIRECT_URI",
    "postLogoutRedirectUri": "POST_LOGOUT_REDIRECT_URI",
    "requiredTyp": "REQUIRED_TYP",
    "allowedAzp": "ALLOWED_AZP",
}
SIGNIN_ON = ["-f", str(SIGNIN_FILE), "--set-string", f"signin.generation={GENERATION}"]
SECRET_REFERENCES = {
    "MERIDIAN_SIGNIN_STAFF_CLIENT_CREDENTIAL": CREDENTIAL_NAME,
    "MERIDIAN_SESSION_KEY": SESSION_KEY_NAME,
}
SWITCHES = {
    "MERIDIAN_SIGNIN": "staff",
    "MERIDIAN_SESSION_EDGE_PLAIN_HTTP": "true",
}
# Stand-ins for the two values the Secret holds: no real value is in a test.
STAND_IN_CREDENTIAL = "stand-in-credential"
STAND_IN_SESSION_KEY = base64.b64encode(bytes(range(40))).decode()


def staff_variables() -> set[str]:
    return {STAFF + suffix for suffix in STAFF_VARIABLES.values()}


@functools.cache
def default_render() -> tuple[dict, ...]:
    return tuple(render(helm_arguments()))


@functools.cache
def staff_render() -> tuple[dict, ...]:
    return tuple(render([*helm_arguments(), *SIGNIN_ON]))


def staff_values() -> dict:
    return yaml.safe_load(SIGNIN_FILE.read_text(encoding="utf-8"))["signin"]


def workloads(documents: tuple[dict, ...]) -> dict[str, dict]:
    return {
        d["metadata"]["name"]: d
        for d in documents
        if d["kind"] in ("Deployment", "Job", "CronJob")
    }


def pod_template(workload: dict) -> dict:
    spec = workload["spec"]
    return (spec["jobTemplate"]["spec"] if workload["kind"] == "CronJob" else spec)[
        "template"
    ]


def env_items(workload: dict) -> list[dict]:
    return [
        item
        for container in pod_template(workload)["spec"]["containers"]
        for item in container.get("env", [])
    ]


def signin_names(workload: dict) -> list[str]:
    """The sign-in or session variables a workload's containers are given."""
    prefixes = ("MERIDIAN_SIGNIN", "MERIDIAN_SESSION")
    return [i["name"] for i in env_items(workload) if i["name"].startswith(prefixes)]


def refusal(*arguments: str) -> str:
    done = run_helm([*helm_arguments(), *arguments])
    assert done.returncode != 0, "the render should have been refused"
    return done.stderr


# ── off ──────────────────────────────────────────────────────────────────────


def test_the_chart_is_off_by_default_and_holds_no_value_of_a_secret() -> None:
    values = yaml.safe_load((CHART_DIR / "values.yaml").read_text(encoding="utf-8"))
    kind = yaml.safe_load((KIND_DIR / "values" / "meridian.yaml").read_text())

    assert values["signin"]["mode"] == "off"
    assert "signin" not in kind  # kind's own values leave it to signin.yaml
    assert values["signin"]["edgePlainHttp"] is False
    assert set(values["signin"]["staff"]) == set(STAFF_VARIABLES)
    assert all(value == "" for value in values["signin"]["staff"].values())


def test_off_renders_no_sign_in_variable_no_dns_config_and_no_annotation() -> None:
    for name, workload in workloads(default_render()).items():
        assert signin_names(workload) == [], name
        template = pod_template(workload)
        assert "dnsConfig" not in template["spec"], name
        notes = template["metadata"].get("annotations", {})
        assert GENERATION_ANNOTATION not in notes, name
        if name != "rate-store":  # its pod carries a checksum of its own
            assert "annotations" not in template["metadata"], name


def test_off_with_every_staff_value_present_renders_what_the_default_does() -> None:
    # The staff values and the generation are set, the mode is off: none of it
    # may show, byte for byte.
    plain = run_helm([*helm_arguments()])
    off = run_helm([*helm_arguments(), *SIGNIN_ON, "--set-string", "signin.mode=off"])

    assert plain.returncode == 0 and off.returncode == 0, off.stderr
    assert plain.stdout == off.stdout


def test_kinds_values_add_exactly_one_variable_to_the_claims_api() -> None:
    chart = yaml.safe_load((CHART_DIR / "values.yaml").read_text(encoding="utf-8"))
    kind = yaml.safe_load((KIND_DIR / "values" / "meridian.yaml").read_text())

    chart_env = chart["services"][CLAIMS_API]["env"]
    kind_env = kind["services"][CLAIMS_API]["env"]  # a list replaces, not merges
    assert kind_env[: len(chart_env)] == chart_env
    assert kind_env[len(chart_env) :] == [
        {"name": "MERIDIAN_ENVIRONMENT", "value": "kind"}
    ]
    (api,) = [d for n, d in workloads(default_render()).items() if n == CLAIMS_API]
    rendered = {i["name"]: i.get("value") for i in env_items(api)}
    assert rendered["MERIDIAN_ENVIRONMENT"] == "kind"


# ── staff ────────────────────────────────────────────────────────────────────


def test_staff_gives_the_claims_api_the_variables_and_no_other_workload() -> None:
    values = staff_values()
    documents = workloads(staff_render())

    items = {i["name"]: i for i in env_items(documents[CLAIMS_API])}
    expected = staff_variables() | set(SECRET_REFERENCES) | set(SWITCHES)
    mine = {n for n in items if n.startswith(("MERIDIAN_SIGNIN", "MERIDIAN_SESSION"))}
    assert mine == expected
    for value_name, suffix in STAFF_VARIABLES.items():
        assert items[STAFF + suffix]["value"] == values["staff"][value_name]
    for name, value in SWITCHES.items():
        assert items[name]["value"] == value
    for name, other in documents.items():
        if name == CLAIMS_API:
            continue
        assert signin_names(other) == [], name
        assert "dnsConfig" not in pod_template(other)["spec"], name
        assert GENERATION_ANNOTATION not in pod_template(other)["metadata"].get(
            "annotations", {}
        ), name


def test_the_two_secret_variables_are_references_to_the_secrets_keys() -> None:
    items = {i["name"]: i for i in env_items(workloads(staff_render())[CLAIMS_API])}

    for name, key in SECRET_REFERENCES.items():
        assert set(items[name]) == {"name", "valueFrom"}, name
        assert items[name]["valueFrom"] == {
            "secretKeyRef": {"name": SECRET_NAME, "key": key}
        }


def test_the_pods_annotation_carries_the_generation_and_nothing_more() -> None:
    template = pod_template(workloads(staff_render())[CLAIMS_API])

    assert template["metadata"]["annotations"] == {GENERATION_ANNOTATION: GENERATION}


def test_a_new_generation_changes_the_pod_template_and_only_that() -> None:
    first = pod_template(workloads(staff_render())[CLAIMS_API])
    other = render(
        [*helm_arguments(), *SIGNIN_ON, "--set-string", "signin.generation=fedcba98"]
    )
    second = pod_template(workloads(tuple(other))[CLAIMS_API])

    assert first != second
    first["metadata"]["annotations"] = second["metadata"]["annotations"]
    assert first == second


def test_the_rendered_environment_builds_the_three_settings_the_modules_read() -> None:
    items = env_items(workloads(staff_render())[CLAIMS_API])
    environ = {i["name"]: i["value"] for i in items if "value" in i}
    environ["MERIDIAN_SIGNIN_STAFF_CLIENT_CREDENTIAL"] = STAND_IN_CREDENTIAL
    environ["MERIDIAN_SESSION_KEY"] = STAND_IN_SESSION_KEY

    bearer = SigninSettings.from_env(environ, "staff")
    flow = FlowSettings.from_env(environ, "staff", return_prefixes=("/adjuster",))
    session = SessionSettings.from_env(environ)

    values = staff_values()["staff"]
    assert bearer.issuer == flow.issuer == values["issuer"]
    assert bearer.audience == "meridian-claims-api"
    assert bearer.required_typ == "Bearer"
    assert bearer.allowed_azp == ("meridian-claims-web", "meridian-scripts")
    assert bearer.keys_url == values["keysUrl"]
    assert flow.client_id == "meridian-claims-web"
    assert flow.token_url == values["tokenUrl"]
    assert session.secure is False  # plain HTTP at the edge, on kind


def test_the_authorised_parties_use_the_separator_the_code_reads() -> None:
    source = Path(SigninSettings.from_env.__code__.co_filename).read_text()

    assert 'azp.split(",")' in source
    assert "," in staff_values()["staff"]["allowedAzp"]
    assert " " not in staff_values()["staff"]["allowedAzp"]


# ── what it refuses ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("value", ["on", "Staff", "claimant", "", "true"])
def test_a_mode_that_is_neither_off_nor_staff_fails_the_render_naming_it(
    value: str,
) -> None:
    stderr = refusal("--set-string", f"signin.mode={value}")

    assert "signin.mode" in stderr
    assert "off" in stderr and "staff" in stderr


@pytest.mark.parametrize("value", sorted(STAFF_VARIABLES))
def test_staff_with_one_staff_value_empty_fails_naming_it(value: str) -> None:
    stderr = refusal(*SIGNIN_ON, "--set-string", f"signin.staff.{value}=")

    assert f"signin.staff.{value}" in stderr


@pytest.mark.parametrize("value", ["secret", "generation"])
def test_staff_with_no_secret_or_no_generation_fails_naming_it(value: str) -> None:
    stderr = refusal(*SIGNIN_ON, "--set-string", f"signin.{value}=")

    assert f"signin.{value}" in stderr


def test_staff_without_the_generation_deploy_passes_fails() -> None:
    stderr = refusal("-f", str(SIGNIN_FILE))

    assert "signin.generation" in stderr


@pytest.mark.parametrize(
    "variable",
    ["MERIDIAN_SIGNIN", "MERIDIAN_SIGNIN_STAFF_ISSUER", "MERIDIAN_SESSION_KEY"],
)
@pytest.mark.parametrize("mode", ["off", "staff"])
def test_a_values_env_item_naming_a_sign_in_or_session_variable_is_refused(
    tmp_path: Path, variable: str, mode: str
) -> None:
    extra = tmp_path / "extra.yaml"
    extra.write_text(
        yaml.safe_dump(
            {"services": {"model-gateway": {"env": [{"name": variable, "value": "x"}]}}}
        ),
        encoding="utf-8",
    )
    stderr = refusal(
        *SIGNIN_ON, "--set-string", f"signin.mode={mode}", "-f", str(extra)
    )

    assert variable in stderr
    assert "model-gateway" in stderr


def test_the_refusal_does_not_catch_the_environment_variable_or_a_near_name(
    tmp_path: Path,
) -> None:
    extra = tmp_path / "extra.yaml"
    items = [
        {"name": "MERIDIAN_ENVIRONMENT", "value": "kind"},
        {"name": "MERIDIAN_SIGNING_NOTE", "value": "x"},
    ]
    extra.write_text(
        yaml.safe_dump({"services": {"model-gateway": {"env": items}}}),
        encoding="utf-8",
    )

    assert render([*helm_arguments(), "-f", str(extra)])


# ── the pod's resolver ───────────────────────────────────────────────────────

SEARCH = ("meridian.svc.cluster.local", "svc.cluster.local", "cluster.local")
# The names the Claims API's pod resolves with the sign-in on: its environment's
# addresses, less the ones only a browser opens (the front URL, on *.localhost),
# and the database, whose address `make up` puts in a Secret.
RESOLVED_BY_THE_POD = {
    "agent-runtime.meridian.svc",
    "otel-collector.observability.svc.cluster.local",
    "keycloak.identity.svc",
    "platform-db-rw.meridian.svc",
}


def candidates(name: str, ndots: int, search: tuple[str, ...] = SEARCH) -> list[str]:
    """The names glibc asks for, in order: a name with at least ``ndots`` dots is
    asked as it is first, one with fewer dots after the search list."""
    if name.endswith("."):
        return [name]
    absolute = f"{name}."
    suffixed = [f"{name}.{suffix}." for suffix in search]
    return [absolute, *suffixed] if name.count(".") >= ndots else [*suffixed, absolute]


def reaches_the_cluster_first(name: str, ndots: int) -> bool:
    """Resolves, and the first name asked is inside the cluster's own zone: a name
    like ``keycloak.identity.svc`` asked as it is leaves the cluster (the node's
    resolver) before the search list is tried, and a resolver that answers for
    names that do not exist would answer it."""
    target = name if name.endswith("cluster.local") else f"{name}.cluster.local"
    asked = candidates(name, ndots)
    return f"{target}." in asked and asked[0].endswith("cluster.local.")


def resolver_options() -> dict[str, str]:
    spec = pod_template(workloads(staff_render())[CLAIMS_API])["spec"]
    return {o["name"]: o["value"] for o in spec["dnsConfig"]["options"]}


def hosts_in_the_environment() -> set[str]:
    hosts = set()
    for item in env_items(workloads(staff_render())[CLAIMS_API]):
        for found in re.findall(r"https?://([^/:\s\"]+)", item.get("value", "")):
            if not found.endswith(".localhost"):
                hosts.add(found)
    database = re.search(
        r"^readonly DATABASE_HOST=(\S+)$", (KIND_DIR / "up.sh").read_text(), re.M
    )
    assert database
    return hosts | {database.group(1)}


def test_the_resolver_has_a_one_second_timeout_two_attempts_and_a_low_ndots() -> None:
    assert resolver_options() == {"timeout": "1", "attempts": "2", "ndots": "3"}


def test_the_names_the_pod_resolves_are_the_ones_checked_here() -> None:
    assert hosts_in_the_environment() == RESOLVED_BY_THE_POD


@pytest.mark.parametrize("name", sorted(RESOLVED_BY_THE_POD))
def test_every_name_the_pod_resolves_still_resolves_in_the_cluster_first(
    name: str,
) -> None:
    assert reaches_the_cluster_first(name, int(resolver_options()["ndots"]))


@pytest.mark.parametrize("ndots", [0, 1, 2])
def test_an_ndots_under_the_dots_of_a_service_name_would_ask_the_node_first(
    ndots: int,
) -> None:
    # keycloak.identity.svc has two dots: with ndots 2 or less it is asked as it
    # is, outside the cluster's zone, before the search list is tried.
    assert not reaches_the_cluster_first("keycloak.identity.svc", ndots)


@pytest.mark.parametrize("ndots", [3, 4, 5])
def test_an_ndots_above_the_dots_of_a_service_name_keeps_it_in_the_cluster(
    ndots: int,
) -> None:
    for name in RESOLVED_BY_THE_POD:
        assert reaches_the_cluster_first(name, ndots), name
