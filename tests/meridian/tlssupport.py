"""Certificates for the tests that run TLS for real (S055).

A certificate authority and the leaf certificates it signs, made with
``cryptography`` and written to a directory the test owns. Python 3.13
verifies with strict X.509 flags: a CA needs a subject key identifier and key
usage, and a leaf an authority key identifier, or the chain is refused. The
keys are throwaway elliptic-curve keys that never leave the test's directory.
"""

import datetime
import ipaddress
import ssl
from dataclasses import dataclass
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

VALIDITY = datetime.timedelta(hours=1)
CLOCK_SKEW = datetime.timedelta(minutes=1)
TRUST_DOMAIN = "meridian.test"
PREFIX = f"spiffe://{TRUST_DOMAIN}/ns/meridian/sa/"
LOOPBACK = "127.0.0.1"


def spiffe(service_id: str) -> str:
    """The URI SAN of a service under the test trust domain."""
    return PREFIX + service_id


def uri_san(value: str) -> x509.GeneralName:
    return x509.UniformResourceIdentifier(value)


def dns_san(value: str) -> x509.GeneralName:
    return x509.DNSName(value)


def loopback_sans() -> list[x509.GeneralName]:
    """What a server on 127.0.0.1 presents, so a client verifies its address."""
    return [dns_san("localhost"), x509.IPAddress(ipaddress.ip_address(LOOPBACK))]


def _name(common_name: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])


def _write_pair(
    directory: Path,
    stem: str,
    key: ec.EllipticCurvePrivateKey,
    certificate: x509.Certificate,
) -> "KeyPair":
    cert_path = directory / f"{stem}.crt"
    key_path = directory / f"{stem}.key"
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return KeyPair(cert=cert_path, key=key_path)


@dataclass(frozen=True, slots=True)
class KeyPair:
    cert: Path
    key: Path


@dataclass(frozen=True, slots=True)
class CertificateAuthority:
    """A self-signed CA that signs leaves into its own directory."""

    directory: Path
    ca_file: Path
    _key: ec.EllipticCurvePrivateKey
    _certificate: x509.Certificate

    def issue(
        self,
        stem: str,
        common_name: str,
        sans: list[x509.GeneralName],
        not_before: datetime.datetime | None = None,
        not_after: datetime.datetime | None = None,
    ) -> KeyPair:
        """A leaf valid from ``not_before`` to ``not_after`` (by default from a
        minute ago for an hour from now)."""
        key = ec.generate_private_key(ec.SECP256R1())
        now = datetime.datetime.now(datetime.UTC)
        certificate = (
            x509.CertificateBuilder()
            .subject_name(_name(common_name))
            .issuer_name(self._certificate.subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(not_before or now - CLOCK_SKEW)
            .not_valid_after(not_after or now + VALIDITY)
            .add_extension(x509.SubjectAlternativeName(sans), critical=False)
            .add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_public_key(
                    self._key.public_key()
                ),
                critical=False,
            )
            .sign(self._key, hashes.SHA256())
        )
        return _write_pair(self.directory, stem, key, certificate)


def make_ca(directory: Path, name: str) -> CertificateAuthority:
    """A CA whose certificate is ``directory/<name>.crt``."""
    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.datetime.now(datetime.UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(_name(name))
        .issuer_name(_name(name))
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - CLOCK_SKEW)
        .not_valid_after(now + VALIDITY)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                key_cert_sign=True,
                crl_sign=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(key.public_key()),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    pair = _write_pair(directory, name, key, certificate)
    return CertificateAuthority(
        directory=directory, ca_file=pair.cert, _key=key, _certificate=certificate
    )


def client_context(
    ca: CertificateAuthority, identity: KeyPair | None
) -> ssl.SSLContext:
    """A client's context the way the platform builds one: verify the server
    against the CA (hostname checking on), and present ``identity`` if given."""
    context = ssl.create_default_context(cafile=str(ca.ca_file))
    if identity is not None:
        context.load_cert_chain(str(identity.cert), str(identity.key))
    return context
