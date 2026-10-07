"""The certificate a process loaded, and when it should stop saying it is healthy
(S056, T-89).

A service reads its certificate when it starts. cert-manager renews the Secret
before the end and the kubelet rewrites the mounted file, but the process keeps
the old certificate in memory, and the kubelet's probe verifies no certificate.
So the service itself says when a restart is worth asking for: ``/healthz``
answers 503, the kubelet restarts the container, and the new process loads the
renewed file.

The rule, by the clock and the file (the file is compared with the certificate
that was loaded, never with the clock)::

    now <  restart_at                    healthy; the file is not read
                                         (restart_at: the margin, and the
                                         service's share of one more, before
                                         the end; see RESTART_SHARE_ENV)
    now >= not_after                     unhealthy: never green past the end
    otherwise, the first certificate in the file is read again:
        unreadable, or ends no later than the loaded one -> healthy
        ends later than the loaded one                   -> unhealthy

Why not the clock alone: if cert-manager's renewal failed, the file on disk is
still the old certificate, so a restarted container loads the same one, answers
503 from its first probe and is never Ready. All six services would loop for the
last 24 hours of the certificate, an outage a day before the end that doing
nothing would not have caused. A restart is only worth asking for when it loads
something newer, or when the certificate has ended and nothing is left to lose.
"""

import dataclasses
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from cryptography import x509

from meridian.platform.common.env import SettingsError
from meridian.platform.common.tls import CERT_FILE_ENV

logger = logging.getLogger(__name__)

# cert-manager renews when a third of the lifetime is left (30 of 90 days). From
# the smaller of 24 hours and a sixth of the lifetime before the end, the
# service looks at the file again, so the order "renewed, then restarted, then
# expired" holds for any lifetime, a one-hour test certificate included. Inside
# the margin it asks for a restart only if the file holds a newer certificate
# (see the module docstring for why, and for the loop that would follow if it
# asked on the clock alone).
RESTART_MARGIN = timedelta(hours=24)
RESTART_FRACTION = 6
# A share is a fraction of one margin, a number from 0 up to, but not including,
# 1, that moves the moment a service starts to look at the file EARLIER than the
# margin alone would: the service looks at restart_at = end - margin * (1 +
# share). The chart gives each service its place in the list of services over
# the count of them (with six services: 0, 1/6, 2/6 ... 5/6, the first none and
# the last five sixths), so the services do not all restart in the same minute,
# and a restart only ever moves earlier, by less than one margin. Absent or
# empty means 0, the moment the margin alone gives. Looking is all a share
# changes: a service whose file has not been renewed stays healthy past its
# restart_at, up to the end of its certificate.
RESTART_SHARE_ENV = "MERIDIAN_TLS_RESTART_SHARE"

Verdict = Literal["far", "ended", "not-renewed", "renewed"]


@dataclass(frozen=True, slots=True)
class LoadedCertificate:
    """The validity of the certificate the process loaded (UTC), and the file it
    came from, so that the file can be read again inside the margin. The file's
    path is neither shown nor compared: it must reach no log line, no exception
    message and no answer."""

    not_before: datetime
    not_after: datetime
    source: Path | None = field(default=None, repr=False, compare=False)
    share: float = 0.0

    @property
    def restart_at(self) -> datetime:
        """From this moment the service looks at the file again: the margin and
        its ``share`` of one more before the end (a share is a fraction of one
        margin, 0 up to, not including, 1). Looking is not restarting: the
        service asks for a restart only when the file holds a newer certificate
        or the certificate has ended, so a file that was never renewed stays
        healthy after this moment, until the certificate ends."""
        lifetime = self.not_after - self.not_before
        margin = min(RESTART_MARGIN, lifetime / RESTART_FRACTION)
        return self.not_after - margin * (1 + self.share)

    def verdict(self, now: datetime) -> Verdict:
        """Where ``now`` stands: ``far`` from the end (the file is not read),
        ``ended``, ``not-renewed`` (inside the margin and the file holds no newer
        certificate, or cannot be read) or ``renewed`` (it holds a newer one)."""
        if now < self.restart_at:
            return "far"
        if now >= self.not_after:
            return "ended"
        on_disk = self._on_disk()
        if on_disk is None or on_disk.not_after <= self.not_after:
            return "not-renewed"
        return "renewed"

    def near_end(self, now: datetime) -> bool:
        """Whether a restart is worth asking for, or the certificate has ended."""
        return self.verdict(now) in ("ended", "renewed")

    def _on_disk(self) -> "LoadedCertificate | None":
        """The first certificate in the file now, or None when there is no file
        to read or it cannot be read or parsed. Nothing of the failure is kept."""
        if self.source is None:
            return None
        try:
            return _read_certificate(self.source)
        except (OSError, ValueError):
            return None


def utc_now() -> datetime:
    return datetime.now(UTC)


def expiry_check(
    certificate: LoadedCertificate | None, clock: Callable[[], datetime] = utc_now
) -> Callable[[], bool]:
    """The health rule both kinds of service share: a function that says
    whether to answer unhealthy by ``clock``, by the rule in the module
    docstring. It is never true when there is no certificate. Each of the three
    messages (renewed on disk, not renewed, ended) is logged once, with the
    certificate's end and never a path, not once per probe."""
    logged: set[Verdict] = set()

    def near_end() -> bool:
        if certificate is None:
            return False
        verdict = certificate.verdict(clock())
        if verdict != "far" and verdict not in logged:
            logged.add(verdict)
            _log_first(verdict, certificate.not_after)
        return verdict in ("ended", "renewed")

    return near_end


def _log_first(verdict: Verdict, not_after: datetime) -> None:
    end = not_after.isoformat()
    if verdict == "renewed":
        logger.warning(
            "the certificate this process loaded ends %s and a renewed one is "
            "on disk; reporting unhealthy so the container is restarted with it",
            end,
        )
    elif verdict == "not-renewed":
        logger.warning(
            "the certificate this process loaded ends %s, and the file holds "
            "no newer one: staying healthy until then; cert-manager has not "
            "renewed it",
            end,
        )
    elif verdict == "ended":
        logger.error(
            "the certificate this process loaded ended at %s; reporting unhealthy",
            end,
        )


def _read_certificate(path: Path) -> LoadedCertificate:
    """The dates of the first certificate in ``path``. Raise ``OSError`` or
    ``ValueError``, whose text can quote the path or the content."""
    certificate = x509.load_pem_x509_certificate(path.read_bytes())
    return LoadedCertificate(
        not_before=certificate.not_valid_before_utc.astimezone(UTC),
        not_after=certificate.not_valid_after_utc.astimezone(UTC),
        source=path,
    )


def load_certificate(environ: Mapping[str, str]) -> LoadedCertificate | None:
    """The dates of the first certificate in the file ``MERIDIAN_TLS_CERT_FILE``
    names (cert-manager writes the leaf first), or None when it is unset or
    empty. The file is read here, and again only inside the margin (see
    ``LoadedCertificate.verdict``).

    Raise ``SettingsError`` naming the variable when the file cannot be read or
    parsed. The text of the failure can quote the path or the file's content,
    so it is not carried. The same for ``MERIDIAN_TLS_RESTART_SHARE`` when it is
    set and is not a number from 0 up to, not including, 1.
    """
    share = _restart_share(environ)
    path = environ.get(CERT_FILE_ENV)
    if not path:
        return None
    try:
        loaded = _read_certificate(Path(path))
    except (OSError, ValueError):
        raise SettingsError(
            f"{CERT_FILE_ENV} cannot be read as a certificate"
        ) from None
    return dataclasses.replace(loaded, share=share)


def _restart_share(environ: Mapping[str, str]) -> float:
    """The share of the margin, 0 when the variable is unset or empty. The value
    is never quoted: it is whatever the deployment set."""
    text = environ.get(RESTART_SHARE_ENV)
    if not text:
        return 0.0
    try:
        share = float(text)
    except ValueError:
        share = float("nan")
    if not 0 <= share < 1:  # written so that nan is refused too
        raise SettingsError(
            f"{RESTART_SHARE_ENV} must be a number from zero up to, but not "
            "including, one"
        ) from None
    return share
