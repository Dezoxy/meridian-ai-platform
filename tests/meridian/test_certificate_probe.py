"""smoke.sh's identity probe, run for real (S056).

The probe's ``foreign-ca`` mode presents the runtime's own URI with a
certificate of a CA the server does not trust. Here the probe runs as a pod
would run it, against a local TLS server from ``tlsserver.py``, so the tests see
what it prints and what it leaves behind. The checks of ``smoke.sh`` that run
with a stub ``kctl`` are in ``test_certificate_smoke.py``.
"""

import os
import re
import socket
import ssl
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from certscriptsupport import CALLER, SECONDS
from test_helm_identity import SMOKE_SH
from tlsserver import Pki, echo_the_caller, serve_tls
from tlssupport import (
    LOOPBACK,
    PREFIX,
    dns_san,
    loopback_sans,
    make_ca,
    spiffe,
    uri_san,
)

# ── smoke.sh: the probe's foreign-ca mode, for real ──────────────────────────

(PROBE,) = re.findall(
    r"^readonly IDENTITY_PROBE='(.*?)'$", SMOKE_SH, re.MULTILINE | re.DOTALL
)
# The pod's one writable path, which the probe names itself: where the leftovers
# of a run would be. Each test runs the probe with that path swapped for a
# directory of its own (`run_probe`), so that another worker's probe, which
# makes and removes its directory at the same moment, is not mistaken for a
# leftover. `POD_TEMP` is that directory while a test runs.
POD_PATH = 'dir="/tmp"'
POD_TEMP = Path("/tmp")  # noqa: S108
TEMP_PATTERN = "foreign-ca-*"


@pytest.fixture(autouse=True)
def pod_temp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    scratch = tmp_path / "pod-tmp"
    scratch.mkdir()
    monkeypatch.setitem(globals(), "POD_TEMP", scratch)
    return scratch


@pytest.fixture(scope="module")
def pki(tmp_path_factory: pytest.TempPathFactory) -> Pki:
    directory = tmp_path_factory.mktemp("foreign-ca")
    ca = make_ca(directory, "meridian-test-ca")
    runtime = ca.issue(
        CALLER, CALLER, [uri_san(spiffe(CALLER)), dns_san(f"{CALLER}.meridian.svc")]
    )
    return Pki(
        ca=ca,
        other_ca=make_ca(directory, "other-ca"),
        server=ca.issue("server", "server", loopback_sans()),
        services={CALLER: runtime},
    )


def run_probe(
    mode: str, url: str, pki: Pki, *, trusting: Path | None = None
) -> subprocess.CompletedProcess[str]:
    """The probe, as a pod would run it: ``python -c PROBE mode host port
    tenant`` with the pod's own TLS files and prefix in the environment, and
    its scratch directory under ``POD_TEMP`` instead of ``/tmp``."""
    host, port = url.removeprefix("https://").split(":")
    runtime = pki.services[CALLER]
    probe = PROBE.replace(POD_PATH, f'dir="{POD_TEMP}"')
    assert probe != PROBE
    return subprocess.run(
        [sys.executable, "-c", probe, mode, host, port, "evaluation"],
        capture_output=True,
        text=True,
        env={
            "PATH": os.environ["PATH"],
            "MERIDIAN_TLS_CA_FILE": str(trusting or pki.ca.ca_file),
            "MERIDIAN_TLS_CERT_FILE": str(runtime.cert),
            "MERIDIAN_TLS_KEY_FILE": str(runtime.key),
            "MERIDIAN_IDENTITY_PREFIX": PREFIX,
        },
        check=False,
        timeout=SECONDS,
    )


def leftovers() -> set[Path]:
    return set(POD_TEMP.glob(TEMP_PATTERN))


def test_the_probe_compiles_and_has_no_single_quote_that_would_end_its_string() -> None:
    compile(PROBE, "identity_probe", "exec")

    assert "'" not in PROBE
    assert "foreign-ca" in PROBE


def read_request(tls: ssl.SSLSocket) -> None:
    """Read one HTTP request whole (its headers, then ``Content-Length`` bytes
    of body) from ``tls`` and close it, so that nothing unread turns the close
    into a reset: the server has taken the request and says nothing."""
    data = b""
    while b"\r\n\r\n" not in data:
        data += received(tls)
    head, _, body = data.partition(b"\r\n\r\n")
    length = int(re.search(rb"(?i)content-length: (\d+)", head).group(1))
    while len(body) < length:
        body += received(tls)
    tls.close()


def received(tls: ssl.SSLSocket) -> bytes:
    chunk = tls.recv(65536)
    if not chunk:
        raise ConnectionError("the client went away before its request was whole")
    return chunk


def serve_raw_tls(
    pki: Pki, behaviour: str, max_version: ssl.TLSVersion | None = None
) -> tuple[socket.socket, str]:
    """A server on plain ``ssl`` (not uvicorn), listening on a loopback port
    the system picks: its URL and the listening socket, which the caller closes.
    ``behaviour`` is ``alert`` (a client certificate from ``pki.ca`` is
    required, so another CA's gets the TLS alert for an unknown CA),
    ``mismatch`` (no cipher in common: a handshake_failure alert, whatever the
    client sends), ``close`` (accept and close at once, no TLS at all) or
    ``accept-and-close`` (any client certificate is accepted, the request is
    read whole, and the connection is closed without an answer)."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(pki.server.cert), str(pki.server.key))
    if max_version is not None:
        context.maximum_version = max_version
    if behaviour == "alert":
        context.load_verify_locations(str(pki.ca.ca_file))
        context.verify_mode = ssl.CERT_REQUIRED
    if behaviour == "mismatch":
        context.maximum_version = TLS_1_2
        context.set_ciphers("ECDHE-RSA-AES128-GCM-SHA256")  # the key is EC
    listener = socket.socket()
    listener.bind((LOOPBACK, 0))
    listener.listen()

    def accept_loop() -> None:
        while True:
            try:
                connection, _ = listener.accept()
            except OSError:  # the listener was closed
                return
            try:
                if behaviour == "accept-and-close":
                    read_request(context.wrap_socket(connection, server_side=True))
                elif behaviour != "close":
                    context.wrap_socket(connection, server_side=True).close()
            except (ssl.SSLError, OSError):  # the handshake failed, as meant
                pass
            finally:
                connection.close()

    threading.Thread(target=accept_loop, daemon=True).start()
    return listener, f"https://{LOOPBACK}:{listener.getsockname()[1]}"


def probe_raw_server(
    pki: Pki, behaviour: str, max_version: ssl.TLSVersion | None = None
) -> subprocess.CompletedProcess[str]:
    listener, url = serve_raw_tls(pki, behaviour, max_version)
    try:
        return run_probe("foreign-ca", url, pki)
    finally:
        listener.close()


def test_the_foreign_ca_mode_prints_refused_when_the_server_sends_the_unknown_ca_alert(
    pki: Pki,
) -> None:
    # Python's ssl names the alert: SSLError, reason TLSV1_ALERT_UNKNOWN_CA. The
    # probe reads before it writes, so under TLS 1.3, where the alert follows
    # the handshake, it is always the first thing read (writing first lost it
    # to a reset in a fifth of the runs).
    before = leftovers()
    answers = [probe_raw_server(pki, "alert") for _ in range(5)]

    for done in answers:
        assert done.stdout.strip() == "refused", done.stderr
        assert done.returncode == 0, done.stderr
        assert done.stderr == ""
        # The throwaway key is in no output, and its directory is gone.
        assert "PRIVATE KEY" not in done.stdout + done.stderr
    assert leftovers() == before


def test_the_foreign_ca_mode_prints_refused_for_the_alert_under_tls_1_2_too(
    pki: Pki,
) -> None:
    # Under 1.2 the alert arrives inside the handshake, in connect().
    done = probe_raw_server(pki, "alert", max_version=TLS_1_2)

    assert done.stdout.strip() == "refused", done.stderr
    assert done.returncode == 0, done.stderr


def test_the_foreign_ca_mode_prints_reset_when_uvicorn_ends_the_connection_unannounced(
    pki: Pki,
) -> None:
    # What the services' own server does with an unknown CA, measured against
    # tlsserver.py (uvicorn with the services' flags, PeerCertProtocol): no
    # alert reaches the client. Under TLS 1.3 the connection is reset.
    before = leftovers()
    with serve_tls(echo_the_caller, pki.ca, pki.server) as url:
        done = run_probe("foreign-ca", url, pki)

    assert done.stdout.strip() == "reset", done.stderr
    assert done.returncode == 0, done.stderr
    assert done.stderr == ""
    assert "PRIVATE KEY" not in done.stdout + done.stderr
    assert leftovers() == before


def test_the_foreign_ca_mode_prints_reset_when_the_server_closes_without_tls(
    pki: Pki,
) -> None:
    # Python's ssl reports an EOF in violation of protocol, no alert.
    done = probe_raw_server(pki, "close")

    assert done.stdout.strip() == "reset", done.stderr
    assert done.returncode == 0, done.stderr


@pytest.mark.parametrize(
    "max_version", [None, ssl.TLSVersion.TLSv1_2], ids=["tls-1.3", "tls-1.2"]
)
def test_a_server_that_takes_a_foreign_certificates_request_and_closes_is_no_reset(
    pki: Pki, max_version: ssl.TLSVersion | None
) -> None:
    # A reset before any request is a refusal; a close after the request went
    # out is what a server that accepted the certificate does when it then
    # hangs up without an answer (RemoteDisconnected is a ConnectionResetError).
    before = leftovers()
    done = probe_raw_server(pki, "accept-and-close", max_version=max_version)

    assert done.stdout.strip() == "closed-after-request", done.stderr
    assert done.returncode == 0, done.stderr
    assert done.stderr == ""
    assert "reset" not in done.stdout
    assert "PRIVATE KEY" not in done.stdout + done.stderr
    assert leftovers() == before


def test_an_alert_that_is_not_the_unknown_ca_is_a_traceback_not_a_refusal(
    pki: Pki,
) -> None:
    # No cipher in common: the server sends handshake_failure. Something else
    # refused the connection, and the probe does not call it the refusal.
    done = probe_raw_server(pki, "mismatch")

    assert done.returncode != 0
    assert "ALERT_HANDSHAKE_FAILURE" in done.stderr
    assert "refused" not in done.stdout
    assert "reset" not in done.stdout


def test_the_foreign_ca_mode_prints_the_status_when_the_handshake_completes(
    pki: Pki,
) -> None:
    with serve_tls(echo_the_caller, pki.ca, pki.server, ssl.CERT_NONE) as url:
        done = run_probe("foreign-ca", url, pki)

    assert done.stdout.strip() == "200", done.stderr
    assert done.returncode == 0, done.stderr


def test_the_other_modes_still_print_the_status_over_the_same_server(
    pki: Pki,
) -> None:
    with serve_tls(echo_the_caller, pki.ca, pki.server) as url:
        health = run_probe("health", url, pki)
        foreign_tenant = run_probe("foreign-tenant", url, pki)

    assert health.stdout.strip() == "200", health.stderr
    assert foreign_tenant.stdout.strip() == "200", foreign_tenant.stderr


def test_a_server_certificate_that_does_not_verify_is_a_traceback_not_a_refusal(
    pki: Pki,
) -> None:
    before = leftovers()
    with serve_tls(echo_the_caller, pki.ca, pki.server) as url:
        done = run_probe("foreign-ca", url, pki, trusting=pki.other_ca.ca_file)

    assert done.returncode != 0
    assert "SSLCertVerificationError" in done.stderr
    assert "refused" not in done.stdout
    assert leftovers() == before


def test_a_connection_that_is_refused_or_unresolvable_is_a_traceback_too(
    pki: Pki,
) -> None:
    with socket.socket() as closed:
        closed.bind(("127.0.0.1", 0))
        port = closed.getsockname()[1]
    refused = run_probe("foreign-ca", f"https://127.0.0.1:{port}", pki)
    unresolved = run_probe("foreign-ca", "https://no-such-host.invalid:8000", pki)

    for done in (refused, unresolved):
        assert done.returncode != 0
        assert "Traceback" in done.stderr
        assert "refused" not in done.stdout
    assert "ConnectionRefusedError" in refused.stderr
    assert "gaierror" in unresolved.stderr


TLS_1_2 = ssl.TLSVersion.TLSv1_2


def negotiated_version(url: str, pki: Pki) -> str | None:
    context = ssl.create_default_context(cafile=str(pki.ca.ca_file))
    host, port = url.removeprefix("https://").split(":")
    with (
        socket.create_connection((host, int(port)), timeout=SECONDS) as raw,
        context.wrap_socket(raw, server_hostname=host) as tls,
    ):
        return tls.version()


def test_the_test_server_can_be_held_to_tls_1_2(pki: Pki) -> None:
    # The tests below mean nothing unless the server really negotiates 1.2.
    with serve_tls(echo_the_caller, pki.ca, pki.server, max_version=TLS_1_2) as url:
        capped = negotiated_version(url, pki)
    with serve_tls(echo_the_caller, pki.ca, pki.server) as url:
        default = negotiated_version(url, pki)

    assert capped == "TLSv1.2"
    assert default == "TLSv1.3"


def test_the_foreign_ca_mode_prints_reset_when_uvicorn_ends_a_tls_1_2_handshake(
    pki: Pki,
) -> None:
    # Python's ssl reports the EOF in violation of protocol, again no alert.
    before = leftovers()
    with serve_tls(echo_the_caller, pki.ca, pki.server, max_version=TLS_1_2) as url:
        done = run_probe("foreign-ca", url, pki)

    assert done.stdout.strip() == "reset", done.stderr
    assert done.returncode == 0, done.stderr
    assert done.stderr == ""
    assert leftovers() == before


def test_under_tls_1_2_a_server_certificate_that_does_not_verify_is_still_a_traceback(
    pki: Pki,
) -> None:
    with serve_tls(echo_the_caller, pki.ca, pki.server, max_version=TLS_1_2) as url:
        done = run_probe("foreign-ca", url, pki, trusting=pki.other_ca.ca_file)

    assert done.returncode != 0
    assert "SSLCertVerificationError" in done.stderr
    assert "refused" not in done.stdout


def test_a_handshake_alert_stays_a_traceback_in_every_mode_but_foreign_ca(
    pki: Pki,
) -> None:
    # The server trusts another CA, so under 1.2 it rejects the runtime's own
    # certificate in the handshake; `foreign-tenant` presents that certificate.
    with serve_tls(
        echo_the_caller, pki.other_ca, pki.server, max_version=TLS_1_2
    ) as url:
        done = run_probe("foreign-tenant", url, pki)

    assert done.returncode != 0
    assert "Traceback" in done.stderr
    assert "ssl.SSL" in done.stderr  # an alert (or the close after it), raised
    assert "refused" not in done.stdout


def test_the_probe_presents_the_runtimes_own_uri_with_a_key_of_its_own() -> None:
    # The right name, the wrong CA: the URI comes from the pod's prefix, the key
    # is made in the probe, in a directory under /tmp that goes with it.
    assert 'os.environ["MERIDIAN_IDENTITY_PREFIX"]' in PROBE
    assert '"agent-runtime"' in PROBE
    assert "TemporaryDirectory" in PROBE
    assert 'dir="/tmp"' in PROBE
    assert "generate_private_key" in PROBE
    assert "print(key" not in PROBE
    assert "check_hostname = False" not in PROBE
    assert "CERT_NONE" not in PROBE


def test_the_probe_opens_the_connection_inside_the_try() -> None:
    answer = PROBE.split("def answer():")[1].split("if mode ==")[0]

    assert answer.index("try:") < answer.index("connection.connect()")


def test_the_foreign_ca_mode_reads_before_it_writes_and_names_the_alert() -> None:
    answer = PROBE.split("def answer():")[1].split("\nif mode ==")[0]

    # Writing first lost the alert to a reset under TLS 1.3 in a fifth of the
    # runs; the alert is named by its reason, not by the class of the error.
    assert answer.index("recv(1)") < answer.index('connection.request("POST"')
    assert "TLSV1_ALERT_UNKNOWN_CA" in answer
    assert 'return "reset"' in answer


def test_the_scratch_listing_helper_sees_a_leftover(tmp_path: Path) -> None:
    # The leftover check above proves nothing unless it can fail.
    marker = POD_TEMP / "foreign-ca-test-marker"
    marker.mkdir(exist_ok=True)
    try:
        assert marker in leftovers()
    finally:
        marker.rmdir()
