"""A caller's TLS identity: the certificate it presents and the CA it trusts (S055).

The platform's services prove which service they are by mutual TLS. A service
that calls another gets three files from the chart (a certificate, its key and
the CA's certificate) named by three variables. ``ClientTls`` holds the three
paths; ``ssl_context`` builds the one context every client of the service
passes as ``verify=``. It trusts only the given CA (not the system's bundle),
checks the server's host name and keeps Python's strict X.509 checks. Nothing
here turns verification off: a client with no variables gets the library's
default context, which does not know the CA, so a call to an ``https`` service
fails instead of going unverified.
"""

import ssl
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Self

from meridian.platform.common.env import SettingsError

CERT_FILE_ENV = "MERIDIAN_TLS_CERT_FILE"
KEY_FILE_ENV = "MERIDIAN_TLS_KEY_FILE"
CA_FILE_ENV = "MERIDIAN_TLS_CA_FILE"


@dataclass(frozen=True, slots=True)
class ClientTls:
    cert_file: Path
    key_file: Path
    ca_file: Path

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> Self | None:
        """None when none of the three variables is set; the paths when all
        are; ``SettingsError`` naming the missing variables for a partial set
        (never a path)."""
        names = (CERT_FILE_ENV, KEY_FILE_ENV, CA_FILE_ENV)
        if not any(environ.get(name) for name in names):
            return None
        missing = [name for name in names if not environ.get(name)]
        if missing:
            raise SettingsError(
                f"{', '.join(missing)} must be set with the other TLS variables"
            )
        return cls(
            cert_file=Path(environ[CERT_FILE_ENV]),
            key_file=Path(environ[KEY_FILE_ENV]),
            ca_file=Path(environ[CA_FILE_ENV]),
        )

    def ssl_context(self) -> ssl.SSLContext:
        """The client context: the server is verified against the CA alone and
        by name, and the certificate chain is presented to the server. Raise
        ``SettingsError`` naming the variables when a file cannot be used (the
        text of the failure can quote a path or a file's content, so it is not
        carried)."""
        try:
            context = ssl.create_default_context(cafile=str(self.ca_file))
        except (OSError, ssl.SSLError):
            raise SettingsError(f"{CA_FILE_ENV} cannot be loaded") from None
        try:
            context.load_cert_chain(str(self.cert_file), str(self.key_file))
        except (OSError, ssl.SSLError):
            raise SettingsError(
                f"{CERT_FILE_ENV} and {KEY_FILE_ENV} cannot be loaded as a "
                "certificate and its key"
            ) from None
        return context


def verify_of(tls: ClientTls | None) -> ssl.SSLContext | bool:
    """What a client passes as ``verify=``: the context of ``tls``, or the
    library's default verification when there is none (never ``False``)."""
    return True if tls is None else tls.ssl_context()
