"""The client's TLS settings and the context built from them (S055)."""

import ssl
from pathlib import Path

import pytest
from tlssupport import loopback_sans, make_ca, spiffe, uri_san

from meridian.platform.common.env import SettingsError
from meridian.platform.common.tls import (
    CA_FILE_ENV,
    CERT_FILE_ENV,
    KEY_FILE_ENV,
    ClientTls,
    verify_of,
)

ALL = {
    CERT_FILE_ENV: "/etc/meridian/tls/tls.crt",
    KEY_FILE_ENV: "/etc/meridian/tls/tls.key",
    CA_FILE_ENV: "/etc/meridian/tls/ca.crt",
}


def test_the_variables_are_named_as_the_chart_sets_them() -> None:
    assert (CERT_FILE_ENV, KEY_FILE_ENV, CA_FILE_ENV) == (
        "MERIDIAN_TLS_CERT_FILE",
        "MERIDIAN_TLS_KEY_FILE",
        "MERIDIAN_TLS_CA_FILE",
    )


def test_no_variable_gives_no_client_tls() -> None:
    assert ClientTls.from_env({"MERIDIAN_DATABASE_URL": "x"}) is None


@pytest.mark.parametrize("empty", [{}, {CERT_FILE_ENV: "", CA_FILE_ENV: ""}])
def test_unset_or_empty_variables_give_no_client_tls(empty: dict[str, str]) -> None:
    assert ClientTls.from_env(empty) is None


def test_all_three_give_the_paths() -> None:
    tls = ClientTls.from_env(ALL)

    assert tls == ClientTls(
        cert_file=Path(ALL[CERT_FILE_ENV]),
        key_file=Path(ALL[KEY_FILE_ENV]),
        ca_file=Path(ALL[CA_FILE_ENV]),
    )


@pytest.mark.parametrize("missing", [CERT_FILE_ENV, KEY_FILE_ENV, CA_FILE_ENV])
def test_two_of_the_three_name_the_missing_variable(missing: str) -> None:
    environ = {k: v for k, v in ALL.items() if k != missing}

    with pytest.raises(SettingsError, match=missing) as raised:
        ClientTls.from_env(environ)

    # The paths that were given are not in the message.
    assert not any(value in str(raised.value) for value in environ.values())


@pytest.mark.parametrize("given", [CERT_FILE_ENV, KEY_FILE_ENV, CA_FILE_ENV])
def test_one_of_the_three_names_the_two_missing_variables(given: str) -> None:
    others = [name for name in ALL if name != given]

    with pytest.raises(SettingsError) as raised:
        ClientTls.from_env({given: ALL[given]})

    assert all(name in str(raised.value) for name in others)
    assert given not in str(raised.value)


def test_an_empty_value_counts_as_missing_in_a_partial_set() -> None:
    with pytest.raises(SettingsError, match=KEY_FILE_ENV):
        ClientTls.from_env({**ALL, KEY_FILE_ENV: ""})


def test_no_client_tls_means_the_default_verification() -> None:
    assert verify_of(None) is True


# The context.
@pytest.fixture
def pki(tmp_path: Path) -> tuple[ClientTls, ClientTls]:
    """A CA and the client TLS of a service it signed, and a second one whose
    certificate belongs to another CA."""
    ca = make_ca(tmp_path, "ca")
    other = make_ca(tmp_path, "other-ca")
    sans = [uri_san(spiffe("agent-runtime")), *loopback_sans()]
    pair = ca.issue("agent-runtime", "agent-runtime", sans)
    foreign = other.issue("foreign", "agent-runtime", sans)
    return (
        ClientTls(cert_file=pair.cert, key_file=pair.key, ca_file=ca.ca_file),
        ClientTls(cert_file=foreign.cert, key_file=foreign.key, ca_file=ca.ca_file),
    )


def test_the_context_verifies_the_server_and_its_name_strictly(
    pki: tuple[ClientTls, ClientTls],
) -> None:
    context = pki[0].ssl_context()

    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
    assert context.verify_flags & ssl.VERIFY_X509_STRICT


def test_the_context_trusts_only_the_given_ca(
    pki: tuple[ClientTls, ClientTls],
) -> None:
    context = pki[0].ssl_context()

    subjects = [dict(rdn[0] for rdn in c["subject"]) for c in context.get_ca_certs()]
    assert subjects == [{"commonName": "ca"}]


def test_a_certificate_and_key_that_do_not_belong_together_stop_the_start(
    tmp_path: Path, pki: tuple[ClientTls, ClientTls]
) -> None:
    good, foreign = pki
    mismatched = ClientTls(
        cert_file=good.cert_file, key_file=foreign.key_file, ca_file=good.ca_file
    )

    with pytest.raises(SettingsError, match=f"{CERT_FILE_ENV} and {KEY_FILE_ENV}"):
        mismatched.ssl_context()


def test_a_file_that_is_missing_stops_the_start_naming_its_variable(
    tmp_path: Path, pki: tuple[ClientTls, ClientTls]
) -> None:
    good = pki[0]

    with pytest.raises(SettingsError, match=CA_FILE_ENV):
        ClientTls(
            cert_file=good.cert_file,
            key_file=good.key_file,
            ca_file=tmp_path / "absent.crt",
        ).ssl_context()
    with pytest.raises(SettingsError, match=CERT_FILE_ENV):
        ClientTls(
            cert_file=tmp_path / "absent.crt",
            key_file=good.key_file,
            ca_file=good.ca_file,
        ).ssl_context()


def test_a_ca_file_that_is_no_certificate_stops_the_start(
    tmp_path: Path, pki: tuple[ClientTls, ClientTls]
) -> None:
    good = pki[0]
    junk = tmp_path / "junk.crt"
    junk.write_text("not a certificate")

    with pytest.raises(SettingsError, match=CA_FILE_ENV):
        ClientTls(
            cert_file=good.cert_file, key_file=good.key_file, ca_file=junk
        ).ssl_context()


def test_verify_of_builds_the_context_for_a_client_tls(
    pki: tuple[ClientTls, ClientTls],
) -> None:
    verify = verify_of(pki[0])

    assert isinstance(verify, ssl.SSLContext)
