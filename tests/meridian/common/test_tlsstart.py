"""The start module of the five services that serve TLS (S069, R11): the server
that uvicorn's own command line would have started, with the certificate read
once (the hand-over itself is in ``test_tlsstart_handover``)."""

import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from tlsserver import echo_the_caller
from tlsstartsupport import (
    HTTP_PROTOCOL,
    Listened,
    Pki,
    chart_arguments,
    make_pki,
    serving,
    stand_in_for_listening,
)
from tlssupport import client_context, spiffe, uri_san
from uvicorn.config import SSL_PROTOCOL_VERSION, create_ssl_context
from uvicorn.main import main as uvicorn_command

from meridian.platform.common import certlife, tlsstart
from meridian.platform.common.tlsstart import STARTUP_FAILURE, main

GARBAGE = "not-a-certificate-canary-9013"
# Not named in the chart; the factory is never called when the start fails.
UNCALLED_FACTORY = "meridian.platform.policy_mcp.app:create_app_from_env"


@pytest.fixture(autouse=True)
def nothing_handed_over(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(certlife, "_handed_over", None)


@pytest.fixture
def pki(tmp_path: Path) -> Pki:
    return make_pki(tmp_path)


def public(config: uvicorn.Config) -> dict[str, Any]:
    return {k: v for k, v in vars(config).items() if not k.startswith("_")}


def configs_built_from(
    monkeypatch: pytest.MonkeyPatch, arguments: list[str]
) -> tuple[uvicorn.Config, uvicorn.Config]:
    """The ``Config`` of uvicorn's command line and the one of the start module,
    each made from the same words; neither server is started."""
    made: dict[str, uvicorn.Config] = {}

    def recorder(name: str) -> Callable[..., None]:
        def record(app: Any, *, app_dir: str | None = None, **kwargs: Any) -> None:
            # The command line's ``--app-dir`` is "" (the working directory goes
            # on ``sys.path``, which ``python -m`` does itself): not a Config
            # attribute, so it is the one thing that differs and is asserted.
            assert app_dir == ("" if name == "command" else None)
            made[name] = uvicorn.Config(app, **kwargs)

        return record

    monkeypatch.setattr(sys.modules["uvicorn.main"], "run", recorder("command"))
    uvicorn_command.main(args=arguments, standalone_mode=False)
    monkeypatch.setattr(uvicorn, "run", recorder("module"))
    main(arguments)
    return made["command"], made["module"]


def test_the_config_the_module_builds_is_the_one_uvicorns_command_builds(
    monkeypatch: pytest.MonkeyPatch, pki: Pki
) -> None:
    command, module = configs_built_from(monkeypatch, chart_arguments(pki))

    given, wanted = public(module), public(command)
    assert callable(given.pop("ssl_context_factory"))
    assert wanted.pop("ssl_context_factory") is None
    assert given == wanted
    # The ones the chart's words decide, so the comparison is not of two
    # defaults.
    assert (given["factory"], given["port"], given["ws"]) == (True, 0, "none")
    assert given["http"] == HTTP_PROTOCOL
    assert given["ssl_cert_reqs"] == 1
    assert given["ssl_certfile"] == str(pki.server.cert)


def test_the_context_that_reaches_create_server_has_uvicorns_own_settings(
    monkeypatch: pytest.MonkeyPatch, pki: Pki
) -> None:
    stand_in_for_listening(monkeypatch)
    own = create_ssl_context(
        str(pki.server.cert),
        str(pki.server.key),
        None,
        SSL_PROTOCOL_VERSION,
        1,
        str(pki.ca.ca_file),
        None,
    )

    with pytest.raises(Listened) as listened:
        main(chart_arguments(pki))

    served = listened.value.context
    assert served.protocol == own.protocol
    assert served.minimum_version == own.minimum_version
    assert served.maximum_version == own.maximum_version
    assert served.options == own.options
    # Optional, exactly: the kubelet's probe presents no certificate.
    assert served.verify_mode == own.verify_mode == 1
    assert served.verify_flags == own.verify_flags
    assert served.check_hostname == own.check_hostname
    assert served.get_ciphers() == own.get_ciphers()
    assert served.security_level == own.security_level
    assert served.cert_store_stats() == own.cert_store_stats()


def test_the_caller_certificate_uris_are_read_through_the_modules_context(
    monkeypatch: pytest.MonkeyPatch, pki: Pki
) -> None:
    """``PeerCertProtocol`` needs a context that asks for a certificate and
    holds the CA: the module's does, so the caller's URI reaches the app."""
    configs: list[uvicorn.Config] = []

    def record(app: Any, *, factory: bool, **kwargs: Any) -> None:
        # The app itself stands in for the factory's product.
        configs.append(uvicorn.Config(echo_the_caller, **kwargs))

    monkeypatch.setattr(uvicorn, "run", record)
    main(chart_arguments(pki))
    caller = pki.ca.issue("caller", "caller", [uri_san(spiffe("agent-runtime"))])
    (config,) = configs

    with (
        serving(config) as url,
        httpx.Client(verify=client_context(pki.ca, caller)) as client,
    ):
        answer = client.get(url).json()

    assert answer == {"uris": [spiffe("agent-runtime")]}


# ── the command line ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "flag", ["--ssl-certfile", "--ssl-keyfile"], ids=["no-certificate", "no-key"]
)
def test_a_start_without_a_certificate_or_key_is_refused_not_served_plain(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    pki: Pki,
    flag: str,
) -> None:
    stand_in_for_listening(monkeypatch)
    words = chart_arguments(pki)
    at = words.index(flag)
    del words[at : at + 2]

    with pytest.raises(SystemExit) as stopped:
        main(words)

    assert stopped.value.code == STARTUP_FAILURE
    assert len(capsys.readouterr().err.splitlines()) == 1


def test_a_flag_the_module_does_not_know_is_refused(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], pki: Pki
) -> None:
    stand_in_for_listening(monkeypatch)

    with pytest.raises(SystemExit) as stopped:
        main([*chart_arguments(pki), "--no-access-log"])

    assert stopped.value.code == STARTUP_FAILURE
    assert len(capsys.readouterr().err.splitlines()) == 1


# ── fail closed ──────────────────────────────────────────────────────────────


def refused(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    arguments: list[str],
) -> str:
    """The one line a start that does not listen leaves on standard error."""
    stand_in_for_listening(monkeypatch)

    with pytest.raises(SystemExit) as stopped:
        main(arguments)

    assert stopped.value.code == 3
    lines = capsys.readouterr().err.splitlines()
    assert len(lines) == 1, lines
    return lines[0]


def test_a_missing_certificate_file_ends_the_start_with_one_line_and_no_path(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    pki: Pki,
) -> None:
    pki.server.cert.unlink()

    line = refused(monkeypatch, capsys, chart_arguments(pki))

    assert "--ssl-certfile" in line
    assert "FileNotFoundError" in line
    assert str(tmp_path) not in line
    assert certlife._handed_over is None


def test_a_file_that_is_no_certificate_ends_the_start_without_its_content(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    pki: Pki,
) -> None:
    pki.server.cert.write_text(GARBAGE)

    line = refused(monkeypatch, capsys, chart_arguments(pki))

    assert "--ssl-certfile" in line
    assert GARBAGE not in line
    assert str(tmp_path) not in line
    assert certlife._handed_over is None


def test_a_key_that_is_not_the_certificates_ends_the_start_without_a_path(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    pki: Pki,
) -> None:
    other = pki.ca.issue("other", "other", [])
    pki.server.key.write_bytes(other.key.read_bytes())

    line = refused(monkeypatch, capsys, chart_arguments(pki))

    assert "--ssl-keyfile" in line
    assert "SSLError" in line
    assert str(tmp_path) not in line
    assert certlife._handed_over is None


def test_a_restart_share_that_is_not_a_fraction_ends_the_start_without_its_value(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], pki: Pki
) -> None:
    monkeypatch.setenv(certlife.RESTART_SHARE_ENV, "7.25")

    line = refused(monkeypatch, capsys, chart_arguments(pki))

    assert certlife.RESTART_SHARE_ENV in line
    assert "SettingsError" in line
    assert "7.25" not in line


def test_bytes_that_never_settle_end_the_start_after_the_fifth_load(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    pki: Pki,
) -> None:
    other = pki.ca.issue("other", "other", [])
    versions = [
        (pki.server.cert.read_bytes(), pki.server.key.read_bytes()),
        (other.cert.read_bytes(), other.key.read_bytes()),
    ]
    loads: list[None] = []

    def swapping_load(**given: Any) -> Any:
        """The real default builder, and a renewal (certificate and key
        together) that lands during each load."""
        context = create_ssl_context(**given)
        loads.append(None)
        certificate, key = versions[len(loads) % 2]
        pki.server.cert.write_bytes(certificate)
        pki.server.key.write_bytes(key)
        return context

    monkeypatch.setattr("uvicorn.config.create_ssl_context", swapping_load)

    line = refused(monkeypatch, capsys, chart_arguments(pki))

    assert len(loads) == tlsstart.ATTEMPTS == 5
    assert "--ssl-certfile" in line
    assert str(tmp_path) not in line
    assert certlife._handed_over is None


def test_a_start_that_fails_in_the_real_process_exits_3_with_one_line_and_no_traceback(
    tmp_path: Path, pki: Pki
) -> None:
    missing = tmp_path / "canary-directory-4410" / "tls.crt"
    words = chart_arguments(pki, app=UNCALLED_FACTORY)
    words[words.index("--ssl-certfile") + 1] = str(missing)

    done = subprocess.run(
        [sys.executable, "-m", "meridian.platform.common.tlsstart", *words],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert done.returncode == 3
    assert done.stdout == ""
    assert len(done.stderr.splitlines()) == 1, done.stderr
    assert "canary-directory-4410" not in done.stderr
    assert "Traceback" not in done.stderr
    assert "--ssl-certfile" in done.stderr
