"""The certificate a process loaded, and when it should stop saying it is healthy
(S056, T-89)."""

import dataclasses
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from tlssupport import CertificateAuthority, KeyPair, loopback_sans, make_ca

from meridian.platform.common.certlife import (
    RESTART_FRACTION,
    RESTART_MARGIN,
    LoadedCertificate,
    load_certificate,
)
from meridian.platform.common.env import SettingsError
from meridian.platform.common.tls import CERT_FILE_ENV

GARBAGE = "not-a-certificate-canary-4417"
SECOND = timedelta(seconds=1)


def whole_second(moment: datetime) -> datetime:
    """A certificate keeps whole seconds."""
    return moment.replace(microsecond=0)


@pytest.fixture
def ca(tmp_path: Path) -> CertificateAuthority:
    return make_ca(tmp_path, "certlife-ca")


def issue(
    ca: CertificateAuthority, lifetime: timedelta, stem: str = "leaf"
) -> tuple[KeyPair, datetime, datetime]:
    """A leaf that began an hour ago and lasts ``lifetime`` from then."""
    not_before = whole_second(datetime.now(UTC) - timedelta(hours=1))
    not_after = not_before + lifetime
    pair = ca.issue(stem, stem, loopback_sans(), not_before, not_after)
    return pair, not_before, not_after


def environ_of(pair: KeyPair) -> dict[str, str]:
    return {CERT_FILE_ENV: str(pair.cert)}


def test_the_margin_is_a_day_or_a_sixth_of_the_lifetime() -> None:
    assert (timedelta(hours=24), 6) == (RESTART_MARGIN, RESTART_FRACTION)


@pytest.mark.parametrize("environ", [{}, {CERT_FILE_ENV: ""}])
def test_no_variable_gives_no_certificate(environ: dict[str, str]) -> None:
    assert load_certificate(environ) is None


def test_a_real_certificates_dates_are_read(ca: CertificateAuthority) -> None:
    pair, not_before, not_after = issue(ca, timedelta(days=90))

    loaded = load_certificate(environ_of(pair))

    assert loaded == LoadedCertificate(not_before=not_before, not_after=not_after)
    assert loaded is not None
    assert loaded.not_before.utcoffset() == timedelta(0)
    assert loaded.not_after.utcoffset() == timedelta(0)


def test_the_margin_is_24_hours_for_a_90_day_certificate(
    ca: CertificateAuthority,
) -> None:
    pair, _, not_after = issue(ca, timedelta(days=90))

    loaded = load_certificate(environ_of(pair))

    assert loaded is not None
    assert loaded.restart_at == not_after - timedelta(hours=24)


def test_the_margin_is_a_sixth_of_the_lifetime_for_a_one_hour_certificate(
    ca: CertificateAuthority,
) -> None:
    pair, _, not_after = issue(ca, timedelta(hours=1))

    loaded = load_certificate(environ_of(pair))

    assert loaded is not None
    assert loaded.restart_at == not_after - timedelta(minutes=10)


def test_the_margin_is_still_24_hours_just_above_six_days(
    ca: CertificateAuthority,
) -> None:
    # A sixth of 6 days is 24 hours: from there up, the day is the smaller.
    pair, _, not_after = issue(ca, timedelta(days=6, hours=6))

    loaded = load_certificate(environ_of(pair))

    assert loaded is not None
    assert loaded.restart_at == not_after - timedelta(hours=24)


def test_near_end_is_false_a_second_before_restart_at_and_true_at_it(
    ca: CertificateAuthority,
) -> None:
    pair, _, _ = issue(ca, timedelta(days=90))
    loaded = load_certificate(environ_of(pair))
    assert loaded is not None

    assert loaded.near_end(loaded.restart_at - SECOND) is False
    assert loaded.near_end(loaded.restart_at) is True
    assert loaded.near_end(loaded.restart_at + SECOND) is True


def test_the_loaded_certificate_cannot_be_changed(ca: CertificateAuthority) -> None:
    pair, _, _ = issue(ca, timedelta(days=90))
    loaded = load_certificate(environ_of(pair))
    assert loaded is not None

    with pytest.raises(dataclasses.FrozenInstanceError):
        loaded.not_after = loaded.not_before  # type: ignore[misc]


def test_a_file_with_two_certificates_gives_the_first_ones_dates(
    ca: CertificateAuthority, tmp_path: Path
) -> None:
    first, not_before, not_after = issue(ca, timedelta(days=90), "first")
    second, _, _ = issue(ca, timedelta(days=10), "second")
    chain = tmp_path / "chain.crt"
    chain.write_bytes(first.cert.read_bytes() + second.cert.read_bytes())

    loaded = load_certificate({CERT_FILE_ENV: str(chain)})

    assert loaded == LoadedCertificate(not_before=not_before, not_after=not_after)


def test_the_file_is_read_once_so_a_renewal_on_disk_changes_nothing(
    ca: CertificateAuthority,
) -> None:
    pair, _, not_after = issue(ca, timedelta(days=90))
    loaded = load_certificate(environ_of(pair))
    renewed = ca.issue("leaf", "leaf", loopback_sans())  # overwrites the file

    assert load_certificate(environ_of(renewed)) != loaded
    assert loaded is not None
    assert loaded.not_after == not_after


def test_a_missing_file_raises_naming_the_variable_and_not_the_path(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "no-such-directory-canary" / "tls.crt"

    with pytest.raises(SettingsError) as raised:
        load_certificate({CERT_FILE_ENV: str(missing)})

    assert CERT_FILE_ENV in str(raised.value)
    assert str(missing) not in str(raised.value)
    assert "canary" not in str(raised.value)
    assert raised.value.__cause__ is None
    assert raised.value.__suppress_context__ is True


def test_a_file_of_garbage_raises_naming_the_variable_and_not_the_content(
    tmp_path: Path,
) -> None:
    garbage = tmp_path / "garbage.crt"
    garbage.write_text(GARBAGE)

    with pytest.raises(SettingsError) as raised:
        load_certificate({CERT_FILE_ENV: str(garbage)})

    assert CERT_FILE_ENV in str(raised.value)
    assert str(garbage) not in str(raised.value)
    assert GARBAGE not in str(raised.value)
    assert raised.value.__cause__ is None
    assert raised.value.__suppress_context__ is True


def test_a_pem_block_that_is_not_a_certificate_raises_too(tmp_path: Path) -> None:
    broken = tmp_path / "broken.crt"
    broken.write_text(
        f"-----BEGIN CERTIFICATE-----\n{GARBAGE}\n-----END CERTIFICATE-----\n"
    )

    with pytest.raises(SettingsError) as raised:
        load_certificate({CERT_FILE_ENV: str(broken)})

    assert GARBAGE not in str(raised.value)
    assert str(broken) not in str(raised.value)
