"""Smoke's check 13: the sign-in issuer, only when the add-on is on (S021, Y2b).

``check_issuer`` in ``infra/kind/smoke.d/13-issuer.sh`` prints one SKIP line while
``MERIDIAN_IDENTITY`` is off, and six lines when it is ``keycloak``: the pod is
Ready and runs the pinned image (init container and environment too), the discovery
document through the edge names the front URL as its issuer, the key document holds
a signing key, four paths are the edge's own empty 404, four ways of climbing to the
master realm are 404 (sent as written), and, from the Claims API's pod, the
discovery document fetched by the Service's name names the front URL. No line reads
a Secret or signs anyone in.

The function runs in bash against stand-ins for ``kctl`` and ``curl``. What no test
here proves: that Envoy forwards the four prefixes and keeps the rest closed, that
the cluster's ``imageID`` is the index digest, and that the pod is ready on the
cluster at all.
"""

import json
import os
import re
import shlex
import subprocess
from pathlib import Path

import pytest
from kindsupport import (
    KIND_DIR,
    SMOKE_SH,
    function_body,
    function_definition,
    one_line_function,
    requires_jq,
)

pytestmark = requires_jq

PINS = (KIND_DIR / "pins.env").read_text(encoding="utf-8")
(IMAGE,) = re.findall(r"^KEYCLOAK_IMAGE=(\S+)$", PINS, re.M)
DIGEST = IMAGE.rsplit("@", 1)[1]
FRONT = "http://id.meridian.localhost:8088"
REALM = f"{FRONT}/realms/meridian-staff"
FUNCTIONS = (
    "issuer_get",
    "check_issuer_pod",
    "check_issuer_discovery",
    "check_issuer_keys",
    "issuer_edge_404",
    "check_issuer_closed",
    "issuer_local_path",
    "issuer_in_allowed_prefix",
    "check_issuer_climbing",
    "check_issuer_inside",
    "check_issuer",
)
# Through the edge, each must be the edge's own 404: the status and an empty body.
CLOSED = [
    "/admin/",
    "/realms/master/",
    "/realms/meridian-staff/account/",
    "/realms/meridian-staff/clients-registrations/openid-connect",
]
CLIMBING = [
    "/realms/meridian-staff/../master/",
    "/realms/meridian-staff/%2e%2e/master/",
    "/realms/meridian-staff/..%2fmaster/",
    "/realms/meridian-staff/.well-known/../../master/",
]
INSIDE = "http://keycloak.identity.svc:8080/realms/meridian-staff"
# The six lines in order: the pod, the discovery document, the keys, the closed
# paths, the ways of climbing, and the question from inside the cluster.
PASSING = ["PASS"] * 6


def pod(
    *,
    ready: bool = True,
    image: str = IMAGE,
    image_id: str | None = None,
    init_image: str = IMAGE,
    env: bool = False,
) -> dict:
    container: dict = {"name": "keycloak", "image": image}
    if env:
        container["env"] = [{"name": "KC_SOMETHING", "value": "x"}]
    return {
        "spec": {
            "initContainers": [{"name": "copy-quarkus", "image": init_image}],
            "containers": [container],
        },
        "status": {
            "conditions": [{"type": "Ready", "status": "True" if ready else "False"}],
            "containerStatuses": [
                {
                    "name": "keycloak",
                    "imageID": image_id or f"quay.io/keycloak/keycloak@{DIGEST}",
                }
            ],
        },
    }


GOOD_POD = json.dumps({"items": [pod()]})
GOOD_DISCOVERY = json.dumps({"issuer": REALM, "jwks_uri": "http://x/certs"})
GOOD_KEYS = json.dumps(
    {
        "keys": [
            {"kid": "a", "use": "enc", "alg": "RSA-OAEP"},
            {"kid": "b", "use": "sig", "alg": "RS256"},
        ]
    }
)


def run_check(
    tmp_path: Path,
    *,
    identity: str = "keycloak",
    pods: str = GOOD_POD,
    pods_fail: bool = False,
    answers: dict[str, tuple[str, ...]] | None = None,
    curl_fail: str = "",
    deployed: bool = True,
    inside: str = REALM,
    inside_fail: bool = False,
) -> tuple[list[str], list[str], list[str]]:
    """The PASS, FAIL and SKIP lines, the ``kctl`` calls and the ``curl`` calls.
    ``inside`` is what the Claims API's pod prints as the issuer; ``deployed``
    False leaves smoke's list of Meridian Deployments empty."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    calls, fetched = tmp_path / "kctl-calls", tmp_path / "curl-calls"
    calls.touch()
    fetched.touch()
    table = {
        f"{REALM}/.well-known/openid-configuration": ("200", GOOD_DISCOVERY),
        f"{REALM}/protocol/openid-connect/certs": ("200", GOOD_KEYS),
        **{f"{FRONT}{path}": ("404", "") for path in (*CLOSED, *CLIMBING)},
        **(answers or {}),
    }
    answers_script = ["declare -A STATUS BODY LOCATION"]
    for n, (url, answer) in enumerate(table.items()):
        status, body, *location = answer
        body_file = tmp_path / f"body-{n}"
        body_file.write_text(body)
        answers_script.append(f"STATUS[{shlex.quote(url)}]={shlex.quote(status)}")
        answers_script.append(f"BODY[{shlex.quote(url)}]={shlex.quote(str(body_file))}")
        where = location[0] if location else ""
        answers_script.append(f"LOCATION[{shlex.quote(url)}]={shlex.quote(where)}")
    script = "\n".join(
        [
            "set -euo pipefail",
            "failures=0; skips=0",
            f"KIND_DIR={KIND_DIR}",
            f"KEYCLOAK_IMAGE={IMAGE}",
            'pass() { echo "PASS  $*"; }',
            'fail() { echo "FAIL  $*"; }',
            'skip() { echo "SKIP  $*"; }',
            'identity_on() { [[ "${MERIDIAN_IDENTITY-}" == keycloak ]]; }',
            one_line_function(SMOKE_SH, "clean_lines"),
            *re.findall(r"^readonly ISSUER_[A-Z_]+=.*$", SMOKE_SH, re.M),
            *re.findall(r'^issuer_[a-z_]+=""$', SMOKE_SH, re.M),
            *(function_definition(SMOKE_SH, name) for name in FUNCTIONS),
            *answers_script,
            "deployed_services() {",
            '  [[ "${DEPLOYED}" != yes ]] || echo deployment.apps/claims-api',
            "}",
            "kctl() {",
            f'  echo "$*" >>"{calls}"',
            '  case "$*" in',
            '    *" exec "*)',
            '      if [[ "${INSIDE_FAIL}" == yes ]]; then',
            '        echo "Traceback: connection refused" >&2; return 1',
            "      fi",
            '      printf "%s\\n" "${INSIDE}"; return 0 ;;',
            "  esac",
            '  if [[ "${PODS_FAIL}" == yes ]]; then',
            '    echo "Error from server" >&2; return 1',
            "  fi",
            '  printf "%s" "${PODS}"',
            "}",
            "curl() {",
            '  local out="" url="" previous="" argument headers=""',
            '  for argument in "$@"; do',
            '    [[ "${previous}" == -o ]] && out="${argument}"',
            '    [[ "${previous}" == -D ]] && headers="${argument}"',
            '    previous="${argument}"; url="${argument}"',
            "  done",
            f'  echo "$*" >>"{fetched}"',
            '  if [[ -n "${CURL_FAIL}" && "${url}" == *"${CURL_FAIL}"* ]]; then',
            '    echo "curl: (7) Failed to connect" >&2; return 7',
            "  fi",
            '  cp "${BODY[${url}]}" "${out}"',
            '  printf "HTTP/1.1 %s\\r\\n" "${STATUS[${url}]}" >"${headers}"',
            '  if [[ -n "${LOCATION[${url}]}" ]]; then',
            '    printf "location: %s\\r\\n" "${LOCATION[${url}]}" >>"${headers}"',
            "  fi",
            '  printf "%s" "${STATUS[${url}]}"',
            "}",
            "check_issuer",
        ]
    )
    done = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env={
            "PATH": os.environ["PATH"],
            "MERIDIAN_IDENTITY": identity,
            "PODS": pods,
            "PODS_FAIL": "yes" if pods_fail else "no",
            "CURL_FAIL": curl_fail,
            "DEPLOYED": "yes" if deployed else "no",
            "INSIDE": inside,
            "INSIDE_FAIL": "yes" if inside_fail else "no",
        },
        check=False,
    )
    assert done.returncode == 0, done.stderr
    return (
        done.stdout.splitlines(),
        calls.read_text().splitlines(),
        fetched.read_text().splitlines(),
    )


def kinds(lines: list[str]) -> list[str]:
    return [line.split()[0] for line in lines]


# ── off: one SKIP, nothing asked ─────────────────────────────────────────────


@pytest.mark.parametrize("identity", ["", "unset"])
def test_while_the_add_on_is_off_the_check_prints_one_skip_and_asks_nothing(
    tmp_path: Path, identity: str
) -> None:
    lines, asked, fetched = run_check(
        tmp_path, identity="" if identity == "" else "other"
    )

    assert kinds(lines) == ["SKIP"]
    assert "issuer" in lines[0] and "MERIDIAN_IDENTITY=keycloak" in lines[0]
    assert asked == [] and fetched == []


# ── on: six lines ────────────────────────────────────────────────────────────


def test_when_all_is_well_six_pass_lines_follow_in_order(tmp_path: Path) -> None:
    lines, asked, fetched = run_check(tmp_path)

    assert kinds(lines) == PASSING
    assert all(line.split()[1] == "issuer:" for line in lines)
    assert "Ready" in lines[0] and DIGEST.split(":")[1][:12] in lines[0]
    assert REALM in lines[1]
    assert "signing key" in lines[2]
    assert all(path in lines[3] for path in CLOSED) and "empty body" in lines[3]
    assert all(path in lines[4] for path in CLIMBING)
    assert INSIDE in lines[5] and REALM in lines[5]
    assert len(asked) == 2  # the pod, and the one exec in the Claims API's pod
    assert "get pod" in asked[0] and "-n identity" in asked[0]
    assert len(fetched) == 2 + len(CLOSED) + len(CLIMBING)


def test_every_path_goes_as_written_and_by_get_alone(tmp_path: Path) -> None:
    _, _, fetched = run_check(tmp_path)

    assert all("--path-as-is" in call.split() for call in fetched)
    assert [call.split()[-1] for call in fetched[-len(CLIMBING) :]] == [
        f"{FRONT}{path}" for path in CLIMBING
    ]


def test_the_check_is_read_only_and_reads_no_secret_and_signs_nobody_in(
    tmp_path: Path,
) -> None:
    _, asked, fetched = run_check(tmp_path)
    body = function_body(SMOKE_SH, "check_issuer")
    text = "".join(function_body(SMOKE_SH, name) for name in FUNCTIONS)

    assert asked[0].startswith("-n identity get pod")
    assert asked[1].startswith("-n meridian exec deploy/claims-api -- python -c ")
    assert not any("secret" in call.lower() for call in asked)
    for call in fetched:
        assert not re.search(r"(^| )(-X|-d|--data|-H|-u|--user|-b|--cookie)\b", call)
        assert not call.split()[-1].endswith("/token")
    assert "secret" not in text.lower()
    assert "password" not in text.lower() and "client_secret" not in text
    assert "grant_type" not in text and "openid-connect/token" not in text
    assert body.count("check_issuer_") == 6


def test_the_closed_and_climbing_paths_are_the_ones_the_script_names() -> None:
    script = SMOKE_SH

    for path in (*CLOSED, *CLIMBING):
        assert path in script, path
    assert "--path-as-is" in script


# ── from inside the cluster ──────────────────────────────────────────────────


def test_the_inside_line_asks_the_service_by_its_name_from_the_claims_apis_pod(
    tmp_path: Path,
) -> None:
    _, asked, _ = run_check(tmp_path)

    assert asked[1].endswith(f"{INSIDE}/.well-known/openid-configuration")


def test_the_inside_line_fails_for_the_service_url_as_issuer_and_for_a_failed_fetch(
    tmp_path: Path,
) -> None:
    wrong, _, _ = run_check(
        tmp_path / "a", inside="http://keycloak.identity.svc:8080/x"
    )
    failed, _, _ = run_check(tmp_path / "b", inside_fail=True)

    for lines in (wrong, failed):
        assert kinds(lines) == [*PASSING[:5], "FAIL"]
        assert lines[5].split()[1] == "issuer:"
    assert "pins" in wrong[5]
    assert "could not fetch" in failed[5] and "connection refused" in failed[5]


def test_the_inside_line_is_skipped_while_the_claims_api_is_not_deployed(
    tmp_path: Path,
) -> None:
    lines, asked, _ = run_check(tmp_path, deployed=False)

    assert kinds(lines) == [*PASSING[:5], "SKIP"]
    assert "make deploy" in lines[5]
    assert len(asked) == 1  # no exec was tried


def test_what_the_pod_prints_cannot_put_an_escape_or_a_line_in_the_inside_line(
    tmp_path: Path,
) -> None:
    lines, _, _ = run_check(tmp_path, inside="x\x1b[31m\nPASS  fake")

    assert len(lines) == 6 and kinds(lines)[5] == "FAIL"
    assert all("\x1b" not in line for line in lines)


def test_the_pod_line_fails_for_each_thing_that_is_not_as_it_should_be(
    tmp_path: Path,
) -> None:
    cases = {
        "no pod": json.dumps({"items": []}),
        "two pods": json.dumps({"items": [pod(), pod()]}),
        "not ready": json.dumps({"items": [pod(ready=False)]}),
        "another image": json.dumps(
            {"items": [pod(image="quay.io/keycloak/keycloak:26.8.0")]}
        ),
        "another digest in the status": json.dumps(
            {"items": [pod(image_id="quay.io/keycloak/keycloak@sha256:" + "0" * 64)]}
        ),
        "an init container with another image": json.dumps(
            {"items": [pod(init_image="quay.io/keycloak/keycloak:26.8.0")]}
        ),
        "an environment on the container": json.dumps({"items": [pod(env=True)]}),
        "not json": "not json at all",
        "empty": "",
    }
    for name, pods in cases.items():
        lines, _, _ = run_check(tmp_path / name.replace(" ", "-"), pods=pods)

        assert kinds(lines) == ["FAIL", *PASSING[1:]], (name, lines)
        assert lines[0].split()[1] == "issuer:", name


def test_the_pod_line_names_the_init_container_and_the_environment(
    tmp_path: Path,
) -> None:
    init, _, _ = run_check(
        tmp_path / "i", pods=json.dumps({"items": [pod(init_image="x/y:1")]})
    )
    env, _, _ = run_check(tmp_path / "e", pods=json.dumps({"items": [pod(env=True)]}))

    assert "init container" in init[0] and "another image" in init[0]
    assert "environment" in env[0]


def test_the_pod_line_fails_when_the_pod_cannot_be_read_and_says_how_to_make_it(
    tmp_path: Path,
) -> None:
    lines, _, _ = run_check(tmp_path, pods_fail=True)

    assert kinds(lines)[0] == "FAIL"
    assert "MERIDIAN_IDENTITY=keycloak make up" in lines[0]


def test_the_discovery_line_wants_200_and_the_front_url_as_the_issuer(
    tmp_path: Path,
) -> None:
    url = f"{REALM}/.well-known/openid-configuration"
    wrong_issuer = json.dumps({"issuer": "http://keycloak.identity.svc:8080/realms/x"})
    cases = {
        "the service url as issuer": ("200", wrong_issuer),
        "no issuer": ("200", "{}"),
        "not json": ("200", "<html>"),
        "a redirect": ("302", ""),
        "not found": ("404", ""),
        "server error": ("503", ""),
    }
    for name, answer in cases.items():
        lines, _, _ = run_check(
            tmp_path / name.replace(" ", "-"), answers={url: answer}
        )

        assert kinds(lines) == ["PASS", "FAIL", *PASSING[2:]], (name, lines)


def test_the_keys_line_wants_a_signing_key_and_an_encryption_key_does_not_count(
    tmp_path: Path,
) -> None:
    url = f"{REALM}/protocol/openid-connect/certs"
    cases = {
        "none": json.dumps({"keys": []}),
        "only an encryption key": json.dumps(
            {"keys": [{"kid": "a", "use": "enc", "alg": "RSA-OAEP"}]}
        ),
        "a signing key of another algorithm": json.dumps(
            {"keys": [{"kid": "a", "use": "sig", "alg": "HS512"}]}
        ),
        "not json": "oops",
    }
    for name, body in cases.items():
        lines, _, _ = run_check(
            tmp_path / name.replace(" ", "-"), answers={url: ("200", body)}
        )

        assert kinds(lines) == [*PASSING[:2], "FAIL", *PASSING[3:]], (name, lines)
    lines, _, _ = run_check(tmp_path / "a-status", answers={url: ("500", GOOD_KEYS)})
    assert kinds(lines) == [*PASSING[:2], "FAIL", *PASSING[3:]]


@pytest.mark.parametrize(
    ("paths", "index"), [(CLOSED, 3), (CLIMBING, 4)], ids=["closed", "climbing"]
)
def test_the_edge_lines_fail_on_any_answer_but_the_edges_own_404_and_name_the_path(
    tmp_path: Path, paths: list[str], index: int
) -> None:
    for path in paths:
        for status in ("200", "302", "400", "401", "403", "503"):
            lines, _, _ = run_check(
                tmp_path / f"{index}-{abs(hash(path))}-{status}",
                answers={f"{FRONT}{path}": (status, "")},
            )

            expected = ["FAIL" if i == index else "PASS" for i in range(6)]
            assert kinds(lines) == expected, (path, status)
            assert path in lines[index] and status in lines[index]
            marker = "got" if index == 3 else "redirect to one:"
            others = [p for p in paths if p != path]
            assert not any(
                other in lines[index].split(marker, 1)[1] for other in others
            ), (path, status)


# ── the climbing line: the edge's 404, or its own redirect to one ────────────

ESCAPED = [p for p in CLIMBING if "%2f" in p]  # the two forms KR1 saw redirected
NOT_ESCAPED = [p for p in CLIMBING if p not in ESCAPED]


def redirected(location: str = "/realms/master/", forms: list[str] = ESCAPED) -> dict:
    """KR1: the edge answers an escaped slash with a 307, no body, Location."""
    return {f"{FRONT}{path}": ("307", "", location) for path in forms}


def climbing_line(tmp_path: Path, **kwargs: object) -> str:
    lines, _, _ = run_check(tmp_path, **kwargs)  # type: ignore[arg-type]
    assert len(lines) == 6 and kinds(lines)[:4] == PASSING[:4]
    assert kinds(lines)[5] == "PASS"
    return lines[4]


def test_what_kr1_saw_passes_and_the_line_says_which_forms_were_which(
    tmp_path: Path,
) -> None:
    line = climbing_line(tmp_path, answers=redirected())

    assert line.startswith("PASS")
    gone, sent = line.split("; a 307 ")
    assert all(p in gone for p in NOT_ESCAPED) and not any(p in gone for p in ESCAPED)
    assert all(f"{p} -> /realms/master/" in sent for p in ESCAPED)
    assert "edge's own empty 404" in gone and "itself the edge's own empty 404" in sent


def test_the_target_is_fetched_as_written_once_for_each_redirected_form(
    tmp_path: Path,
) -> None:
    _, _, fetched = run_check(tmp_path, answers=redirected())

    targets = [call for call in fetched if call.endswith(f"{FRONT}/realms/master/")]
    # Once for the closed line's own path, and once for each redirected form.
    assert len(targets) == 1 + len(ESCAPED)
    assert all("--path-as-is" in call.split() for call in targets)


@pytest.mark.parametrize(
    "location",
    [
        "/realms/master/",
        f"{FRONT}/realms/master/",
        "/realms/master/?x=1",
        "/nowhere",
    ],
)
def test_a_307_to_a_path_on_this_host_that_is_the_edges_own_404_passes(
    tmp_path: Path, location: str
) -> None:
    answers = redirected(location)
    target = location.removeprefix(FRONT)
    answers[f"{FRONT}{target}"] = ("404", "")

    assert climbing_line(tmp_path, answers=answers).startswith("PASS")


@pytest.mark.parametrize(
    "location",
    [
        "/resources/x.css",
        "/resources",
        "/realms/meridian-staff/protocol/openid-connect/auth",
        "/realms/meridian-staff/.well-known/openid-configuration",
        "/realms/meridian-staff/login-actions/authenticate?x=1",
        f"{FRONT}/resources/",
    ],
)
def test_a_307_to_an_allowed_prefix_fails_and_names_the_form(
    tmp_path: Path, location: str
) -> None:
    line = climbing_line(tmp_path, answers=redirected(location))

    assert line.startswith("FAIL")
    for form in ESCAPED:
        assert f"{form} -> 307 to " in line
    assert "a prefix the route forwards" in line
    assert not any(f"{form} -> 404" in line for form in NOT_ESCAPED)


@pytest.mark.parametrize("status", ["200", "400", "302", "503"])
def test_a_307_whose_target_answers_anything_but_the_edges_404_fails(
    tmp_path: Path, status: str
) -> None:
    answers = redirected("/elsewhere/")
    answers[f"{FRONT}/elsewhere/"] = (status, "")
    line = climbing_line(tmp_path, answers=answers)

    assert line.startswith("FAIL")
    assert f"-> 307 to /elsewhere/, which answers {status}" in line


def test_a_307_whose_target_answers_404_with_a_body_fails(tmp_path: Path) -> None:
    answers = redirected("/elsewhere/")
    answers[f"{FRONT}/elsewhere/"] = ("404", "<html>not found</html>")
    line = climbing_line(tmp_path, answers=answers)

    assert line.startswith("FAIL")
    assert "which answers 404 with a body" in line and "Keycloak" in line


@pytest.mark.parametrize(
    "location",
    ["http://example.org/realms/master/", "//example.org/realms/master/", ""],
    ids=["another host", "protocol-relative", "no location"],
)
def test_a_307_to_another_host_or_with_no_location_fails(
    tmp_path: Path, location: str
) -> None:
    line = climbing_line(tmp_path, answers=redirected(location))

    assert line.startswith("FAIL")
    assert "not a path on this host" in line


def test_a_307_with_a_body_and_other_redirects_are_not_the_edges_and_fail(
    tmp_path: Path,
) -> None:
    body = redirected()
    key = f"{FRONT}{ESCAPED[0]}"
    body[key] = ("307", "<html>moved</html>", "/realms/master/")

    assert climbing_line(tmp_path / "a", answers=body).startswith("FAIL")
    for status in ("301", "302", "308"):
        line = climbing_line(
            tmp_path / status,
            answers={key: (status, "", "/realms/master/")},
        )
        assert line.startswith("FAIL") and f"{ESCAPED[0]} -> {status}" in line


@pytest.mark.parametrize("status", ["200", "400"])
def test_a_200_or_a_400_for_a_form_fails_and_names_it(
    tmp_path: Path, status: str
) -> None:
    form = NOT_ESCAPED[1]
    line = climbing_line(tmp_path, answers={f"{FRONT}{form}": (status, "")})

    assert line.startswith("FAIL") and f"{form} -> {status}" in line


def test_a_redirect_does_not_excuse_the_closed_line(tmp_path: Path) -> None:
    lines, _, _ = run_check(
        tmp_path, answers={f"{FRONT}/admin/": ("307", "", "/realms/master/")}
    )

    assert kinds(lines) == [*PASSING[:3], "FAIL", *PASSING[4:]]


@pytest.mark.parametrize(
    ("paths", "index"), [(CLOSED, 3), (CLIMBING, 4)], ids=["closed", "climbing"]
)
def test_a_404_with_a_body_is_keycloaks_and_not_the_edges(
    tmp_path: Path, paths: list[str], index: int
) -> None:
    for position, path in enumerate(paths):
        lines, _, _ = run_check(
            tmp_path / f"{index}-{position}",
            answers={f"{FRONT}{path}": ("404", "<html>Page not found</html>")},
        )

        assert lines[index].startswith("FAIL"), (path, lines)
        assert path in lines[index] and "with a body" in lines[index]
        assert "Keycloak" in lines[index]


def test_a_request_that_gets_no_answer_is_a_fail_and_not_a_404(tmp_path: Path) -> None:
    for needle, index in (
        ("/.well-known/", 1),
        ("/certs", 2),
        ("/admin/", 3),
        ("/%2e%2e/", 4),
    ):
        lines, _, _ = run_check(
            tmp_path / needle.strip("/.%").replace("/", "-"), curl_fail=needle
        )

        assert kinds(lines)[index] == "FAIL", (needle, lines)
        assert "did not answer" in lines[index]


def test_an_answer_cannot_put_terminal_escapes_or_extra_lines_in_a_line(
    tmp_path: Path,
) -> None:
    url = f"{REALM}/.well-known/openid-configuration"
    hostile = json.dumps({"issuer": "http://evil\x1b[31m\nPASS  fake"})
    lines, _, _ = run_check(tmp_path, answers={url: ("200", hostile)})

    assert len(lines) == 6
    assert all("\x1b" not in line for line in lines)
    assert kinds(lines)[1] == "FAIL"


# ── where the check sits ─────────────────────────────────────────────────────


def test_the_script_sources_the_part_and_calls_the_check_after_the_twelfth() -> None:
    entry = (KIND_DIR / "smoke.sh").read_text(encoding="utf-8")
    lines = entry.splitlines()

    assert '. "${KIND_DIR}/smoke.d/13-issuer.sh"' in lines
    assert "# shellcheck source=smoke.d/13-issuer.sh" in lines
    assert lines.index("check_telemetry_stores") + 1 == lines.index("check_issuer")
    assert "13 issuer" in entry
    part = KIND_DIR / "smoke.d" / "13-issuer.sh"
    assert not part.stat().st_mode & 0o111  # a part is not executable


def test_the_part_names_its_paragraph_with_the_number_thirteen() -> None:
    first = (KIND_DIR / "smoke.d" / "13-issuer.sh").read_text().splitlines()[:3]

    assert first[0] == "# shellcheck shell=bash"
    assert re.match(r"^#  13\. issuer: ", first[1])
