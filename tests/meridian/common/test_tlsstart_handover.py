"""The certificate a service serves is the one its health check watches
(S069, R11).

uvicorn builds the TLS context, then the app factory runs and reads the
certificate file for ``/healthz``. A renewal that lands between the two used to
leave the health check on a newer certificate than the one served. Here the
real ``Config.load`` runs the start module's context factory and then an app
factory, in one process: no port is bound, and a handshake over memory reads
back which certificate the context holds.
"""

import ssl
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from servicesupport import REGISTRY_DIR
from starlette.testclient import TestClient
from starlette.types import ASGIApp
from tlsstartsupport import Pki, der_of, make_pki, served_certificate
from tlssupport import KeyPair, loopback_sans
from uvicorn.config import create_ssl_context

from meridian.platform.common import certlife, tlsstart
from meridian.platform.common.certlife import RESTART_SHARE_ENV
from meridian.platform.common.http import create_service_app
from meridian.platform.common.tls import CERT_FILE_ENV
from meridian.platform.policy_mcp.app import create_app as create_policy_app
from meridian.platform.toolserver.settings import ToolServerSettings

ENDED = {"status": "certificate-expiring"}
RENEWED_LIFETIME = timedelta(days=60)


@pytest.fixture(autouse=True)
def nothing_handed_over(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(certlife, "_handed_over", None)


@pytest.fixture
def pki(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Pki:
    """The service's files, holding a certificate that has ended (a context
    loads one, and the health check then needs no clock to say so)."""
    pki = make_pki(tmp_path)
    now = datetime.now(UTC).replace(microsecond=0)
    ended = pki.ca.issue(
        "ended",
        "ended",
        loopback_sans(),
        now - timedelta(hours=2),
        now - timedelta(hours=1),
    )
    put(ended, pki.server)
    monkeypatch.setenv(CERT_FILE_ENV, str(pki.server.cert))
    return pki


def put(source: KeyPair, target: KeyPair) -> None:
    """A renewal: the Secret's certificate and key, together."""
    target.cert.write_bytes(source.cert.read_bytes())
    target.key.write_bytes(source.key.read_bytes())


def renewed_pair(pki: Pki) -> KeyPair:
    now = datetime.now(UTC).replace(microsecond=0)
    return pki.ca.issue(
        "renewed",
        "renewed",
        loopback_sans(),
        now - timedelta(minutes=1),
        now + RENEWED_LIFETIME,
    )


def service_app() -> ASGIApp:
    return create_service_app(
        title="start-test",
        description="start-test",
        service_name="start-test",
        tracer_name="start-test",
        max_body_bytes=1024,
    ).app


def tool_app() -> ASGIApp:
    settings = ToolServerSettings(
        registry_dir=REGISTRY_DIR,
        database_url="postgresql://role:pw@db.invalid/m",
        allowed_hosts=("127.0.0.1:*",),
    )
    return create_policy_app(settings).app


BUILDERS = {"service-app": service_app, "tool-app": tool_app}


def load_config(
    pki: Pki,
    app_factory: Callable[[], ASGIApp],
    context_factory: Callable[..., ssl.SSLContext] | None,
) -> uvicorn.Config:
    """The ``Config`` the chart's flags make, loaded: the context, then the app
    factory, as ``uvicorn.run`` does."""
    config = uvicorn.Config(
        app_factory,
        factory=True,
        ssl_certfile=str(pki.server.cert),
        ssl_keyfile=str(pki.server.key),
        ssl_ca_certs=str(pki.ca.ca_file),
        ssl_cert_reqs=ssl.CERT_OPTIONAL,
        ssl_context_factory=context_factory,
    )
    config.load()
    return config


def health(config: uvicorn.Config) -> tuple[int, Any]:
    response = TestClient(config.loaded_app).get("/healthz")
    return response.status_code, response.json()


def renewing_before_the_app(
    pki: Pki, renewed: KeyPair, build: Callable[[], ASGIApp]
) -> Callable[[], ASGIApp]:
    """An app factory that runs a moment after the context was built, when the
    renewal has landed."""

    def factory() -> ASGIApp:
        put(renewed, pki.server)
        return build()

    return factory


@pytest.mark.parametrize("kind", BUILDERS)
def test_a_renewal_after_the_context_leaves_health_on_the_served_certificate(
    pki: Pki, kind: str
) -> None:
    served_file = der_of(pki.server.cert)
    renewed = renewed_pair(pki)

    config = load_config(
        pki,
        renewing_before_the_app(pki, renewed, BUILDERS[kind]),
        tlsstart.serve_one_certificate,
    )

    # The context holds the certificate that was served when it was built, and
    # the file now holds the renewed one.
    assert served_certificate(config.ssl) == served_file
    assert der_of(pki.server.cert) == der_of(renewed.cert)
    # So the health check watches the served certificate, which has ended.
    assert health(config) == (503, ENDED)


@pytest.mark.parametrize("kind", BUILDERS)
def test_without_the_hand_over_the_health_check_watches_the_newer_certificate(
    pki: Pki, kind: str
) -> None:
    """The fault, with uvicorn's own context and no hand-over: the same race
    leaves the health check on the renewed certificate, green, while the
    context serves one that has ended."""
    served_file = der_of(pki.server.cert)
    renewed = renewed_pair(pki)

    config = load_config(
        pki, renewing_before_the_app(pki, renewed, BUILDERS[kind]), None
    )

    assert served_certificate(config.ssl) == served_file
    assert health(config) == (200, {"status": "ok"})


def test_a_renewal_during_the_load_makes_the_module_load_again(
    pki: Pki, monkeypatch: pytest.MonkeyPatch
) -> None:
    renewed = renewed_pair(pki)
    loads: list[None] = []

    def renewing_during_the_first_load(**given: Any) -> ssl.SSLContext:
        context = create_ssl_context(**given)
        loads.append(None)
        if len(loads) == 1:
            put(renewed, pki.server)
        return context

    monkeypatch.setattr(
        "uvicorn.config.create_ssl_context", renewing_during_the_first_load
    )

    config = load_config(pki, service_app, tlsstart.serve_one_certificate)

    assert len(loads) == 2
    # The context and the handed certificate are both the renewed one.
    assert served_certificate(config.ssl) == der_of(renewed.cert)
    handed = certlife.load_certificate({})
    assert handed is not None
    assert handed.not_after == certlife._read_certificate(renewed.cert).not_after
    assert health(config) == (200, {"status": "ok"})


def test_the_share_comes_from_the_environment_the_process_runs_in(
    pki: Pki, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(RESTART_SHARE_ENV, "0.5")

    load_config(pki, service_app, tlsstart.serve_one_certificate)

    handed = certlife.load_certificate({})
    assert handed is not None
    assert handed.share == 0.5
    assert handed.source == pki.server.cert


def test_the_module_never_reads_the_key_file_itself(
    pki: Pki, monkeypatch: pytest.MonkeyPatch
) -> None:
    read: list[Path] = []
    real = Path.read_bytes

    def recording(self: Path) -> bytes:
        read.append(self)
        return real(self)

    monkeypatch.setattr(Path, "read_bytes", recording)

    load_config(pki, service_app, tlsstart.serve_one_certificate)

    assert pki.server.cert in read
    assert pki.server.key not in read
