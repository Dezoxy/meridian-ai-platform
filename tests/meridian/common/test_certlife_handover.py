"""The certificate the start module hands over (S069, R11).

The server reads its certificate file once, for the TLS context, and hands the
certificate it parsed from the same bytes to ``certlife``: ``/healthz`` then
watches the certificate that is served, not whatever the file holds when the
app factory runs.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from tlssupport import CertificateAuthority, KeyPair, loopback_sans, make_ca

from meridian.platform.common import certlife
from meridian.platform.common.certlife import (
    RESTART_SHARE_ENV,
    hand_over_certificate,
    load_certificate,
)
from meridian.platform.common.env import SettingsError
from meridian.platform.common.tls import CERT_FILE_ENV

GARBAGE = b"not-a-certificate-canary-5521"


@pytest.fixture
def ca(tmp_path: Path) -> CertificateAuthority:
    return make_ca(tmp_path, "handover-ca")


def issue(ca: CertificateAuthority, stem: str, ends_in: timedelta) -> KeyPair:
    now = datetime.now(UTC).replace(microsecond=0)
    return ca.issue(
        stem, stem, loopback_sans(), now - timedelta(hours=1), now + ends_in
    )


def test_a_certificate_handed_over_is_what_load_certificate_returns(
    ca: CertificateAuthority,
) -> None:
    served = issue(ca, "served", timedelta(hours=5))
    renewed = issue(ca, "renewed", timedelta(days=60))
    environ = {CERT_FILE_ENV: str(renewed.cert)}

    handed = hand_over_certificate(served.cert.read_bytes(), served.cert, environ)

    # The file the variable names holds a later certificate than the bytes
    # handed over; the process serves the handed one.
    assert load_certificate(environ) is handed
    assert handed.not_after == certlife._read_certificate(served.cert).not_after
    assert handed.not_after < certlife._read_certificate(renewed.cert).not_after


def test_a_certificate_handed_over_is_returned_when_the_variable_is_unset(
    ca: CertificateAuthority,
) -> None:
    served = issue(ca, "served", timedelta(hours=5))

    handed = hand_over_certificate(served.cert.read_bytes(), served.cert, {})

    assert load_certificate({}) is handed


def test_the_handed_certificate_watches_the_file_it_came_from(
    ca: CertificateAuthority,
) -> None:
    served = issue(ca, "served", timedelta(minutes=5))
    handed = hand_over_certificate(served.cert.read_bytes(), served.cert, {})
    now = datetime.now(UTC)

    assert handed.verdict(now) == "not-renewed"

    renewed = issue(ca, "renewed", timedelta(days=60))
    served.cert.write_bytes(renewed.cert.read_bytes())

    assert handed.verdict(now) == "renewed"


def test_the_handed_certificate_takes_its_share_from_the_environment(
    ca: CertificateAuthority,
) -> None:
    served = issue(ca, "served", timedelta(days=60))

    handed = hand_over_certificate(
        served.cert.read_bytes(), served.cert, {RESTART_SHARE_ENV: "0.5"}
    )

    assert handed.share == 0.5


def test_a_share_that_is_not_a_fraction_is_refused_without_its_value() -> None:
    with pytest.raises(SettingsError) as refused:
        hand_over_certificate(GARBAGE, Path("/nowhere"), {RESTART_SHARE_ENV: "7.25"})

    assert "7.25" not in str(refused.value)
    assert RESTART_SHARE_ENV in str(refused.value)


def test_bytes_that_are_no_certificate_are_refused_without_their_content() -> None:
    with pytest.raises(SettingsError) as refused:
        hand_over_certificate(GARBAGE, Path("/nowhere/tls.crt"), {})

    # It names what was read, the served certificate, not the variable.
    assert "--ssl-certfile" in str(refused.value)
    assert CERT_FILE_ENV not in str(refused.value)
    assert GARBAGE.decode() not in str(refused.value)
    assert "/nowhere" not in str(refused.value)
    assert refused.value.__cause__ is None
    assert certlife._handed_over is None


def test_nothing_handed_over_means_the_file_is_read_as_before(
    ca: CertificateAuthority,
) -> None:
    pair = issue(ca, "plain", timedelta(hours=5))

    loaded = load_certificate({CERT_FILE_ENV: str(pair.cert)})

    assert loaded is not None
    assert loaded.source == pair.cert
