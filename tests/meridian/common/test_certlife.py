"""The certificate a process loaded, and when it should stop saying it is healthy
(S056, T-89)."""

import dataclasses
import logging
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path

import pytest
from tlssupport import CertificateAuthority, KeyPair, loopback_sans, make_ca

from meridian.platform.common import certlife
from meridian.platform.common.certlife import (
    RESTART_FRACTION,
    RESTART_MARGIN,
    LoadedCertificate,
    expiry_check,
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


def reissue(
    ca: CertificateAuthority, not_before: datetime, not_after: datetime
) -> KeyPair:
    """What cert-manager and the kubelet do at renewal: a new certificate in
    the same file (``leaf.crt``, the file ``issue`` wrote)."""
    return ca.issue("leaf", "leaf", loopback_sans(), not_before, not_after)


class Cycle:
    """A 90-day certificate loaded from its file, and a clock a test moves."""

    def __init__(self, ca: CertificateAuthority) -> None:
        self.ca = ca
        self.pair, self.not_before, self.not_after = issue(ca, timedelta(days=90))
        loaded = load_certificate(environ_of(self.pair))
        assert loaded is not None
        self.loaded = loaded
        self.now = self.not_before
        self.check = expiry_check(loaded, lambda: self.now)

    def renew_by(self, later: timedelta) -> None:
        """A certificate ending ``later`` after the loaded one is now on disk."""
        reissue(self.ca, self.not_before + later, self.not_after + later)

    def break_file(self, how: str) -> None:
        if how == "garbage":
            self.pair.cert.write_text(GARBAGE)
        elif how == "missing":
            self.pair.cert.unlink()
        elif how == "renewed":
            self.renew_by(timedelta(days=60))
        elif how == "older":
            reissue(
                self.ca,
                self.not_before - timedelta(days=60),
                self.not_after - timedelta(days=60),
            )
        elif how == "one-second-older":
            reissue(self.ca, self.not_before, self.not_after - SECOND)
        elif how == "equal":
            reissue(self.ca, self.not_before, self.not_after)
        else:
            assert how == "unchanged", how

    @property
    def restart_at(self) -> datetime:
        return self.loaded.restart_at


@pytest.fixture
def cycle(ca: CertificateAuthority) -> Cycle:
    return Cycle(ca)


def warnings_of(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.levelno == logging.WARNING]


def errors_of(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.levelno >= logging.ERROR]


def test_near_end_follows_the_rule_a_second_before_at_and_after_restart_at(
    cycle: Cycle,
) -> None:
    loaded = cycle.loaded

    # Not renewed on disk: a restart would load the same certificate.
    assert loaded.near_end(loaded.restart_at - SECOND) is False
    assert loaded.near_end(loaded.restart_at) is False
    assert loaded.near_end(loaded.restart_at + SECOND) is False
    # Renewed on disk: a restart loads it, from restart_at on.
    cycle.break_file("renewed")
    assert loaded.near_end(loaded.restart_at - SECOND) is False
    assert loaded.near_end(loaded.restart_at) is True
    assert loaded.near_end(loaded.restart_at + SECOND) is True


def test_a_certificate_that_has_ended_is_near_its_end_whatever_the_file_holds(
    cycle: Cycle,
) -> None:
    loaded = cycle.loaded

    assert loaded.near_end(loaded.not_after - SECOND) is False
    assert loaded.near_end(loaded.not_after) is True
    assert loaded.near_end(loaded.not_after + SECOND) is True
    cycle.break_file("missing")
    assert loaded.near_end(loaded.not_after) is True


def test_a_certificate_with_no_file_to_read_again_is_never_renewed() -> None:
    now = datetime.now(UTC)
    loaded = LoadedCertificate(not_before=now - timedelta(days=89), not_after=now)

    assert loaded.near_end(loaded.restart_at) is False
    assert loaded.near_end(loaded.not_after) is True


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


def test_a_renewal_on_disk_does_not_change_the_certificate_that_was_loaded(
    ca: CertificateAuthority,
) -> None:
    pair, _, not_after = issue(ca, timedelta(days=90))
    loaded = load_certificate(environ_of(pair))
    renewed = ca.issue("leaf", "leaf", loopback_sans())  # overwrites the file

    assert load_certificate(environ_of(renewed)) != loaded
    assert loaded is not None
    assert loaded.not_after == not_after


def test_the_loaded_certificate_never_shows_its_path(
    ca: CertificateAuthority,
) -> None:
    pair, _, _ = issue(ca, timedelta(days=90))

    loaded = load_certificate(environ_of(pair))

    assert loaded is not None
    assert str(pair.cert) not in repr(loaded)
    assert str(pair.cert.parent) not in repr(loaded)


# ── the health rule (S056, finding H1) ──────────────────────────────────────
@pytest.mark.parametrize("how", ["unchanged", "garbage", "missing", "renewed"])
def test_far_from_the_end_the_file_is_not_read_so_it_decides_nothing(
    cycle: Cycle,
    how: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    cycle.break_file(how)
    reads: list[Path] = []
    real_read_bytes = Path.read_bytes
    monkeypatch.setattr(
        Path, "read_bytes", lambda self: reads.append(self) or real_read_bytes(self)
    )

    with caplog.at_level(logging.DEBUG, logger=certlife.__name__):
        answers = []
        for now in (cycle.not_before, cycle.restart_at - SECOND):
            cycle.now = now
            answers.append(cycle.check())

    assert answers == [False, False]
    assert reads == []
    assert caplog.records == []


@pytest.mark.parametrize(
    "how",
    ["unchanged", "garbage", "missing", "older", "one-second-older", "equal"],
)
def test_at_restart_at_a_file_that_is_not_renewed_stays_healthy_and_warns_once(
    cycle: Cycle, how: str, caplog: pytest.LogCaptureFixture
) -> None:
    cycle.break_file(how)

    with caplog.at_level(logging.DEBUG, logger=certlife.__name__):
        answers = []
        for now in (
            cycle.restart_at,
            cycle.restart_at + SECOND,
            cycle.not_after - SECOND,
        ):
            cycle.now = now
            answers.append(cycle.check())

    warnings = warnings_of(caplog)
    assert answers == [False, False, False]
    assert len(warnings) == 1
    assert len(caplog.records) == 1
    assert cycle.not_after.isoformat() in warnings[0].getMessage()
    assert "no newer" in warnings[0].getMessage()
    assert "cert-manager has not renewed it" in warnings[0].getMessage()
    assert str(cycle.pair.cert) not in caplog.text
    assert str(cycle.pair.cert.parent) not in caplog.text
    assert GARBAGE not in caplog.text


@pytest.mark.parametrize("later", [SECOND, timedelta(days=60)])
def test_at_restart_at_a_renewed_file_turns_it_unhealthy_with_one_warning(
    cycle: Cycle, later: timedelta, caplog: pytest.LogCaptureFixture
) -> None:
    """The T-89 regression: the file on disk is compared with the certificate
    that was loaded, never with the clock."""
    cycle.renew_by(later)

    with caplog.at_level(logging.DEBUG, logger=certlife.__name__):
        answers = []
        for now in (
            cycle.restart_at - SECOND,
            cycle.restart_at,
            cycle.restart_at + SECOND,
        ):
            cycle.now = now
            answers.append(cycle.check())

    warnings = warnings_of(caplog)
    assert answers == [False, True, True]
    assert len(warnings) == 1
    assert len(caplog.records) == 1
    assert cycle.not_after.isoformat() in warnings[0].getMessage()
    assert str(cycle.pair.cert) not in caplog.text
    assert str(cycle.pair.cert.parent) not in caplog.text


@pytest.mark.parametrize("how", ["unchanged", "garbage", "missing", "renewed"])
def test_at_not_after_and_after_it_is_unhealthy_whatever_the_file_holds(
    cycle: Cycle, how: str, caplog: pytest.LogCaptureFixture
) -> None:
    cycle.break_file(how)

    with caplog.at_level(logging.DEBUG, logger=certlife.__name__):
        answers = []
        for now in (
            cycle.not_after,
            cycle.not_after + SECOND,
            cycle.not_after + timedelta(days=3),
        ):
            cycle.now = now
            answers.append(cycle.check())

    errors = errors_of(caplog)
    assert answers == [True, True, True]
    assert len(errors) == 1
    assert cycle.not_after.isoformat() in errors[0].getMessage()
    assert str(cycle.pair.cert) not in caplog.text
    assert str(cycle.pair.cert.parent) not in caplog.text
    assert GARBAGE not in caplog.text


def test_a_second_before_not_after_an_unrenewed_file_is_still_healthy(
    cycle: Cycle,
) -> None:
    cycle.now = cycle.not_after - SECOND

    assert cycle.check() is False


def test_each_message_is_logged_once_across_the_whole_cycle(
    cycle: Cycle, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG, logger=certlife.__name__):
        cycle.now = cycle.restart_at
        cycle.check()
        cycle.check()  # not renewed: one warning
        cycle.break_file("renewed")
        cycle.check()
        cycle.check()  # renewed: one more warning
        cycle.now = cycle.not_after
        cycle.check()
        cycle.check()  # ended: one error

    assert [r.levelno for r in caplog.records] == [
        logging.WARNING,
        logging.WARNING,
        logging.ERROR,
    ]
    assert str(cycle.pair.cert) not in caplog.text


def test_the_file_is_read_on_each_call_between_restart_at_and_not_after(
    cycle: Cycle,
) -> None:
    cycle.now = cycle.restart_at
    assert cycle.check() is False

    cycle.break_file("renewed")
    assert cycle.check() is True


def test_the_full_cycle_a_restart_loads_the_renewed_certificate_and_is_healthy(
    cycle: Cycle,
) -> None:
    cycle.now = cycle.restart_at
    cycle.break_file("renewed")
    assert cycle.check() is True  # the old process asks to be restarted

    restarted = load_certificate(environ_of(cycle.pair))
    assert restarted is not None
    after_restart = expiry_check(restarted, lambda: cycle.now)

    assert restarted.not_after > cycle.loaded.not_after
    assert after_restart() is False  # the new process is healthy at the same clock


def test_with_no_certificate_the_check_is_never_true() -> None:
    check = expiry_check(None, lambda: datetime(2999, 1, 1, tzinfo=UTC))

    assert check() is False


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


# ── the share of the margin (S073): the services' restarts are spread ─────────
SERVICES_IN_CHART = 6


def shared(not_before: datetime, lifetime: timedelta, place: int) -> LoadedCertificate:
    """A certificate of ``lifetime`` that the ``place``-th of six services loaded,
    with the share the chart gives it (place over count), as text through the
    environment's own parse."""
    return LoadedCertificate(
        not_before=not_before,
        not_after=not_before + lifetime,
        share=float(str(place / SERVICES_IN_CHART)),
    )


def restart_times(lifetime: timedelta) -> list[datetime]:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    return [
        shared(start, lifetime, place).restart_at for place in range(SERVICES_IN_CHART)
    ]


def test_the_variable_is_named_in_the_style_of_the_certificates_others() -> None:
    assert certlife.RESTART_SHARE_ENV == "MERIDIAN_TLS_RESTART_SHARE"


@pytest.mark.parametrize("environ", [{}, {certlife.RESTART_SHARE_ENV: ""}])
def test_an_absent_or_empty_share_is_zero(
    ca: CertificateAuthority, environ: dict[str, str]
) -> None:
    pair, _, _ = issue(ca, timedelta(days=90))

    loaded = load_certificate({**environ_of(pair), **environ})

    assert loaded is not None
    assert loaded.share == 0.0


@pytest.mark.parametrize("text", ["0", "0.0", "0.5", "0.8333333333333334", "0.999"])
def test_a_share_in_zero_to_below_one_is_read(
    ca: CertificateAuthority, text: str
) -> None:
    pair, _, _ = issue(ca, timedelta(days=90))

    loaded = load_certificate({**environ_of(pair), certlife.RESTART_SHARE_ENV: text})

    assert loaded is not None
    assert loaded.share == float(text)


@pytest.mark.parametrize(
    "text", ["1", "1.0", "1.5", "-0.1", "-1", "nan", "inf", "-inf", "abc", "0,5"]
)
def test_a_share_that_is_not_a_number_below_one_refuses_the_start(
    ca: CertificateAuthority, text: str
) -> None:
    pair, _, _ = issue(ca, timedelta(days=90))

    with pytest.raises(SettingsError) as raised:
        load_certificate({**environ_of(pair), certlife.RESTART_SHARE_ENV: text})

    assert certlife.RESTART_SHARE_ENV in str(raised.value)
    assert text not in str(raised.value).replace(certlife.RESTART_SHARE_ENV, "")
    assert raised.value.__cause__ is None
    assert raised.value.__suppress_context__ is True


def test_a_bad_share_is_refused_even_when_no_certificate_is_named() -> None:
    with pytest.raises(SettingsError):
        load_certificate({certlife.RESTART_SHARE_ENV: "1"})


def test_a_share_of_zero_restarts_exactly_when_it_did_before() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    ninety_days = LoadedCertificate(start, start + timedelta(days=90))
    one_hour = LoadedCertificate(start, start + timedelta(hours=1))

    assert ninety_days.restart_at == ninety_days.not_after - timedelta(hours=24)
    assert one_hour.restart_at == one_hour.not_after - timedelta(minutes=10)
    assert ninety_days.share == one_hour.share == 0.0


def test_six_services_of_a_one_hour_certificate_restart_100_seconds_apart() -> None:
    times = restart_times(timedelta(hours=1))

    gaps = {earlier - later for earlier, later in pairwise(times)}

    assert gaps == {timedelta(seconds=100)}


def test_six_services_of_a_90_day_certificate_restart_4_hours_apart() -> None:
    times = restart_times(timedelta(days=90))

    gaps = {earlier - later for earlier, later in pairwise(times)}

    assert gaps == {timedelta(hours=4)}


@pytest.mark.parametrize(
    "lifetime", [timedelta(hours=1), timedelta(days=3), timedelta(days=90)]
)
def test_no_service_restarts_later_than_it_did_before_the_share(
    lifetime: timedelta,
) -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    before_the_share = LoadedCertificate(start, start + lifetime).restart_at

    times = restart_times(lifetime)

    assert times[0] == before_the_share
    assert all(moment <= before_the_share for moment in times)


@pytest.mark.parametrize(
    "lifetime", [timedelta(hours=1), timedelta(days=3), timedelta(days=90)]
)
def test_every_restart_comes_after_the_renewal_at_the_default_renew_before(
    lifetime: timedelta,
) -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    renewal = start + lifetime - lifetime / 3  # cert-manager's default

    times = restart_times(lifetime)

    assert all(moment > renewal for moment in times)


def test_the_last_service_restarts_at_most_one_and_five_sixths_margins_early() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    loaded = shared(start, timedelta(days=90), SERVICES_IN_CHART - 1)

    early = loaded.not_after - loaded.restart_at

    assert early < 2 * RESTART_MARGIN
    assert abs(early - RESTART_MARGIN * 11 / 6) < timedelta(milliseconds=1)


def test_a_share_moves_the_first_look_at_the_file_earlier_by_that_share_of_the_margin(
    ca: CertificateAuthority,
) -> None:
    pair, not_before, not_after = issue(ca, timedelta(days=90))
    loaded = LoadedCertificate(not_before, not_after, pair.cert, share=0.5)
    reissue(ca, not_before + timedelta(days=60), not_after + timedelta(days=60))
    unshared_restart = not_after - RESTART_MARGIN

    assert loaded.restart_at == unshared_restart - RESTART_MARGIN / 2
    assert loaded.verdict(loaded.restart_at - SECOND) == "far"
    # The renewed file is read from the new time on, and at the time it was
    # read before the share.
    assert loaded.verdict(loaded.restart_at) == "renewed"
    assert loaded.verdict(unshared_restart) == "renewed"


def test_with_a_share_an_unrenewed_file_stays_healthy_over_the_wider_window(
    ca: CertificateAuthority,
) -> None:
    pair, not_before, not_after = issue(ca, timedelta(days=90))
    loaded = LoadedCertificate(not_before, not_after, pair.cert, share=0.5)

    inside_the_widened_window = not_after - RESTART_MARGIN - SECOND

    assert loaded.verdict(inside_the_widened_window) == "not-renewed"
    assert loaded.near_end(inside_the_widened_window) is False
    assert loaded.verdict(not_after) == "ended"


def test_a_renewal_after_a_services_restart_time_costs_the_spread_not_health(
    ca: CertificateAuthority,
) -> None:
    # One hour, a margin of 10 minutes; share 5/6 looks at the file 18 min 20 s
    # before the end, share 0 at 10 minutes. The renewal comes 5 minutes before
    # the end: after both looks, so the rule must read the file and not the
    # clock alone.
    pair, not_before, not_after = issue(ca, timedelta(hours=1))
    now = not_before
    last = LoadedCertificate(not_before, not_after, pair.cert, share=5 / 6)
    first = LoadedCertificate(not_before, not_after, pair.cert, share=0.0)
    last_check = expiry_check(last, lambda: now)
    first_check = expiry_check(first, lambda: now)

    eighteen_twenty_early = not_after - timedelta(minutes=18, seconds=20)
    assert abs(last.restart_at - eighteen_twenty_early) < timedelta(milliseconds=1)
    now = last.restart_at
    assert last.verdict(now) == "not-renewed"
    assert (last_check(), first_check()) == (False, False)
    now = first.restart_at
    assert (last_check(), first_check()) == (False, False)

    now = not_after - timedelta(minutes=5)
    reissue(ca, not_before + timedelta(minutes=30), not_after + timedelta(hours=1))

    assert (last_check(), first_check()) == (True, True)
