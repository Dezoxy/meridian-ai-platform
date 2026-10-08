"""The switch of the staff sign-in on kind: MERIDIAN_SIGNIN (S021, Y4b).

``MERIDIAN_SIGNIN`` is empty or ``off`` (the default) or ``staff``; any other word is
a usage line, from every script that reads it (``up.sh``, ``deploy.sh``,
``smoke.sh``, ``identity.sh``) before it does anything. With ``staff``:

- ``deploy.sh`` refuses, before its first change, unless ``MERIDIAN_IDENTITY`` is
  ``keycloak`` and the Secret ``claims-api-signin`` exists with a generation (it reads
  the name and that one annotation, never the data), and then adds
  ``values/signin.yaml`` and the generation to the release;
- the adjuster-pages check of ``smoke.sh`` expects the queue to answer 303 to the
  app's start, follows it once and expects a 303 to the issuer's authorization
  address, and prints neither query. It still prints three PASS lines.

Everything runs in bash against stand-ins for ``kubectl``, ``curl`` and the rest: no
cluster, no container. What none of this proves: that the app answers those 303s (the
Python side is another contract's), that Envoy forwards them, that the issuer shows
its login page.
"""

import json
import os
import re
import stat
import subprocess
from pathlib import Path

import pytest
import yaml
from kindsupport import (
    KIND_DIR,
    SMOKE_SH,
    function_definition,
    requires_jq,
)

pytestmark = requires_jq

DEPLOY_SH = (KIND_DIR / "deploy.sh").read_text(encoding="utf-8")
IDENTITY_SH = (KIND_DIR / "identity.sh").read_text(encoding="utf-8")
COMMON_SH = (KIND_DIR / "common.sh").read_text(encoding="utf-8")
SIGNIN_FILE = KIND_DIR / "values" / "signin.yaml"
USAGE = "MERIDIAN_SIGNIN must be"
GENERATION = "0123456789abcdef"
SECRET_NAME = "claims-api-signin"  # noqa: S105 (a Secret's name)
ORIGIN = "http://claims.meridian.localhost:8088"
AUTHORIZATION = "http://id.meridian.localhost:8088/realms/meridian-staff/protocol/openid-connect/auth"
# Stand-ins for what an address carries after its `?`: nothing real.
QUERY_MARK = "stand-in-query"


def stubs_in(directory: Path, *names: str) -> Path:
    """A folder of stand-in commands that log their arguments and fail."""
    directory.mkdir(parents=True, exist_ok=True)
    for name in names:
        path = directory / name
        path.write_text(
            '#!/usr/bin/env bash\necho "$0 $*" >>"${STUB_LOG}"\nexit 99\n',
            encoding="utf-8",
        )
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return directory


def bash(script: str, **env: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", f"set -euo pipefail\n{script}"],
        capture_output=True,
        text=True,
        env={"PATH": os.environ["PATH"], **env},
        check=False,
    )


# ── the switch ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("value", [None, "", "off", "staff"])
def test_the_values_that_are_accepted(value: str | None) -> None:
    env = {} if value is None else {"MERIDIAN_SIGNIN": value}

    done = bash(f'. "{KIND_DIR}/common.sh"\nsignin_switch_check', **env)

    assert done.returncode == 0, done.stderr


@pytest.mark.parametrize(
    "value", ["on", "Staff", "STAFF", "keycloak", "1", "true", " "]
)
def test_any_other_word_is_a_usage_line(value: str) -> None:
    done = bash(f'. "{KIND_DIR}/common.sh"\nsignin_switch_check', MERIDIAN_SIGNIN=value)

    assert done.returncode != 0
    assert USAGE in done.stderr
    assert "empty or off" in done.stderr and "staff" in done.stderr


@pytest.mark.parametrize(
    ("value", "on"), [(None, False), ("", False), ("off", False), ("staff", True)]
)
def test_only_staff_turns_it_on(value: str | None, on: bool) -> None:
    env = {} if value is None else {"MERIDIAN_SIGNIN": value}

    done = bash(f'. "{KIND_DIR}/common.sh"\nsignin_on', **env)

    assert (done.returncode == 0) is on


@pytest.mark.parametrize("script", ["up.sh", "deploy.sh", "smoke.sh", "identity.sh"])
def test_a_script_that_reads_the_switch_refuses_a_bad_word_before_it_asks_anything(
    tmp_path: Path, script: str
) -> None:
    log = tmp_path / "calls"
    stubs = stubs_in(
        tmp_path / "bin", "kubectl", "docker", "helm", "kind", "curl", "git"
    )

    done = subprocess.run(
        ["bash", str(KIND_DIR / script), "up"],
        capture_output=True,
        text=True,
        env={
            "PATH": f"{stubs}:{os.environ['PATH']}",
            "STUB_LOG": str(log),
            "MERIDIAN_SIGNIN": "on",
            "HOME": str(tmp_path),
            "CLUSTER_HOLDER": "tests",
        },
        check=False,
        timeout=60,
    )

    assert done.returncode != 0
    assert USAGE in done.stderr
    assert not log.exists()


def test_the_scripts_that_read_it_are_the_four_and_no_other() -> None:
    readers = sorted(
        path.name
        for path in KIND_DIR.glob("*.sh")
        if "signin_switch_check" in path.read_text(encoding="utf-8")
        and path.name != "common.sh"
    )

    assert readers == ["deploy.sh", "identity.sh", "smoke.sh", "up.sh"]


def test_up_reads_the_switch_once_and_before_it_does_anything() -> None:
    lines = (KIND_DIR / "up.sh").read_text(encoding="utf-8").splitlines()
    code = [line for line in lines if not line.startswith("#")]

    assert code.count("signin_switch_check") == 1
    assert code.index("signin_switch_check") < code.index("check_prerequisites")
    # The issuer add-on stays the one block that runs identity.sh: the switch adds
    # to what that script makes and is no second call of it.
    assert sum("identity.sh" in line for line in code) == 2


# ── deploy ───────────────────────────────────────────────────────────────────


ISSUER_GENERATION = "fedcba9876543210"
ISSUER_SECRET = "keycloak-credentials"  # noqa: S105 (a Secret's name)


def deploy_functions(
    tmp_path: Path,
    secret: str,
    *,
    identity: str,
    signin: str,
    issuer: str = "same",
):
    """``require_signin`` and ``install_release`` of deploy.sh, run in bash against
    a stand-in ``kubectl`` and a ``helm_chart`` that prints its arguments. The
    stand-in answers the reads of the Secret: ``secret`` is ``absent``, ``bare`` (no
    generation) or ``made`` (the generation GENERATION). ``issuer`` is the
    generation ``keycloak-credentials`` carries: ``same`` (GENERATION), ``other``
    (ISSUER_GENERATION), ``bare`` (none) or ``absent`` (the read fails)."""
    log = tmp_path / "calls"
    stubs = tmp_path / "bin"
    stubs.mkdir(parents=True)
    kubectl = stubs / "kubectl"
    kubectl.write_text(
        "#!/usr/bin/env bash\n"
        'echo "$*" >>"${STUB_LOG}"\n'
        'case "$*" in\n'
        '  *"get secret claims-api-signin -o name"*)\n'
        '    [[ "${SECRET}" == absent ]] || echo secret/claims-api-signin ;;\n'
        '  *"get secret claims-api-signin -o jsonpath={.metadata.annotations."*)\n'
        f'    [[ "${{SECRET}}" == bare ]] || printf %s {GENERATION} ;;\n'
        f'  *"-n identity get secret {ISSUER_SECRET} -o jsonpath='
        '{.metadata.annotations."*)\n'
        '    case "${ISSUER}" in\n'
        f"      same) printf %s {GENERATION} ;;\n"
        f"      other) printf %s {ISSUER_GENERATION} ;;\n"
        "      bare) ;;\n"
        '      *) echo "Error from server (NotFound)" >&2; exit 1 ;;\n'
        "    esac ;;\n"
        '  *) echo "unexpected: $*" >&2; exit 99 ;;\n'
        "esac\n",
        encoding="utf-8",
    )
    kubectl.chmod(kubectl.stat().st_mode | stat.S_IXUSR)
    constants = "\n".join(
        re.findall(
            r"^(?:readonly (?:SIGNIN_(?:VALUES_FILE|GENERATION_KEY|ISSUER_\w+)"
            r"|HELM_UPGRADE_TIMEOUT)|signin_generation)=.*$",
            DEPLOY_SH,
            re.M,
        )
    )
    script = "\n".join(
        [
            f'. "{KIND_DIR}/common.sh"',
            "NAMESPACE=meridian",
            "tag=0123456789ab",
            constants,
            # install_release sends the call's standard output away: this one is
            # read from standard error.
            'helm_chart() { printf "HELM %s\\n" "$*" >&2; }',
            function_definition(DEPLOY_SH, "require_signin"),
            function_definition(DEPLOY_SH, "install_release"),
            "require_signin",
            "install_release",
        ]
    )
    done = bash(
        script,
        PATH=f"{stubs}:{os.environ['PATH']}",
        STUB_LOG=str(log),
        SECRET=secret,
        ISSUER=issuer,
        MERIDIAN_IDENTITY=identity,
        MERIDIAN_SIGNIN=signin,
    )
    calls = log.read_text().splitlines() if log.exists() else []
    return done, calls


def helm_line(done: subprocess.CompletedProcess[str]) -> str:
    (line,) = [x for x in done.stderr.splitlines() if x.startswith("HELM ")]
    return line


def test_without_the_switch_deploy_reads_nothing_and_passes_nothing_extra(
    tmp_path: Path,
) -> None:
    done, calls = deploy_functions(tmp_path, "made", identity="", signin="")

    assert done.returncode == 0, done.stderr
    assert calls == []
    assert "signin" not in helm_line(done)
    assert "-f" not in helm_line(done).split()


def test_with_the_switch_off_it_is_the_same_as_without(tmp_path: Path) -> None:
    done, calls = deploy_functions(tmp_path, "made", identity="keycloak", signin="off")

    assert done.returncode == 0, done.stderr
    assert calls == [] and "signin" not in helm_line(done)


def test_staff_adds_the_values_file_and_the_secrets_generation_to_the_release(
    tmp_path: Path,
) -> None:
    done, calls = deploy_functions(
        tmp_path, "made", identity="keycloak", signin="staff"
    )

    assert done.returncode == 0, done.stderr
    words = helm_line(done).split()
    assert words[words.index("-f") + 1] == str(SIGNIN_FILE)
    assert f"signin.generation={GENERATION}" in words
    assert words[words.index("--set-string") + 1] == f"signin.generation={GENERATION}"
    # By name, and the one annotation of each Secret: the data is never read.
    assert len(calls) == 3
    assert "get secret claims-api-signin -o name --ignore-not-found" in calls[0]
    note = "jsonpath={.metadata.annotations.meridian\\.local/identity-generation}"
    assert note in calls[1] and "-n meridian" in calls[1]
    assert (
        note in calls[2] and "-n identity get secret keycloak-credentials" in (calls[2])
    )
    assert not any(".data" in call for call in calls)
    assert GENERATION in done.stdout  # the log says which generation (no secret)


def test_staff_without_the_issuer_add_on_is_refused_and_nothing_is_read(
    tmp_path: Path,
) -> None:
    done, calls = deploy_functions(tmp_path, "made", identity="", signin="staff")

    assert done.returncode != 0
    assert "MERIDIAN_SIGNIN=staff needs MERIDIAN_IDENTITY=keycloak" in done.stderr
    assert "nothing was changed" in done.stderr
    assert calls == []


def test_staff_with_a_mistyped_issuer_switch_is_refused_as_not_keycloak(
    tmp_path: Path,
) -> None:
    done, calls = deploy_functions(tmp_path, "made", identity="dex", signin="staff")

    assert done.returncode != 0
    assert "needs MERIDIAN_IDENTITY=keycloak (exactly that word)" in done.stderr
    assert calls == []


def test_staff_with_no_secret_is_refused_naming_the_command_that_makes_it(
    tmp_path: Path,
) -> None:
    done, calls = deploy_functions(
        tmp_path, "absent", identity="keycloak", signin="staff"
    )

    assert done.returncode != 0
    assert f"the Secret {SECRET_NAME} is not in meridian" in done.stderr
    assert "MERIDIAN_IDENTITY=keycloak MERIDIAN_SIGNIN=staff make up" in done.stderr
    assert "nothing was changed" in done.stderr
    assert len(calls) == 1
    assert "HELM" not in done.stderr


def test_staff_with_a_secret_that_has_no_generation_is_refused(tmp_path: Path) -> None:
    done, _ = deploy_functions(tmp_path, "bare", identity="keycloak", signin="staff")

    assert done.returncode != 0
    assert "carries no generation" in done.stderr
    assert "HELM" not in done.stderr


# L1: the Secret must be of the generation the issuer's credentials carry now.


def test_a_secret_of_the_issuers_generation_is_accepted(tmp_path: Path) -> None:
    done, _ = deploy_functions(
        tmp_path, "made", identity="keycloak", signin="staff", issuer="same"
    )

    assert done.returncode == 0, done.stderr
    assert "HELM" in done.stderr


def test_a_secret_of_an_older_generation_than_the_issuers_is_refused_before_a_change(
    tmp_path: Path,
) -> None:
    # A rotation without the switch made new credentials; this Secret still holds
    # the old client's secret, and every sign-in would fail.
    done, calls = deploy_functions(
        tmp_path, "made", identity="keycloak", signin="staff", issuer="other"
    )

    assert done.returncode != 0
    assert f"the Secret {SECRET_NAME} is of generation {GENERATION}" in done.stderr
    assert f"{ISSUER_SECRET} of generation {ISSUER_GENERATION}" in done.stderr
    assert "MERIDIAN_IDENTITY=keycloak MERIDIAN_SIGNIN=staff make up" in done.stderr
    assert "nothing was changed" in done.stderr
    assert "HELM" not in done.stderr
    assert not any(
        re.search(r" (apply|create|delete|patch|label|annotate) ", c) for c in calls
    )


def test_credentials_with_no_generation_are_refused_too(tmp_path: Path) -> None:
    done, _ = deploy_functions(
        tmp_path, "made", identity="keycloak", signin="staff", issuer="bare"
    )

    assert done.returncode != 0
    assert f"{ISSUER_SECRET} carries no generation" in done.stderr
    assert "HELM" not in done.stderr


def test_credentials_that_cannot_be_read_are_refused_too(tmp_path: Path) -> None:
    done, _ = deploy_functions(
        tmp_path, "made", identity="keycloak", signin="staff", issuer="absent"
    )

    assert done.returncode != 0
    assert f"could not read the generation of {ISSUER_SECRET}" in done.stderr
    assert "nothing was changed" in done.stderr
    assert "HELM" not in done.stderr


def test_the_issuers_secret_is_named_as_identity_sh_names_it() -> None:
    assert shell_constant(DEPLOY_SH, "SIGNIN_ISSUER_SECRET") == (
        shell_constant(IDENTITY_SH, "IDENTITY_CREDENTIALS_SECRET")
    )
    assert shell_constant(DEPLOY_SH, "SIGNIN_ISSUER_NAMESPACE") == (
        shell_constant(IDENTITY_SH, "IDENTITY_NAMESPACE")
    )


# L2: what a deploy says about the switch.


def test_a_deploy_without_the_switch_says_the_pages_are_open(tmp_path: Path) -> None:
    for signin in ("", "off"):
        done, calls = deploy_functions(
            tmp_path / (signin or "empty"), "made", identity="", signin=signin
        )

        assert done.returncode == 0, done.stderr
        assert calls == []  # it only says so
        said = [x for x in done.stdout.splitlines() if "sign-in is OFF" in x]
        assert len(said) == 1
        assert "adjuster's pages are open to whoever reaches them" in said[0]
        assert "MERIDIAN_IDENTITY=keycloak MERIDIAN_SIGNIN=staff make deploy" in said[0]


def test_a_deploy_with_the_switch_does_not_say_it_is_off(tmp_path: Path) -> None:
    done, _ = deploy_functions(tmp_path, "made", identity="keycloak", signin="staff")

    assert "OFF" not in done.stdout


def rollout_failure(signin: str) -> subprocess.CompletedProcess[str]:
    script = "\n".join(
        [
            f'. "{KIND_DIR}/common.sh"',
            "NAMESPACE=meridian",
            *re.findall(r"^readonly ROLLOUT_TIMEOUT=.*$", DEPLOY_SH, re.M),
            "kctl() { return 1; }",
            function_definition(DEPLOY_SH, "wait_for_deployment"),
            "wait_for_deployment claims-api",
        ]
    )
    return bash(script, MERIDIAN_SIGNIN=signin)


def test_a_failed_rollout_of_a_staff_deploy_says_the_old_pod_may_still_serve() -> None:
    done = rollout_failure("staff")

    assert done.returncode != 0
    assert "deployment claims-api did not roll out" in done.stderr
    assert "previous pod, without the sign-in, may still be serving" in done.stderr


@pytest.mark.parametrize("signin", ["", "off"])
def test_a_failed_rollout_without_the_switch_says_what_it_always_said(
    signin: str,
) -> None:
    done = rollout_failure(signin)

    assert done.returncode != 0
    assert "deployment claims-api did not roll out" in done.stderr
    assert "sign-in" not in done.stderr


def test_require_signin_runs_before_the_first_change_and_reads_only() -> None:
    lines = [line.strip() for line in DEPLOY_SH.splitlines()]

    assert lines.index("record_cluster_holder changing") < lines.index("require_signin")
    assert lines.index("require_signin") < lines.index("require_database")
    assert lines.index("require_signin") < lines.index("apply_alert_rules")
    assert lines.index("apply_alert_rules") < lines.index("build_image")
    # The switch itself is checked before any tool is looked for.
    assert DEPLOY_SH.index("signin_switch_check") < DEPLOY_SH.index("need_tools ")
    body = function_definition(DEPLOY_SH, "require_signin")
    assert re.findall(r"kctl [^\n]*", body) and not re.search(
        r"kctl [^\n]*(apply|create|delete|patch|label|annotate)", body
    )


def test_the_generation_annotation_is_the_one_identity_sh_writes() -> None:
    (written,) = re.findall(
        r"^readonly IDENTITY_GENERATION_KEY=(\S+)$", IDENTITY_SH, re.M
    )
    (read,) = re.findall(r"^readonly SIGNIN_GENERATION_KEY=(\S+)$", DEPLOY_SH, re.M)

    assert read == written


# ── ties between files ───────────────────────────────────────────────────────


def values() -> dict:
    return yaml.safe_load(SIGNIN_FILE.read_text(encoding="utf-8"))["signin"]


def shell_constant(text: str, name: str) -> str:
    (value,) = re.findall(rf'^readonly {name}="?([^"\n]+)"?$', text, re.M)
    return value


def test_the_redirect_address_is_the_one_identity_sh_gives_the_realm() -> None:
    origin = shell_constant(IDENTITY_SH, "IDENTITY_CLAIMS_ORIGIN")
    redirect = shell_constant(IDENTITY_SH, "IDENTITY_REDIRECT_URI")

    assert redirect == "${IDENTITY_CLAIMS_ORIGIN}/auth/callback"
    assert values()["staff"]["appOrigin"] == origin
    assert values()["staff"]["redirectUri"] == f"{origin}/auth/callback"


def test_the_return_after_sign_out_is_the_one_identity_sh_gives_the_realm() -> None:
    origin = shell_constant(IDENTITY_SH, "IDENTITY_CLAIMS_ORIGIN")
    post = shell_constant(IDENTITY_SH, "IDENTITY_POST_LOGOUT_URI")

    assert post == "${IDENTITY_CLAIMS_ORIGIN}/adjuster/claims"
    assert values()["staff"]["postLogoutRedirectUri"] == f"{origin}/adjuster/claims"


def test_the_app_origin_is_the_claims_apis_host_on_the_edges_port() -> None:
    kind = yaml.safe_load((KIND_DIR / "values" / "meridian.yaml").read_text())
    host = kind["route"]["hostname"]
    ports = yaml.safe_load((KIND_DIR / "cluster.yaml").read_text())

    assert values()["staff"]["appOrigin"] == f"http://{host}:8088"
    assert "8088" in json.dumps(ports)


def test_the_return_after_sign_out_is_the_queue_under_the_origin() -> None:
    staff = values()["staff"]

    assert staff["postLogoutRedirectUri"] == f"{staff['appOrigin']}/adjuster/claims"
    assert staff["postLogoutRedirectUri"] == SMOKE_SH_QUEUE


SMOKE_SH_QUEUE = shell_constant(SMOKE_SH, "ADJUSTER_QUEUE_URL")


def test_the_issuer_and_its_addresses_are_the_discovery_documents_of_the_run() -> None:
    document = json.loads(
        (
            KIND_DIR.parents[1]
            / "tests"
            / "meridian"
            / "common"
            / "fixtures"
            / "discovery-kind-2026-10-08.json"
        ).read_text(encoding="utf-8")
    )
    staff = values()["staff"]
    service = "http://keycloak.identity.svc:8080"
    host = shell_constant(IDENTITY_SH, "IDENTITY_ROUTE_HOST")
    assert shell_constant(IDENTITY_SH, "IDENTITY_FRONT_URL") == (
        "http://${IDENTITY_ROUTE_HOST}:8088"
    )
    front = f"http://{host}:8088"

    assert staff["issuer"] == document["issuer"] == f"{front}/realms/meridian-staff"
    assert staff["authorizationUrl"] == document["authorization_endpoint"]
    assert staff["endSessionUrl"] == document["end_session_endpoint"]
    # The back channel is the same path on the Service's name.
    path = lambda url: url.removeprefix(document["issuer"])  # noqa: E731
    assert staff["tokenUrl"] == service + "/realms/meridian-staff" + path(
        document["token_endpoint"]
    )
    assert staff["keysUrl"] == service + "/realms/meridian-staff" + path(
        document["jwks_uri"]
    )


def test_smoke_expects_the_authorization_address_the_values_name() -> None:
    assert (
        shell_constant(SMOKE_SH, "ADJUSTER_SIGNIN_AUTHORIZATION")
        == (values()["staff"]["authorizationUrl"])
    )
    assert shell_constant(SMOKE_SH, "ADJUSTER_SIGNIN_START_PATH") == "/auth/start"


def test_the_secret_is_named_once_and_the_values_name_it() -> None:
    assert shell_constant(COMMON_SH, "SIGNIN_SECRET") == SECRET_NAME
    assert values()["secret"] == SECRET_NAME
    for text in (IDENTITY_SH, DEPLOY_SH):
        assert "readonly SIGNIN_SECRET=" not in text  # one definition, in common.sh


def test_the_audience_and_the_clients_are_the_realms() -> None:
    realm = (KIND_DIR / "identity-realm.sh").read_text(encoding="utf-8")
    staff = values()["staff"]

    assert (
        f"API_AUDIENCE={staff['audience']}" in realm.replace("'", "").replace('"', "")
        or staff["audience"] in realm
    )
    assert staff["clientId"] in realm
    assert staff["allowedAzp"] == f"{staff['clientId']},meridian-scripts"
    assert "meridian-scripts" in realm
