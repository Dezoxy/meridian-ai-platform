"""The certificate a process loaded, and when it should stop saying it is healthy
(S056, T-89).

A service reads its certificate once, when it starts. cert-manager renews the
Secret before the end and the kubelet rewrites the mounted file, but the
process keeps the old certificate in memory, and the kubelet's probe verifies no
certificate. So the service itself says when the certificate it holds is near
its end: ``/healthz`` answers 503, the kubelet restarts the container, and the
new process loads the renewed file.

Nothing here opens the file again after ``load_certificate`` returned: the file
on disk changes at renewal, the process's certificate does not.
"""

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography import x509

from meridian.platform.common.env import SettingsError
from meridian.platform.common.tls import CERT_FILE_ENV

logger = logging.getLogger(__name__)

# cert-manager renews when a third of the lifetime is left (30 of 90 days). The
# service turns unhealthy when the smaller of 24 hours and a sixth of the
# lifetime is left, so the order "renewed, then restarted, then expired" holds
# for any lifetime, a one-hour test certificate included.
RESTART_MARGIN = timedelta(hours=24)
RESTART_FRACTION = 6


@dataclass(frozen=True, slots=True)
class LoadedCertificate:
    """The validity of the certificate the process loaded (UTC)."""

    not_before: datetime
    not_after: datetime

    @property
    def restart_at(self) -> datetime:
        """From this moment the service asks to be restarted."""
        lifetime = self.not_after - self.not_before
        return self.not_after - min(RESTART_MARGIN, lifetime / RESTART_FRACTION)

    def near_end(self, now: datetime) -> bool:
        return now >= self.restart_at


def utc_now() -> datetime:
    return datetime.now(UTC)


def expiry_check(
    certificate: LoadedCertificate | None, clock: Callable[[], datetime] = utc_now
) -> Callable[[], bool]:
    """The health rule both kinds of service share: a function that says
    whether the loaded certificate is near its end by ``clock``. It is never
    true when there is no certificate. The first time it is true it logs one
    warning with the certificate's end (never a path), not one per probe."""
    warned = False

    def near_end() -> bool:
        nonlocal warned
        if certificate is None or not certificate.near_end(clock()):
            return False
        if not warned:
            warned = True
            logger.warning(
                "the certificate this process loaded ends %s; reporting "
                "unhealthy so the container is restarted with the renewed one",
                certificate.not_after.isoformat(),
            )
        return True

    return near_end


def load_certificate(environ: Mapping[str, str]) -> LoadedCertificate | None:
    """The dates of the first certificate in the file ``MERIDIAN_TLS_CERT_FILE``
    names (cert-manager writes the leaf first), or None when it is unset or
    empty. The file is read once, here.

    Raise ``SettingsError`` naming the variable when the file cannot be read or
    parsed. The text of the failure can quote the path or the file's content,
    so it is not carried.
    """
    path = environ.get(CERT_FILE_ENV)
    if not path:
        return None
    try:
        certificate = x509.load_pem_x509_certificate(Path(path).read_bytes())
        return LoadedCertificate(
            not_before=certificate.not_valid_before_utc.astimezone(UTC),
            not_after=certificate.not_valid_after_utc.astimezone(UTC),
        )
    except (OSError, ValueError):
        raise SettingsError(
            f"{CERT_FILE_ENV} cannot be read as a certificate"
        ) from None
