"""The two TLS lines of smoke's telemetry check, and telemetrygen's Job (S063, N4b).

``check_telemetry`` in ``infra/kind/smoke.sh`` opens with two lines of its own,
before it sends anything (check 4 prints nine lines, the last three S064's and G1's):

- ``check_telemetry_ca``: the ConfigMap ``telemetry-ca`` in `meridian` holds the
  certificate the authority has now (the fingerprint of its ``ca.crt`` equals the
  one of ``tls.crt`` in the Secret ``telemetry-ca`` in `observability`). Only
  that one field of the Secret is read, never the object and never the key.
- ``check_telemetry_clear_text``: a Job pod in `meridian`, which the policies
  admit to the collector's port, speaks plain HTTP to it, and the answer must not
  be a 2xx. The pod runs the platform database's own image (on the node after
  ``make up``: no image is pulled and the Meridian services need not be
  deployed) and speaks with bash's ``/dev/tcp``.

The functions run in bash against a stub ``kctl``; the fingerprints are read by
the real ``openssl`` from real certificates (``tlssupport``), and the probe's own
script runs for real against a TLS server, a plain one and a closed port. The
telemetrygen Job's arguments are read from the script.
"""

import json
import os
import re
import shlex
import socket
import subprocess
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
import yaml
from test_kind_manifests import (
    SMOKE_SH,
    function_definition,
    one_line_function,
    requires_jq,
)
from tlsserver import echo_the_caller, serve_tls
from tlssupport import loopback_sans, make_ca

pytestmark = requires_jq

COLLECTOR = "otel-collector.observability.svc.cluster.local:4318"
CONFIGMAP = "telemetry-ca"
COLLECTOR_DEPLOYED = "deployment.apps/otel-collector"
AUTHORITY_PRESENT = "secret/telemetry-ca"
NEXT_RUN = "run make up"
FINGERPRINT_PREFIX = 12
CONSTANTS = (
    r"^readonly (?:COLLECTOR_ENDPOINT|TELEMETRYGEN_NAMESPACE|JOB_TIMEOUT"
    r"|TELEMETRY_ANSWER_LENGTH|TELEMETRY_CA_CONFIGMAP|TELEMETRY_CA_SECRET"
    r"|TELEMETRY_CA_NAMESPACE|TELEMETRY_FINGERPRINT_SHOWN|TELEMETRYGEN_CA_DIRECTORY"
    r"|TELEMETRYGEN_CA_FILE"
    r"|CLEAR_TEXT_JOB_PREFIX|CLEAR_TEXT_TIMEOUT)=\S+$"
)
PROBE = re.search(r"^readonly CLEAR_TEXT_PROBE='(.*?)'$", SMOKE_SH, re.M | re.S)
assert PROBE, "smoke.sh has no CLEAR_TEXT_PROBE"
PROBE_SCRIPT = PROBE.group(1)

CA_STUB = r"""
kctl() {
  printf '%s\n' "${*//$'\n'/ }" >>"${ASKED}"
  case "$*" in
    "-n observability get secret telemetry-ca -o name --ignore-not-found")
      [[ "${AUTHORITY_LOOKUP}" != FAIL ]] || { echo "Error" >&2; return 1; }
      printf "%s" "${AUTHORITY_LOOKUP}" ;;
    "-n observability get secret telemetry-ca -o jsonpath={.data.tls\.crt}")
      [[ "${AUTHORITY_READ}" != FAIL ]] || { echo "Error" >&2; return 1; }
      printf "%s" "${AUTHORITY_B64}" ;;
    "-n meridian get configmap telemetry-ca "*"ca\.crt"*"--ignore-not-found")
      [[ "${CONFIGMAP_READ}" != FAIL ]] || { echo "Error" >&2; return 1; }
      printf "%s" "${CONFIGMAP_PEM}" ;;
    *) echo "unexpected kctl call: $*" >&2; return 1 ;;
  esac
}
"""

CLEAR_STUB = r"""
kctl() {
  printf '%s\n' "${*//$'\n'/ }" >>"${ASKED}"
  case "$*" in
    *"get deployment otel-collector"*)
      [[ "${DEPLOYED}" != FAIL ]] || { echo "Error" >&2; return 1; }
      printf "%s" "${DEPLOYED}" ;;
    *" create "*)
      cat >"${STATE}/created.json"
      [[ "${CREATE_STATUS}" == 0 ]] || { echo "Error: create" >&2; return 1; } ;;
    *" wait "*)
      [[ "${WAIT_STATUS}" == 0 ]] ||
        { echo "${WAIT_SAYS-error: timed out}" >&2; return 1; } ;;
    *" logs "*)
      printf "%s" "${ANSWER}"
      return "${LOGS_STATUS}" ;;
    *) echo "unexpected kctl call: $*" >&2; return 1 ;;
  esac
}
"""


def pem_of(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def fingerprint(path: Path) -> str:
    """The SHA-256 fingerprint of a certificate file, as ``openssl`` prints it."""
    done = subprocess.run(
        ["openssl", "x509", "-noout", "-fingerprint", "-sha256", "-in", str(path)],
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.strip().partition("=")[2].replace(":", "")


def run_function(
    tmp_path: Path,
    name: str,
    stub: str,
    environment: dict[str, str],
    extra: list[str] | None = None,
    stderr: list[str] | None = None,
) -> tuple[list[str], str]:
    """Run ``name`` of smoke.sh against ``stub``; its output lines and what
    ``kctl`` was asked, one call per line. The function's standard error is
    appended to ``stderr`` (as one text) when a list is given."""
    state = tmp_path / "state"
    state.mkdir()
    asked = tmp_path / "kctl-calls"
    asked.touch()
    script = "\n".join(
        [
            "set -euo pipefail",
            "failures=0; skips=0",
            'pass() { echo "PASS  $*"; }',
            'fail() { echo "FAIL  $*"; }',
            'skip() { echo "SKIP  $*"; }',
            "log() { :; }",
            "epoch=1700000000",
            "POSTGRES_IMAGE=postgres:test",
            f"CLEAR_TEXT_PROBE={shlex.quote(PROBE_SCRIPT)}",
            *re.findall(CONSTANTS, SMOKE_SH, re.M),
            one_line_function(SMOKE_SH, "clean_lines"),
            function_definition(SMOKE_SH, "telemetry_answer"),
            stub,
            *(function_definition(SMOKE_SH, each) for each in extra or []),
            function_definition(SMOKE_SH, name),
            name,
        ]
    )
    done = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        check=False,
        env={
            **os.environ,
            "ASKED": str(asked),
            "STATE": str(state),
            **environment,
        },
    )
    assert done.returncode == 0, done.stderr
    if stderr is not None:
        stderr.append(done.stderr)
    return done.stdout.splitlines(), asked.read_text(encoding="utf-8")


# ── the ConfigMap holds the authority's certificate ─────────────────────────


@pytest.fixture(scope="module")
def authorities(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    directory = tmp_path_factory.mktemp("smoke-telemetry-ca")
    return (
        make_ca(directory, "telemetry-test-ca").ca_file,
        make_ca(directory, "stale-ca").ca_file,
    )


def run_ca_check(
    tmp_path: Path,
    authority: Path,
    configmap: str | None = None,
    *,
    authority_lookup: str = AUTHORITY_PRESENT,
    authority_read: str = "ok",
    configmap_read: str = "ok",
    authority_text: str | None = None,
) -> tuple[list[str], str]:
    pem = authority_text if authority_text is not None else pem_of(authority)
    encoded = subprocess.run(
        ["base64", "-w0"], input=pem, capture_output=True, text=True, check=True
    ).stdout
    return run_function(
        tmp_path,
        "check_telemetry_ca",
        CA_STUB,
        {
            "AUTHORITY_LOOKUP": authority_lookup,
            "AUTHORITY_READ": authority_read,
            "AUTHORITY_B64": encoded,
            "CONFIGMAP_READ": configmap_read,
            "CONFIGMAP_PEM": configmap if configmap is not None else pem,
        },
        extra=["pem_fingerprint"],
    )


def test_a_configmap_with_the_authoritys_certificate_passes(
    tmp_path: Path, authorities: tuple[Path, Path]
) -> None:
    current, _ = authorities

    lines, _ = run_ca_check(tmp_path, current)

    assert len(lines) == 1
    assert lines[0].startswith("PASS  telemetry: ")
    assert "equal" in lines[0]
    assert CONFIGMAP in lines[0]


def test_the_passing_line_prints_the_start_of_the_fingerprint_and_no_more(
    tmp_path: Path, authorities: tuple[Path, Path]
) -> None:
    current, _ = authorities
    full = fingerprint(current)

    (line,) = run_ca_check(tmp_path, current)[0]

    assert full[:FINGERPRINT_PREFIX] in line
    assert full[: FINGERPRINT_PREFIX + 1] not in line
    assert "BEGIN" not in line


def test_a_configmap_with_another_certificate_fails_and_says_to_run_make_up(
    tmp_path: Path, authorities: tuple[Path, Path]
) -> None:
    current, stale = authorities

    (line,) = run_ca_check(tmp_path, current, configmap=pem_of(stale))[0]

    assert line.startswith("FAIL  telemetry: ")
    assert NEXT_RUN in line
    # The services read the mounted file at each new connection, which the
    # kubelet refreshes: `make up` is the whole remedy, with no restart (S063,
    # the security review's M1).
    assert "then restart" not in line
    assert "rollout" not in line
    assert "kubelet refreshes the mounted file" in line
    assert "without a restart" in line
    assert fingerprint(current)[:FINGERPRINT_PREFIX] in line
    assert fingerprint(stale)[:FINGERPRINT_PREFIX] in line
    assert fingerprint(current)[: FINGERPRINT_PREFIX + 1] not in line


def test_a_missing_configmap_fails_and_says_to_run_make_up(
    tmp_path: Path, authorities: tuple[Path, Path]
) -> None:
    current, _ = authorities

    (line,) = run_ca_check(tmp_path, current, configmap="")[0]

    assert line.startswith("FAIL  telemetry: ")
    assert NEXT_RUN in line
    assert "missing" in line


def test_a_configmap_that_holds_no_certificate_fails_without_its_content(
    tmp_path: Path, authorities: tuple[Path, Path]
) -> None:
    current, _ = authorities

    (line,) = run_ca_check(tmp_path, current, configmap="canary-not-a-certificate")[0]

    assert line.startswith("FAIL  telemetry: ")
    assert "canary" not in line
    assert NEXT_RUN in line


def test_an_authority_that_is_not_a_certificate_fails_without_its_content(
    tmp_path: Path, authorities: tuple[Path, Path]
) -> None:
    current, _ = authorities

    (line,) = run_ca_check(tmp_path, current, authority_text="canary-no-cert")[0]

    assert line.startswith("FAIL  telemetry: ")
    assert "canary" not in line


def test_an_authority_that_is_not_there_skips_the_line(
    tmp_path: Path, authorities: tuple[Path, Path]
) -> None:
    current, _ = authorities

    (line,) = run_ca_check(tmp_path, current, authority_lookup="")[0]

    assert line.startswith("SKIP  telemetry: ")
    assert NEXT_RUN in line


@pytest.mark.parametrize(
    "broken",
    [
        {"authority_lookup": "FAIL"},
        {"authority_read": "FAIL"},
        {"configmap_read": "FAIL"},
    ],
)
def test_a_read_that_fails_fails_the_line(
    tmp_path: Path, authorities: tuple[Path, Path], broken: dict[str, str]
) -> None:
    current, _ = authorities

    (line,) = run_ca_check(tmp_path, current, **broken)[0]

    assert line.startswith("FAIL  telemetry: ")


def test_only_the_certificate_field_of_the_secret_is_read(
    tmp_path: Path, authorities: tuple[Path, Path]
) -> None:
    current, _ = authorities

    _, asked = run_ca_check(tmp_path, current)
    calls = asked.splitlines()

    assert len(calls) == 3
    whole_object = re.compile(r"-o (yaml|json)( |$)")
    assert not [c for c in calls if "tls.key" in c or whole_object.search(c)]
    assert [c for c in calls if "secret" in c and "jsonpath" in c] == [
        "-n observability get secret telemetry-ca -o jsonpath={.data.tls\\.crt}"
    ]


# ── plain HTTP to the TLS port ──────────────────────────────────────────────


def run_clear_text(
    tmp_path: Path,
    answer: str,
    *,
    deployed: str = COLLECTOR_DEPLOYED,
    create_status: int = 0,
    wait_status: int = 0,
    logs_status: int = 0,
    wait_says: str | None = None,
    stderr: list[str] | None = None,
) -> tuple[list[str], str, dict]:
    """``wait_says`` is what the stub ``wait`` prints on standard error when it
    fails (its default is a short line of its own); ``stderr`` collects the
    function's standard error."""
    environment = {
        "ANSWER": answer,
        "DEPLOYED": deployed,
        "CREATE_STATUS": str(create_status),
        "WAIT_STATUS": str(wait_status),
        "LOGS_STATUS": str(logs_status),
    }
    if wait_says is not None:
        environment["WAIT_SAYS"] = wait_says
    lines, asked = run_function(
        tmp_path,
        "check_telemetry_clear_text",
        CLEAR_STUB,
        environment,
        extra=["clear_text_job_spec", "show_wait_error"],
        stderr=stderr,
    )
    created = tmp_path / "state" / "created.json"
    job = json.loads(created.read_text()) if created.exists() else {}
    return lines, asked, job


def healthy_ca_lines(tmp_path: Path) -> list[str]:
    """What ``check_telemetry_ca`` prints when the ConfigMap is current: the
    count of ``test_smoke_line_count.py`` takes it from here."""
    folder = tmp_path / "authority"
    folder.mkdir()
    return run_ca_check(tmp_path, make_ca(folder, "healthy-ca").ca_file)[0]


def healthy_clear_text_lines(tmp_path: Path) -> list[str]:
    """What ``check_telemetry_clear_text`` prints when the listener refuses."""
    return run_clear_text(tmp_path, "answered HTTP/1.0 400 Bad Request")[0]


@pytest.mark.parametrize(
    ("answer", "shown"),
    [
        ("answered HTTP/1.0 400 Bad Request", "HTTP/1.0 400 Bad Request"),
        ("closed", "closed"),
    ],
)
def test_a_clear_text_push_that_gets_a_400_or_no_answer_passes_and_says_which(
    tmp_path: Path, answer: str, shown: str
) -> None:
    (line,) = run_clear_text(tmp_path, answer)[0]

    assert line.startswith("PASS  telemetry: ")
    assert shown in line
    assert COLLECTOR in line


@pytest.mark.parametrize(
    "status",
    [
        "100 Continue",
        "301 Moved Permanently",
        "404 Not Found",
        "503 Service Unavailable",
    ],
)
def test_a_clear_text_push_answered_with_any_other_status_fails_as_not_showing_tls(
    tmp_path: Path, status: str
) -> None:
    (line,) = run_clear_text(tmp_path, f"answered HTTP/1.1 {status}")[0]

    # A plain HTTP receiver answers 404 or 503 as well as a TLS listener answers
    # 400: only the 400 (the evidence of 2026-10-06) is the listener's own.
    assert line.startswith("FAIL  telemetry: ")
    assert f"HTTP/1.1 {status}" in line
    assert COLLECTOR in line
    assert "does not show TLS" in line
    assert "a status other than 400 means something answered HTTP in clear text" in line
    assert "proves nothing" not in line


@pytest.mark.parametrize("status", ["200", "202", "204"])
def test_a_clear_text_push_that_is_accepted_fails(tmp_path: Path, status: str) -> None:
    (line,) = run_clear_text(tmp_path, f"answered HTTP/1.1 {status} OK")[0]

    assert line.startswith("FAIL  telemetry: ")
    assert status in line
    assert "serve TLS" in line


@pytest.mark.parametrize(
    "answer",
    [
        "timeout",
        "no answer",
        "error: bash: connect: Connection refused",
        "",
        "something else entirely",
    ],
)
def test_a_probe_that_could_not_connect_or_is_not_understood_fails(
    tmp_path: Path, answer: str
) -> None:
    (line,) = run_clear_text(tmp_path, answer)[0]

    assert line.startswith("FAIL  telemetry: ")
    assert "proves nothing" in line


def test_a_probe_that_did_not_complete_fails_and_names_its_logs(
    tmp_path: Path,
) -> None:
    (line,) = run_clear_text(tmp_path, "", wait_status=1)[0]

    assert line.startswith("FAIL  telemetry: ")
    assert "kubectl -n meridian logs job/smoke-cleartext-1700000000" in line


BOUND_SENTENCE = (
    "error: kubectl wait was ended after its --timeout: the API server did not answer"
)
ORDINARY_TIMEOUT = "error: timed out waiting for the condition on jobs/x"


def test_a_wait_that_an_outer_bound_ends_shows_its_sentence_on_standard_error(
    tmp_path: Path,
) -> None:
    said: list[str] = []

    (line,) = run_clear_text(
        tmp_path, "", wait_status=1, wait_says=BOUND_SENTENCE, stderr=said
    )[0]

    # The FAIL line is as it was, and the sentence is no longer thrown away.
    assert line.startswith("FAIL  telemetry: the clear-text probe ")
    assert said[0].splitlines() == [BOUND_SENTENCE]


def test_a_wait_that_merely_timed_out_prints_nothing_more_than_the_fail_line(
    tmp_path: Path,
) -> None:
    said: list[str] = []

    (line,) = run_clear_text(
        tmp_path, "", wait_status=1, wait_says=ORDINARY_TIMEOUT, stderr=said
    )[0]

    assert line.startswith("FAIL  telemetry: ")
    assert said == [""]


def test_a_sentence_beside_the_ordinary_line_is_shown_without_it_or_controls(
    tmp_path: Path,
) -> None:
    said: list[str] = []
    both = f"{ORDINARY_TIMEOUT}\n{BOUND_SENTENCE}\x1b[31m"

    run_clear_text(tmp_path, "", wait_status=1, wait_says=both, stderr=said)

    # The escape character is dropped (what is left of it is printable text).
    assert said[0].splitlines() == [BOUND_SENTENCE + "[31m"]


def test_both_waits_of_the_telemetry_check_show_what_they_say() -> None:
    sites = re.findall(
        r'(if ! wait_said="\$\(kctl [^\n]*wait --for=condition=complete.*?'
        r"then\n\s+show_wait_error)",
        SMOKE_SH,
        re.S,
    )

    assert len(sites) == 2
    assert "wait_said" in function_definition(SMOKE_SH, "check_telemetry_clear_text")
    assert "local signal wait_said" in function_definition(SMOKE_SH, "check_telemetry")


def test_a_job_that_cannot_be_made_fails(tmp_path: Path) -> None:
    (line,) = run_clear_text(tmp_path, "", create_status=1)[0]

    assert line.startswith("FAIL  telemetry: ")
    assert "Error: create" in line


def test_a_log_that_cannot_be_read_fails(tmp_path: Path) -> None:
    (line,) = run_clear_text(tmp_path, "answered HTTP/1.1 400 Bad", logs_status=1)[0]

    assert line.startswith("FAIL  telemetry: ")


def test_a_collector_that_is_not_deployed_skips_the_line_and_makes_nothing(
    tmp_path: Path,
) -> None:
    lines, asked, job = run_clear_text(tmp_path, "", deployed="")

    assert [line.split()[0] for line in lines] == ["SKIP"]
    assert "make up" in lines[0]
    assert " create " not in asked
    assert job == {}


def test_a_lookup_of_the_collector_that_fails_fails_the_line(tmp_path: Path) -> None:
    (line,) = run_clear_text(tmp_path, "", deployed="FAIL")[0]

    assert line.startswith("FAIL  telemetry: ")


def test_a_hostile_answer_stays_on_one_line_without_escapes(tmp_path: Path) -> None:
    hostile = "answered HTTP/1.0 400 \x1b[2J\nPASS  forged: all is well\r\nFAIL  other"

    lines, _, _ = run_clear_text(tmp_path, hostile)

    assert len(lines) == 1
    assert lines[0].startswith("PASS  telemetry: ")
    assert "\x1b" not in lines[0]
    assert "\r" not in lines[0]


def test_the_probe_job_runs_where_the_policies_admit_it_with_what_is_on_the_node(
    tmp_path: Path,
) -> None:
    _, _, job = run_clear_text(tmp_path, "closed")
    (container,) = job["spec"]["template"]["spec"]["containers"]
    pod = job["spec"]["template"]["spec"]

    assert job["kind"] == "Job"
    assert job["metadata"]["namespace"] == "meridian"
    assert job["metadata"]["name"] == "smoke-cleartext-1700000000"
    # The label smoke-networkpolicy.yaml selects: DNS and the collector's 4318.
    assert job["spec"]["template"]["metadata"]["labels"] == {
        "app.kubernetes.io/name": "meridian-smoke"
    }
    assert job["spec"]["backoffLimit"] == 0
    assert job["spec"]["activeDeadlineSeconds"] > 0
    assert job["spec"]["ttlSecondsAfterFinished"] > 0
    assert pod["restartPolicy"] == "Never"
    assert container["image"] == "postgres:test"
    assert container["imagePullPolicy"] == "IfNotPresent"
    assert container["command"][:2] == ["bash", "-c"]
    assert container["command"][3:] == [
        "probe",
        "otel-collector.observability.svc.cluster.local",
        "4318",
        "4",
    ]


def test_the_probe_job_is_as_restricted_as_telemetrygens(tmp_path: Path) -> None:
    _, _, job = run_clear_text(tmp_path, "closed")
    pod = job["spec"]["template"]["spec"]
    (container,) = pod["containers"]

    assert pod["securityContext"]["runAsNonRoot"] is True
    assert pod["securityContext"]["seccompProfile"] == {"type": "RuntimeDefault"}
    assert container["securityContext"] == {
        "allowPrivilegeEscalation": False,
        "readOnlyRootFilesystem": True,
        "capabilities": {"drop": ["ALL"]},
    }
    assert pod["automountServiceAccountToken"] is False
    assert "volumes" not in pod


# ── the probe's own script, run for real ────────────────────────────────────


def probe(*arguments: str, path: str | None = None) -> str:
    done = subprocess.run(
        ["bash", "-c", PROBE_SCRIPT, "probe", *arguments],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "PATH": path or os.environ["PATH"]},
        timeout=60,
    )
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


@contextmanager
def plain_server(status: int) -> Iterator[int]:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            self.send_response(status)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args: object) -> None:
            return

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_the_probe_reads_the_status_line_of_a_server_that_accepts_clear_text() -> None:
    with plain_server(200) as port:
        answer = probe("127.0.0.1", str(port), "2")

    assert re.fullmatch(r"answered HTTP/1\.[01] 200 OK", answer), answer


def test_the_probe_reads_a_refusal_of_clear_text_as_a_status() -> None:
    with plain_server(400) as port:
        answer = probe("127.0.0.1", str(port), "2")

    assert re.fullmatch(r"answered HTTP/1\.[01] 400 Bad Request", answer), answer


def test_the_probe_sees_a_tls_server_drop_a_clear_text_request(
    tmp_path: Path,
) -> None:
    authority = make_ca(tmp_path, "probe-ca")
    server = authority.issue("server", "server", loopback_sans())
    with serve_tls(echo_the_caller, authority, server) as url:
        answer = probe("127.0.0.1", url.rpartition(":")[2], "2")

    assert answer in ("closed", "no answer"), answer
    assert not answer.startswith("answered HTTP/1.1 2")


def test_the_probe_tells_a_refused_connection_from_a_timeout() -> None:
    with socket.socket() as closed:
        closed.bind(("127.0.0.1", 0))
        port = closed.getsockname()[1]
    answer = probe("127.0.0.1", str(port), "2")

    assert answer.startswith("error: ")
    assert answer != "timeout"


def test_the_probe_says_timeout_when_the_connect_does_not_finish(
    tmp_path: Path,
) -> None:
    stub = tmp_path / "bin"
    stub.mkdir()
    (stub / "timeout").write_text("#!/bin/sh\nexit 124\n", encoding="utf-8")
    (stub / "timeout").chmod(0o755)

    answer = probe("127.0.0.1", "1", "2", path=f"{stub}:{os.environ['PATH']}")

    assert answer == "timeout"


def test_the_probe_says_no_answer_when_a_server_listens_and_says_nothing() -> None:
    with socket.socket() as silent:
        silent.bind(("127.0.0.1", 0))
        silent.listen(1)
        answer = probe("127.0.0.1", str(silent.getsockname()[1]), "1")

    assert answer == "no answer"


# ── telemetrygen's Job ──────────────────────────────────────────────────────


def telemetrygen_job(tmp_path: Path, signal: str = "traces") -> dict:
    """The manifest ``start_job`` feeds ``kctl create`` for ``signal``, read from
    a run of the function with a stub ``kctl`` that keeps its input."""
    created = tmp_path / "created.yaml"
    script = "\n".join(
        [
            "set -euo pipefail",
            "TELEMETRYGEN_IMAGE=telemetrygen:test",
            "epoch=1700000000 service=meridian-smoke-1700000000",
            *re.findall(CONSTANTS, SMOKE_SH, re.M),
            f'kctl() {{ cat >"{created}"; }}',
            function_definition(SMOKE_SH, "start_job"),
            f"start_job {signal} --{signal}",
        ]
    )
    done = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, check=False
    )
    assert done.returncode == 0, done.stderr
    return yaml.safe_load(created.read_text(encoding="utf-8"))


def test_telemetrygen_pushes_over_https_with_the_authoritys_certificate(
    tmp_path: Path,
) -> None:
    (container,) = telemetrygen_job(tmp_path)["spec"]["template"]["spec"]["containers"]

    args = container["args"]

    assert "--otlp-insecure" not in args
    assert "--otlp-http" in args
    assert args[args.index("--otlp-endpoint") + 1] == COLLECTOR
    assert args[args.index("--ca-cert") + 1] == "/etc/telemetry-ca/ca.crt"
    assert "--otlp-insecure-skip-verify" not in args


def test_telemetrygens_pod_reads_the_authority_from_the_configmap_in_meridian(
    tmp_path: Path,
) -> None:
    job = telemetrygen_job(tmp_path)
    pod = job["spec"]["template"]["spec"]
    (container,) = pod["containers"]

    assert job["metadata"]["namespace"] == "meridian"
    assert pod["volumes"] == [
        {
            "name": "telemetry-ca",
            "configMap": {
                "name": CONFIGMAP,
                "items": [{"key": "ca.crt", "path": "ca.crt"}],
            },
        }
    ]
    assert container["volumeMounts"] == [
        {"name": "telemetry-ca", "mountPath": "/etc/telemetry-ca", "readOnly": True}
    ]
    assert container["securityContext"]["readOnlyRootFilesystem"] is True


@pytest.mark.parametrize("signal", ["traces", "logs", "metrics"])
def test_each_telemetrygen_job_fails_at_a_deadline_a_little_over_smokes_own_wait(
    tmp_path: Path, signal: str
) -> None:
    (wait,) = re.findall(r"^readonly JOB_TIMEOUT=(\d+)s$", SMOKE_SH, re.M)

    spec = telemetrygen_job(tmp_path, signal)["spec"]

    # A pod that never starts (the ConfigMap `telemetry-ca` is missing, so it
    # stays in ContainerCreating) leaves a Job that never finishes, and
    # ttlSecondsAfterFinished only counts from a finished Job: the deadline makes
    # that Job Failed, and the TTL then removes it.
    assert spec["ttlSecondsAfterFinished"] > 0
    assert int(wait) < spec["activeDeadlineSeconds"] <= int(wait) + 60
