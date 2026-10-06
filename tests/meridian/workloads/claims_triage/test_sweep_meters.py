"""What a sweep pass found, as gauges the sweep sends before it exits (S064).

The first half records a planted ``PassResult`` on a provider with an in-memory
reader; the second runs ``main`` against PostgreSQL with the provider replaced,
to hold what happens around the flush: it is called after the pass and before
the exit, it is bounded, and nothing it does changes the exit code or the
summary line.
"""

import logging
import sys
from collections.abc import Callable

import pytest
from dbsupport import DatabaseHandle
from opentelemetry import metrics as otel_metrics
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import (
    InMemoryMetricReader,
    MetricExporter,
    MetricExportResult,
    MetricsData,
    PeriodicExportingMetricReader,
)
from servicesupport import metric_points
from sweepsupport import (
    LOGGER,
    MINUTE,
    add_claim,
    breaking,
    environ_of,
)

from meridian.platform.common import metrics as common_metrics
from meridian.platform.common.env import SettingsError
from meridian.workloads.claims_triage import sweep, sweep_meters
from meridian.workloads.claims_triage.sweep import (
    DATABASE_URL_ENV,
    TRIAGE_LEASE_SECONDS,
    PassResult,
    main,
)
from meridian.workloads.claims_triage.sweep_meters import (
    FLUSH_TIMEOUT_MILLIS,
    SHUTDOWN_TIMEOUT_MILLIS,
    findings,
    record_pass,
    report_pass,
)

SERIES = "meridian.sweep.last_pass"
CANARY = "claimant-text-canary-74"
METERS_LOGGER = "meridian.workloads.claims_triage.sweep_meters"
PLANTED = PassResult(
    overdue=1, not_started=2, abandoned=3, runs_ended=4, threads_cleaned=5, failures=6
)
# The six words, one for each number of the summary line.
FINDING_WORDS = {
    "documents-overdue": 1,
    "triage-not-started": 2,
    "triage-abandoned": 3,
    "runs-ended": 4,
    "threads-cleaned": 5,
    "failures": 6,
}
# A quarter of the CronJob's deadline for a pass (120 s).
BUDGET_MILLIS = 30_000


def series_of(reader: InMemoryMetricReader) -> dict[str, float]:
    return {a["meridian.finding"]: v for a, v in metric_points(reader, SERIES)}


# ── the gauges ──────────────────────────────────────────────────────────────
def test_the_six_gauges_are_the_numbers_of_the_pass_under_their_fixed_words() -> None:
    reader = InMemoryMetricReader()

    record_pass(MeterProvider(metric_readers=[reader]), PLANTED)

    assert series_of(reader) == FINDING_WORDS


def test_the_only_label_is_the_finding_word() -> None:
    reader = InMemoryMetricReader()

    record_pass(MeterProvider(metric_readers=[reader]), PLANTED)

    keys = {
        key for attributes, _ in metric_points(reader, SERIES) for key in attributes
    }
    assert keys == {"meridian.finding"}


def test_a_pass_that_found_nothing_is_six_series_of_zero() -> None:
    reader = InMemoryMetricReader()

    record_pass(MeterProvider(metric_readers=[reader]), PassResult(0, 0, 0, 0, 0, 0))

    assert series_of(reader) == dict.fromkeys(FINDING_WORDS, 0)


def test_the_gauge_is_a_gauge_that_keeps_the_last_value_set() -> None:
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])

    record_pass(provider, PLANTED)
    record_pass(provider, PassResult(7, 7, 7, 7, 7, 7))

    assert series_of(reader) == dict.fromkeys(FINDING_WORDS, 7)
    (metric,) = [
        m
        for r in reader.get_metrics_data().resource_metrics
        for s in r.scope_metrics
        for m in s.metrics
        if m.name == SERIES
    ]
    assert type(metric.data).__name__ == "Gauge"


def test_the_words_are_the_summary_lines_and_the_moves_own() -> None:
    assert findings(PLANTED) == FINDING_WORDS
    assert list(findings(PLANTED)) == [
        sweep.DOCUMENTS_OVERDUE.trigger,
        sweep.TRIAGE_NOT_STARTED.trigger,
        sweep.TRIAGE_ABANDONED.trigger,
        sweep.RUNS_ENDED,
        sweep.THREADS_CLEANED,
        sweep.FAILURES,
    ]


def test_the_bounds_of_the_flush_and_the_shutdown_fit_the_cronjobs_deadline() -> None:
    assert FLUSH_TIMEOUT_MILLIS == 5_000
    assert SHUTDOWN_TIMEOUT_MILLIS == 5_000
    assert FLUSH_TIMEOUT_MILLIS + SHUTDOWN_TIMEOUT_MILLIS <= BUDGET_MILLIS


# ── a provider that records what is asked of it ─────────────────────────────
class Spy(MeterProvider):
    """A provider with an in-memory reader that notes its calls, what had been
    set when the flush came, and the bounds it was given; it can fail them."""

    def __init__(
        self,
        *,
        flush_raises: bool = False,
        shutdown_raises: bool = False,
    ) -> None:
        self.reader = InMemoryMetricReader()
        super().__init__(metric_readers=[self.reader])
        self.calls: list[str] = []
        self.bounds: dict[str, float] = {}
        self.flushed: dict[str, float] = {}
        self.flush_raises = flush_raises
        self.shutdown_raises = shutdown_raises

    def force_flush(self, timeout_millis: float = 10_000) -> bool:
        self.calls.append("flush")
        self.bounds["flush"] = timeout_millis
        self.flushed = series_of(self.reader)
        if self.flush_raises:
            raise RuntimeError("canary-address.invalid refused the export")
        return True

    def shutdown(self, timeout_millis: float = 30_000) -> None:
        self.calls.append("shutdown")
        self.bounds["shutdown"] = timeout_millis
        # As the SDK does: it gives up its exit hook, then raises.
        super().shutdown(timeout_millis=timeout_millis)
        if self.shutdown_raises:
            raise RuntimeError("canary-address.invalid refused the shutdown")


def use(monkeypatch: pytest.MonkeyPatch, spy: Spy) -> Spy:
    monkeypatch.setattr(sweep_meters, "make_meter_provider", lambda _name: spy)
    return spy


def warnings_of(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == METERS_LOGGER and r.levelno == logging.WARNING
    ]


# ── report_pass ─────────────────────────────────────────────────────────────
def test_the_pass_is_set_then_flushed_then_shut_down_each_within_its_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spy = use(monkeypatch, Spy())

    report_pass(PLANTED)

    assert spy.calls == ["flush", "shutdown"]
    assert spy.flushed == FINDING_WORDS
    assert spy.bounds == {
        "flush": FLUSH_TIMEOUT_MILLIS,
        "shutdown": SHUTDOWN_TIMEOUT_MILLIS,
    }


@pytest.mark.parametrize(
    ("options", "warning"),
    [
        pytest.param(
            {"flush_raises": True},
            "the sweep's metrics were not sent: the flush failed (RuntimeError)",
            id="flush-raises",
        ),
        pytest.param(
            {"shutdown_raises": True},
            "the sweep's metrics were not sent: the shutdown failed (RuntimeError)",
            id="shutdown-raises",
        ),
        pytest.param(
            {"flush_raises": True, "shutdown_raises": True},
            "the sweep's metrics were not sent: the flush failed (RuntimeError)",
            id="both-fail",
        ),
    ],
)
def test_a_flush_or_shutdown_that_fails_is_one_warning_with_a_class_and_no_address(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    options: dict[str, bool],
    warning: str,
) -> None:
    spy = use(monkeypatch, Spy(**options))

    report_pass(PLANTED)

    assert warnings_of(caplog) == [warning]
    assert "canary-address" not in caplog.text
    # The shutdown is tried however the flush went.
    assert spy.calls == ["flush", "shutdown"]


def test_a_provider_that_cannot_be_built_is_one_warning_and_nothing_is_raised(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def refuse(_name: str) -> MeterProvider:
        raise SettingsError("OTEL_EXPORTER_OTLP_CERTIFICATE must name the CA file")

    monkeypatch.setattr(sweep_meters, "make_meter_provider", refuse)

    report_pass(PLANTED)

    assert warnings_of(caplog) == [
        "the sweep's metrics were not sent: "
        "OTEL_EXPORTER_OTLP_CERTIFICATE must name the CA file"
    ]


def test_an_unexpected_failure_building_the_provider_names_only_its_class(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def refuse(_name: str) -> MeterProvider:
        raise OSError("canary-address.invalid unreachable")

    monkeypatch.setattr(sweep_meters, "make_meter_provider", refuse)

    report_pass(PLANTED)

    assert warnings_of(caplog) == [
        "the sweep's metrics were not sent: no provider (OSError)"
    ]
    assert "canary-address" not in caplog.text


def test_a_pass_that_cannot_be_recorded_is_one_warning_and_the_shutdown_still_runs(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    spy = use(monkeypatch, Spy())

    def refuse(*_: object, **__: object) -> None:
        raise ValueError("not an allowed metric attribute")

    monkeypatch.setattr(sweep_meters, "record_pass", refuse)

    report_pass(PLANTED)

    assert warnings_of(caplog) == [
        "the sweep's metrics were not sent: the pass could not be recorded (ValueError)"
    ]
    assert "shutdown" in spy.calls


class Recording(MetricExporter):
    """An exporter that keeps what a flush hands it, behind the real reader."""

    def __init__(self) -> None:
        super().__init__()
        self.batches: list[MetricsData] = []

    def export(
        self,
        metrics_data: MetricsData,
        timeout_millis: float = 10_000,
        **kwargs: object,
    ) -> MetricExportResult:
        self.batches.append(metrics_data)
        return MetricExportResult.SUCCESS

    def force_flush(self, timeout_millis: float = 10_000) -> bool:
        return True

    def shutdown(self, timeout_millis: float = 30_000, **kwargs: object) -> None:
        return None


def exported_findings(exporter: Recording) -> list[dict[str, float]]:
    return [
        {
            p.attributes["meridian.finding"]: p.value
            for r in batch.resource_metrics
            for s in r.scope_metrics
            for m in s.metrics
            if m.name == SERIES
            for p in m.data.data_points
        }
        for batch in exporter.batches
    ]


def test_a_real_periodic_reader_exports_the_pass_when_the_sweep_flushes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exporter = Recording()
    # An interval of a day: only the flush and the shutdown export.
    provider = MeterProvider(
        metric_readers=[
            PeriodicExportingMetricReader(exporter, export_interval_millis=86_400_000)
        ]
    )
    monkeypatch.setattr(sweep_meters, "make_meter_provider", lambda _name: provider)

    report_pass(PLANTED)

    assert exported_findings(exporter)
    assert all(batch == FINDING_WORDS for batch in exported_findings(exporter))


class Refusing(Recording):
    """An exporter whose collector refuses every batch, as the OTLP exporter
    reports it: a ``FAILURE`` result, not an exception."""

    def export(
        self,
        metrics_data: MetricsData,
        timeout_millis: float = 10_000,
        **kwargs: object,
    ) -> MetricExportResult:
        super().export(metrics_data, timeout_millis, **kwargs)
        return MetricExportResult.FAILURE


def test_an_export_the_collector_refuses_is_not_the_sweeps_warning_and_raises_nothing(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # What the SDK does with a refused export: the reader ignores the result, so
    # the flush returns as if it had gone well and the sweep has nothing to warn
    # of. The exporter's own logger says it (the real one, at ERROR); the sweep's
    # exit code and summary line are untouched, which the tests of main hold.
    exporter = Refusing()
    provider = MeterProvider(
        metric_readers=[
            PeriodicExportingMetricReader(exporter, export_interval_millis=86_400_000)
        ]
    )
    monkeypatch.setattr(sweep_meters, "make_meter_provider", lambda _name: provider)

    report_pass(PLANTED)

    assert exported_findings(exporter)
    assert warnings_of(caplog) == []


def test_without_the_collectors_address_no_exporter_is_built_and_nothing_fails(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_METRICS_ENDPOINT", raising=False)
    built: list[object] = []
    monkeypatch.setattr(
        common_metrics, "OTLPMetricExporter", lambda *a, **k: built.append((a, k))
    )

    report_pass(PLANTED)

    assert built == []
    assert warnings_of(caplog) == []


# ── main ────────────────────────────────────────────────────────────────────
def planted_pass(db: DatabaseHandle) -> None:
    add_claim(db, "CLM-5001", "submitted", age_seconds=TRIAGE_LEASE_SECONDS + MINUTE)


def test_main_reports_the_pass_it_ran_before_it_returns_and_exits_as_before(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    planted_pass(fresh_database)
    spy = use(monkeypatch, Spy())

    code = main(environ_of(fresh_database))

    assert code == 0
    assert spy.calls == ["flush", "shutdown"]
    assert spy.flushed == {
        "documents-overdue": 0,
        "triage-not-started": 1,
        "triage-abandoned": 0,
        "runs-ended": 0,
        "threads-cleaned": 0,
        "failures": 0,
    }


@pytest.mark.parametrize(
    "options",
    [
        pytest.param({"flush_raises": True}, id="flush-raises"),
        pytest.param({"shutdown_raises": True}, id="shutdown-raises"),
    ],
)
def test_a_flush_that_fails_changes_neither_the_exit_code_nor_the_summary_line(
    fresh_database: DatabaseHandle,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    options: dict[str, bool],
) -> None:
    planted_pass(fresh_database)
    use(monkeypatch, Spy(**options))
    caplog.set_level(logging.INFO)

    code = main(environ_of(fresh_database))

    assert code == 0
    assert [r.getMessage() for r in caplog.records if r.name == LOGGER] == [
        "sweep pass: 0 claims referred as overdue, 1 claims failed as not started, "
        "0 claims failed as abandoned, 0 runs ended, 0 threads cleaned, 0 failures"
    ]
    assert len(warnings_of(caplog)) == 1


def an_import_that_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    # A ``None`` in ``sys.modules`` makes the import raise ``ImportError``.
    monkeypatch.setitem(sys.modules, sweep_meters.__name__, None)


def a_call_that_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def explode(result: PassResult) -> None:
        raise RuntimeError(CANARY)

    monkeypatch.setattr(sweep_meters, "report_pass", explode)


@pytest.mark.parametrize(
    ("break_reporting", "word"),
    [
        pytest.param(an_import_that_fails, "ModuleNotFoundError", id="import-fails"),
        pytest.param(a_call_that_raises, "RuntimeError", id="call-raises"),
    ],
)
def test_reporting_that_breaks_is_one_warning_with_a_class_and_the_pass_stands(
    fresh_database: DatabaseHandle,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    break_reporting: Callable[[pytest.MonkeyPatch], None],
    word: str,
) -> None:
    planted_pass(fresh_database)
    break_reporting(monkeypatch)
    caplog.set_level(logging.INFO)

    code = main(environ_of(fresh_database))

    assert code == 0
    messages = [r.getMessage() for r in caplog.records if r.name == LOGGER]
    assert messages[-2:] == [
        "sweep pass: 0 claims referred as overdue, 1 claims failed as not started, "
        "0 claims failed as abandoned, 0 runs ended, 0 threads cleaned, 0 failures",
        f"the sweep's metrics were not sent: {word}",
    ]
    assert CANARY not in caplog.text


@pytest.mark.parametrize(
    "break_reporting",
    [an_import_that_fails, a_call_that_raises],
    ids=["import", "call"],
)
def test_reporting_that_breaks_leaves_the_exit_code_of_a_pass_with_a_failed_item(
    fresh_database: DatabaseHandle,
    monkeypatch: pytest.MonkeyPatch,
    break_reporting: Callable[[pytest.MonkeyPatch], None],
) -> None:
    planted_pass(fresh_database)
    monkeypatch.setattr(
        sweep, "move_claim", breaking(sweep.move_claim, "CLM-5001", "claim_id")
    )
    break_reporting(monkeypatch)

    assert main(environ_of(fresh_database)) == 1


def test_a_pass_with_a_failed_item_is_reported_and_still_exits_one(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    planted_pass(fresh_database)
    monkeypatch.setattr(
        sweep, "move_claim", breaking(sweep.move_claim, "CLM-5001", "claim_id")
    )
    spy = use(monkeypatch, Spy(flush_raises=True))

    code = main(environ_of(fresh_database))

    assert code == 1
    assert spy.flushed["failures"] == 1


def test_a_pass_that_could_not_run_reports_nothing_and_exits_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spy = use(monkeypatch, Spy())

    code = main({DATABASE_URL_ENV: "postgresql://claims_sweep@127.0.0.1:1/x"})

    assert code == 1
    assert spy.calls == []


def test_a_missing_setting_reports_nothing_and_exits_two(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spy = use(monkeypatch, Spy())

    assert main({}) == 2
    assert spy.calls == []


def test_a_provider_that_cannot_be_built_leaves_the_exit_code_of_the_pass(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    planted_pass(fresh_database)

    def refuse(_name: str) -> MeterProvider:
        raise SettingsError("OTEL_EXPORTER_OTLP_CERTIFICATE must name the CA file")

    monkeypatch.setattr(sweep_meters, "make_meter_provider", refuse)

    assert main(environ_of(fresh_database)) == 0


def test_the_sweep_sets_no_global_meter_provider(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    planted_pass(fresh_database)
    calls: list[str] = []
    monkeypatch.setattr(
        otel_metrics, "set_meter_provider", lambda provider: calls.append("meter")
    )
    use(monkeypatch, Spy())

    main(environ_of(fresh_database))

    assert calls == []


def test_without_the_collectors_address_main_sends_nothing(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    planted_pass(fresh_database)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_METRICS_ENDPOINT", raising=False)
    built: list[object] = []
    monkeypatch.setattr(
        common_metrics, "OTLPMetricExporter", lambda *a, **k: built.append((a, k))
    )

    code = main(environ_of(fresh_database))

    assert code == 0
    assert built == []
