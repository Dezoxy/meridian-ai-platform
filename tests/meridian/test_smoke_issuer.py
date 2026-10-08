"""Smoke's check 13: the sign-in issuer, only when the add-on is on (S021, Y2b).

``check_issuer`` in ``infra/kind/smoke.d/13-issuer.sh`` prints one SKIP line while
``MERIDIAN_IDENTITY`` is off, and four lines when it is ``keycloak``: the pod is
Ready and runs the pinned image, the discovery document through the edge names the
front URL as its issuer, the key document holds a signing key, and ``/admin/`` and
``/realms/master/`` through the edge are 404. No line reads a Secret or signs
anyone in.

The function runs in bash against stand-ins for ``kctl`` and ``curl``. What no test
here proves: that Envoy forwards the two prefixes, that the cluster's ``imageID`` is
the index digest, and that the pod is ready on the cluster at all.
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
    "check_issuer_closed",
    "check_issuer",
)


def pod(
    *,
    ready: bool = True,
    image: str = IMAGE,
    image_id: str | None = None,
) -> dict:
    return {
        "spec": {"containers": [{"name": "keycloak", "image": image}]},
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
    answers: dict[str, tuple[str, str]] | None = None,
    curl_fail: str = "",
) -> tuple[list[str], list[str], list[str]]:
    """The PASS, FAIL and SKIP lines, the ``kctl`` calls and the ``curl`` calls."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    calls, fetched = tmp_path / "kctl-calls", tmp_path / "curl-calls"
    calls.touch()
    fetched.touch()
    table = {
        f"{REALM}/.well-known/openid-configuration": ("200", GOOD_DISCOVERY),
        f"{REALM}/protocol/openid-connect/certs": ("200", GOOD_KEYS),
        f"{FRONT}/admin/": ("404", ""),
        f"{FRONT}/realms/master/": ("404", ""),
        **(answers or {}),
    }
    answers_script = ["declare -A STATUS BODY"]
    for n, (url, (status, body)) in enumerate(table.items()):
        body_file = tmp_path / f"body-{n}"
        body_file.write_text(body)
        answers_script.append(f"STATUS[{shlex.quote(url)}]={shlex.quote(status)}")
        answers_script.append(f"BODY[{shlex.quote(url)}]={shlex.quote(str(body_file))}")
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
            "kctl() {",
            f'  echo "$*" >>"{calls}"',
            '  if [[ "${PODS_FAIL}" == yes ]]; then',
            '    echo "Error from server" >&2; return 1',
            "  fi",
            '  printf "%s" "${PODS}"',
            "}",
            "curl() {",
            '  local out="" url="" previous="" argument',
            '  for argument in "$@"; do',
            '    [[ "${previous}" == -o ]] && out="${argument}"',
            '    previous="${argument}"; url="${argument}"',
            "  done",
            f'  echo "$*" >>"{fetched}"',
            '  if [[ -n "${CURL_FAIL}" && "${url}" == *"${CURL_FAIL}"* ]]; then',
            '    echo "curl: (7) Failed to connect" >&2; return 7',
            "  fi",
            '  cp "${BODY[${url}]}" "${out}"',
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


# ── on: four lines ───────────────────────────────────────────────────────────


def test_when_all_is_well_four_pass_lines_follow_in_order(tmp_path: Path) -> None:
    lines, asked, fetched = run_check(tmp_path)

    assert kinds(lines) == ["PASS"] * 4
    assert all(line.split()[1] == "issuer:" for line in lines)
    assert "Ready" in lines[0] and DIGEST.split(":")[1][:12] in lines[0]
    assert REALM in lines[1]
    assert "signing key" in lines[2]
    assert "/admin/" in lines[3] and "/realms/master/" in lines[3] and "404" in lines[3]
    assert len(asked) == 1 and "get pod" in asked[0] and "-n identity" in asked[0]
    assert len(fetched) == 4


def test_the_check_is_read_only_and_reads_no_secret_and_signs_nobody_in(
    tmp_path: Path,
) -> None:
    _, asked, fetched = run_check(tmp_path)
    body = function_body(SMOKE_SH, "check_issuer")
    text = "".join(function_body(SMOKE_SH, name) for name in FUNCTIONS)

    assert all(call.startswith("-n identity get pod") for call in asked)
    assert not any("secret" in call.lower() for call in asked)
    for call in fetched:
        assert not re.search(r"(^| )(-X|-d|--data|-H|-u|--user|-b|--cookie)\b", call)
        assert not call.split()[-1].endswith("/token")
    assert "secret" not in text.lower()
    assert "password" not in text.lower() and "client_secret" not in text
    assert "grant_type" not in text and "openid-connect/token" not in text
    assert body.count("check_issuer_") == 4


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
        "not json": "not json at all",
        "empty": "",
    }
    for name, pods in cases.items():
        lines, _, _ = run_check(tmp_path / name.replace(" ", "-"), pods=pods)

        assert kinds(lines) == ["FAIL", "PASS", "PASS", "PASS"], (name, lines)
        assert lines[0].split()[1] == "issuer:", name


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

        assert kinds(lines) == ["PASS", "FAIL", "PASS", "PASS"], (name, lines)


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

        assert kinds(lines) == ["PASS", "PASS", "FAIL", "PASS"], (name, lines)
    lines, _, _ = run_check(tmp_path / "a-status", answers={url: ("500", GOOD_KEYS)})
    assert kinds(lines) == ["PASS", "PASS", "FAIL", "PASS"]


def test_the_closed_line_fails_on_any_answer_but_404_and_names_the_path(
    tmp_path: Path,
) -> None:
    for path in ("/admin/", "/realms/master/"):
        for status in ("200", "302", "401", "403", "503"):
            lines, _, _ = run_check(
                tmp_path / f"{path.strip('/').replace('/', '-')}-{status}",
                answers={f"{FRONT}{path}": (status, "")},
            )

            assert kinds(lines) == ["PASS", "PASS", "PASS", "FAIL"], (path, status)
            assert path in lines[3] and status in lines[3]
            other = "/realms/master/" if path == "/admin/" else "/admin/"
            assert other not in lines[3].split("expected")[0] or status in lines[3]


def test_a_request_that_gets_no_answer_is_a_fail_and_not_a_404(tmp_path: Path) -> None:
    for needle, index in (("/.well-known/", 1), ("/certs", 2), ("/admin/", 3)):
        lines, _, _ = run_check(
            tmp_path / needle.strip("/.").replace("/", "-"), curl_fail=needle
        )

        assert kinds(lines)[index] == "FAIL", (needle, lines)
        assert "did not answer" in lines[index]


def test_an_answer_cannot_put_terminal_escapes_or_extra_lines_in_a_line(
    tmp_path: Path,
) -> None:
    url = f"{REALM}/.well-known/openid-configuration"
    hostile = json.dumps({"issuer": "http://evil\x1b[31m\nPASS  fake"})
    lines, _, _ = run_check(tmp_path, answers={url: ("200", hostile)})

    assert len(lines) == 4
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
