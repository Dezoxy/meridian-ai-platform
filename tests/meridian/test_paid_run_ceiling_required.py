"""S071 L3, B-1: a paid run cannot start without its ceiling by omission.

With the real provider (``inner`` is None) ``live_recording_gateway``, and so
``record_run`` and ``record_injection_run``, refuse before they read an
environment variable or build anything, unless the registry directory they are
given is not the committed one and every tenant the run charges holds a budget
at or below the ceiling the caller names. With a fake provider the default stays
as it was.

No database, no network and no file under ``config/`` or ``data/``: the gateway
is never built here. The test that lets a proper copy through stops it at the
provider's constructor with a marker exception, and asserts the refusal did not
come first.
"""

import shutil
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import evalsupport
import pytest
from evalsupport import live_recording_gateway, record_run
from injectionrecordsupport import _run_registry, record_injection_run
from runceilingsupport import (
    GOLDEN_RUN_CEILING,
    INJECTION_RUN_CEILING,
    CeilingError,
    RunCeiling,
    golden_run_registry,
    registry_with_ceilings,
    require_run_ceiling,
)
from servicesupport import REGISTRY_DIR

from meridian.platform.gateway.settings import ENDPOINTS_ENV, TENANT_ID_ENV

CLAIMS_TRIAGE = "claims-triage"
EVALUATION = "evaluation"
CEILING = RunCeiling(Decimal("0.50"), (CLAIMS_TRIAGE, EVALUATION))
# A handle the gateway is never built from: the refusal comes before ``dsn`` is
# called, and the marker exception comes before the app is made.
NO_DATABASE = SimpleNamespace(dsn=lambda role: "postgresql://postgres@127.0.0.1:1/none")


class ProviderBuilt(Exception):
    """The real provider's constructor was reached: the refusal did not stop it."""


@pytest.fixture
def azure_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENDPOINTS_ENV, "{}")
    monkeypatch.setenv(TENANT_ID_ENV, "00000000-0000-0000-0000-000000000000")


@pytest.fixture
def provider_constructor_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def raise_marker(settings):
        raise ProviderBuilt

    monkeypatch.setattr(evalsupport, "_azure_provider", raise_marker)


@pytest.fixture
def no_azure_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neither variable is set, so a gateway that read the environment before
    refusing would fail with a ``KeyError``, not with the refusal."""
    monkeypatch.delenv(ENDPOINTS_ENV, raising=False)
    monkeypatch.delenv(TENANT_ID_ENV, raising=False)


def proper_copy(tmp_path: Path, name: str = "registry") -> Path:
    return registry_with_ceilings(
        REGISTRY_DIR, tmp_path / name, {t: CEILING.eur for t in CEILING.tenants}
    )


# ── the refusals ────────────────────────────────────────────────────────────
@pytest.mark.usefixtures("no_azure_variables")
def test_the_real_provider_with_the_default_directory_is_refused_by_name() -> None:
    with pytest.raises(CeilingError, match=r"pass ceiling=.*registry_dir"):
        live_recording_gateway(NO_DATABASE)  # type: ignore[arg-type]


@pytest.mark.usefixtures("no_azure_variables")
def test_the_real_provider_with_the_default_directory_and_a_ceiling_is_refused() -> (
    None
):
    with pytest.raises(CeilingError, match="committed registry"):
        live_recording_gateway(NO_DATABASE, ceiling=CEILING)  # type: ignore[arg-type]


@pytest.mark.usefixtures("no_azure_variables")
def test_the_real_provider_with_the_committed_directory_passed_is_refused() -> None:
    with pytest.raises(CeilingError, match="committed registry"):
        live_recording_gateway(  # type: ignore[arg-type]
            NO_DATABASE, registry_dir=REGISTRY_DIR, ceiling=CEILING
        )


@pytest.mark.usefixtures("no_azure_variables")
def test_the_committed_directory_reached_by_another_path_is_refused(
    tmp_path: Path,
) -> None:
    link = tmp_path / "link"
    link.symlink_to(REGISTRY_DIR, target_is_directory=True)

    with pytest.raises(CeilingError, match="committed registry"):
        live_recording_gateway(  # type: ignore[arg-type]
            NO_DATABASE, registry_dir=link, ceiling=CEILING
        )


@pytest.mark.usefixtures("no_azure_variables")
def test_a_copy_whose_budget_is_above_the_ceiling_is_refused_naming_the_tenant(
    tmp_path: Path,
) -> None:
    copy = proper_copy(tmp_path)
    lower = RunCeiling(Decimal("0.10"), (CLAIMS_TRIAGE,))

    with pytest.raises(CeilingError, match=CLAIMS_TRIAGE) as raised:
        live_recording_gateway(  # type: ignore[arg-type]
            NO_DATABASE, registry_dir=copy, ceiling=lower
        )
    assert "0.50" not in str(raised.value)


@pytest.mark.usefixtures("no_azure_variables")
def test_a_copy_that_lowers_one_tenant_but_not_the_other_is_refused(
    tmp_path: Path,
) -> None:
    only_one = registry_with_ceilings(
        REGISTRY_DIR, tmp_path / "registry", {CLAIMS_TRIAGE: CEILING.eur}
    )

    with pytest.raises(CeilingError, match=EVALUATION):
        live_recording_gateway(  # type: ignore[arg-type]
            NO_DATABASE, registry_dir=only_one, ceiling=CEILING
        )


@pytest.mark.usefixtures("no_azure_variables")
def test_a_ceiling_that_names_a_tenant_the_registry_lacks_is_refused(
    tmp_path: Path,
) -> None:
    copy = proper_copy(tmp_path)
    unknown = RunCeiling(CEILING.eur, ("no-such-tenant",))

    with pytest.raises(CeilingError, match="no-such-tenant"):
        live_recording_gateway(  # type: ignore[arg-type]
            NO_DATABASE, registry_dir=copy, ceiling=unknown
        )


@pytest.mark.parametrize(
    "ceiling",
    [RunCeiling(Decimal("0.50"), ()), RunCeiling(Decimal(0), (CLAIMS_TRIAGE,))],
    ids=["no tenant", "no amount"],
)
@pytest.mark.usefixtures("no_azure_variables")
def test_a_ceiling_that_bounds_nothing_is_refused(
    tmp_path: Path, ceiling: RunCeiling
) -> None:
    with pytest.raises(CeilingError, match="no tenant or no amount"):
        live_recording_gateway(  # type: ignore[arg-type]
            NO_DATABASE, registry_dir=proper_copy(tmp_path), ceiling=ceiling
        )


@pytest.mark.usefixtures("no_azure_variables")
def test_the_two_paid_runs_are_refused_by_the_same_rule() -> None:
    with pytest.raises(CeilingError, match="pass ceiling="):
        record_run(NO_DATABASE)  # type: ignore[arg-type]
    with pytest.raises(CeilingError, match="committed registry"):
        record_injection_run(  # type: ignore[arg-type]
            NO_DATABASE, registry_dir=REGISTRY_DIR
        )


# ── what goes on ────────────────────────────────────────────────────────────
@pytest.mark.usefixtures("azure_variables", "provider_constructor_raises")
def test_a_proper_copy_goes_on_to_the_provider_and_the_refusal_did_not_come_first(
    tmp_path: Path,
) -> None:
    copy = proper_copy(tmp_path)

    with pytest.raises(ProviderBuilt):
        live_recording_gateway(  # type: ignore[arg-type]
            NO_DATABASE, registry_dir=copy, ceiling=CEILING
        )
    with pytest.raises(ProviderBuilt):
        record_run(NO_DATABASE, registry_dir=copy, ceiling=CEILING)  # type: ignore[arg-type]


@pytest.mark.usefixtures("azure_variables", "provider_constructor_raises")
def test_the_injection_run_with_the_real_provider_builds_its_own_capped_copy() -> None:
    """Its default directory is the capped copy, so the injection run reaches the
    provider's constructor with no argument at all."""
    with pytest.raises(ProviderBuilt):
        record_injection_run(NO_DATABASE)  # type: ignore[arg-type]


def test_what_the_paid_runs_build_passes_their_own_ceiling_check(
    tmp_path: Path,
) -> None:
    """The golden runs' directory and the injection run's default are what the
    check accepts, with the constants the runs name."""

    require_run_ceiling(golden_run_registry(tmp_path), GOLDEN_RUN_CEILING)
    with _run_registry(None) as directory:
        require_run_ceiling(directory, INJECTION_RUN_CEILING)
    assert INJECTION_RUN_CEILING.eur == Decimal("0.50")


def test_the_committed_registry_is_not_a_paid_runs_registry() -> None:
    for ceiling in (GOLDEN_RUN_CEILING, INJECTION_RUN_CEILING):
        with pytest.raises(CeilingError, match="committed registry"):
            require_run_ceiling(REGISTRY_DIR, ceiling)


# ── the symlinked tenants file ──────────────────────────────────────────────
def test_a_source_whose_tenants_file_is_a_link_is_refused(tmp_path: Path) -> None:
    """The copy keeps links, and the edit would write through one."""
    source = tmp_path / "source"
    shutil.copytree(REGISTRY_DIR, source)
    real = tmp_path / "elsewhere.yaml"
    shutil.move(source / "tenants.yaml", real)
    (source / "tenants.yaml").symlink_to(real)
    before = real.read_bytes()

    with pytest.raises(CeilingError, match="link"):
        registry_with_ceilings(
            source, tmp_path / "target", {CLAIMS_TRIAGE: CEILING.eur}
        )
    assert real.read_bytes() == before
    assert not (tmp_path / "target").exists()
