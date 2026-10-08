"""S071 L1: a paid run carries its own ceiling, which the gateway holds.

``registry_with_ceilings`` copies the registry and lowers the monthly cost
budget of the tenants a run charges; a live run builds its gateway from that
copy (``record_run(..., registry_dir=...)``). The tests show the copy is exact
and refuses what it must, and that a run through the real stack, with a fake
provider behind the gateway that reports large token counts, is refused by the
gateway under a ceiling and is not refused under the committed budgets.

Nothing here calls a model or reaches a network, and nothing writes under
``config/`` or ``data/``: copies go under ``tmp_path``, and every test checks
the SHA-256 of every file under both directories before and after.
"""

import difflib
import hashlib
import json
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import pytest
from dbsupport import DatabaseHandle
from evalsupport import EVALUATION_DIR, record_run, recording_problems
from runceilingsupport import CeilingError, registry_with_ceilings
from servicesupport import REGISTRY_DIR, REPO_ROOT, FakeClock, owner_rows
from stacksupport import CLAIMS, claims_that_ask_the_model, golden_answer

from meridian.platform.gateway.budget import estimate_input_tokens
from meridian.platform.gateway.models import MAX_OUTPUT_TOKENS, ChatRequest
from meridian.platform.gateway.operations import INPUT_TOKEN_FACTOR
from meridian.platform.gateway.providers.base import (
    EmbeddingReply,
    ProviderError,
    ProviderReply,
)
from meridian.platform.guardrails import holds_special_category
from meridian.platform.registry import load_registry

CLAIMS_TRIAGE = "claims-triage"
EVALUATION = "evaluation"
COST_FIELD = "cost_per_month_eur"
FEW_CENTS = Decimal("0.05")
MICRO = 1_000_000
# The most a reply may report: the gateway takes an input count up to
# ``INPUT_TOKEN_FACTOR`` times its own estimate of the request and an output
# count up to the wire's cap, and treats anything above as a bad response
# (``operations._bounded``). A fake that reports exactly that is the largest
# honest-looking answer: on gpt-4o's prices (USD 3.025 in and 12.10 out per
# million tokens, 1.1355 USD to the euro) one such call costs about EUR 0.02,
# against a reservation of under EUR 0.01, so a few cents are spent in a few
# calls and the one that crosses the ceiling is admitted by its reservation
# and settles above it.
# The claims the rules would send to the model and whose description holds
# special-category data: no call is made for them (S047).
WITHHELD = frozenset(
    c
    for c in claims_that_ask_the_model()
    if holds_special_category(CLAIMS[c]["description"])
)
GUARDED_DIRECTORIES = (REGISTRY_DIR, EVALUATION_DIR)


def digests(*directories: Path) -> dict[str, str]:
    """The SHA-256 of every file under each directory, by path from the
    repository root."""
    return {
        str(path.relative_to(REPO_ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
        for directory in directories
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


@pytest.fixture(autouse=True)
def committed_files_stay_as_they_are() -> Iterator[None]:
    """Every test: the files of ``config/registry`` and ``data/evaluation`` are
    byte for byte the same afterwards (and there is something to compare)."""
    before = digests(*GUARDED_DIRECTORIES)
    assert any(name.endswith("tenants.yaml") for name in before)
    yield
    assert digests(*GUARDED_DIRECTORIES) == before


def budget_of(registry, tenant: str) -> Decimal:
    (found,) = [t for t in registry.tenants if t.id == tenant]
    return found.limits.cost_per_month_eur


# ── the copy ────────────────────────────────────────────────────────────────
def test_the_copy_differs_from_the_source_in_the_named_tenants_one_field(
    tmp_path: Path,
) -> None:
    target = registry_with_ceilings(
        REGISTRY_DIR,
        tmp_path / "registry",
        {CLAIMS_TRIAGE: Decimal("0.50"), EVALUATION: Decimal("0.07")},
    )

    source_files = {p.relative_to(REGISTRY_DIR) for p in REGISTRY_DIR.rglob("*")}
    assert {p.relative_to(target) for p in target.rglob("*")} == source_files
    changed = [
        path
        for path in sorted(source_files)
        if (REGISTRY_DIR / path).is_file()
        and (REGISTRY_DIR / path).read_bytes() != (target / path).read_bytes()
    ]
    assert changed == [Path("tenants.yaml")]
    was = (REGISTRY_DIR / "tenants.yaml").read_text(encoding="utf-8").splitlines()
    now = (target / "tenants.yaml").read_text(encoding="utf-8").splitlines()
    edits = [line for line in difflib.ndiff(was, now) if line[:1] in "+-"]
    assert edits == [
        f"-       {COST_FIELD}: 10",
        f"+       {COST_FIELD}: 0.50",
        f"-       {COST_FIELD}: 5",
        f"+       {COST_FIELD}: 0.07",
    ]
    copy = load_registry(target)
    committed = load_registry(REGISTRY_DIR)
    assert budget_of(copy, CLAIMS_TRIAGE) == Decimal("0.50")
    assert budget_of(copy, EVALUATION) == Decimal("0.07")
    assert budget_of(copy, "development") == budget_of(committed, "development")
    assert copy.tenants[0].limits.tokens_per_day == (
        committed.tenants[0].limits.tokens_per_day
    )


def test_a_tenant_that_is_not_named_keeps_its_budget(tmp_path: Path) -> None:
    target = registry_with_ceilings(
        REGISTRY_DIR, tmp_path / "registry", {EVALUATION: Decimal("1.00")}
    )

    copy, committed = load_registry(target), load_registry(REGISTRY_DIR)
    assert budget_of(copy, EVALUATION) == Decimal("1.00")
    for tenant in (CLAIMS_TRIAGE, "development"):
        assert budget_of(copy, tenant) == budget_of(committed, tenant)


def test_a_ceiling_may_equal_the_committed_budget_and_may_be_one_micro_euro(
    tmp_path: Path,
) -> None:
    committed = budget_of(load_registry(REGISTRY_DIR), CLAIMS_TRIAGE)
    same = registry_with_ceilings(
        REGISTRY_DIR, tmp_path / "same", {CLAIMS_TRIAGE: committed}
    )
    least = registry_with_ceilings(
        REGISTRY_DIR, tmp_path / "least", {CLAIMS_TRIAGE: Decimal("0.000001")}
    )

    assert budget_of(load_registry(same), CLAIMS_TRIAGE) == committed
    assert budget_of(load_registry(least), CLAIMS_TRIAGE) == Decimal("0.000001")


# ── the refusals ────────────────────────────────────────────────────────────
def refused(tmp_path: Path, ceilings: dict[str, Decimal], **options) -> CeilingError:
    """The error ``registry_with_ceilings`` raises, and a check that it left
    no copy behind."""
    target = options.pop("target", tmp_path / "registry")
    existed = target.exists()
    with pytest.raises(CeilingError) as raised:
        registry_with_ceilings(REGISTRY_DIR, target, ceilings, **options)
    assert target.exists() == existed
    return raised.value


def test_a_tenant_the_file_does_not_hold_is_refused_by_name(tmp_path: Path) -> None:
    error = refused(tmp_path, {CLAIMS_TRIAGE: FEW_CENTS, "no-such-tenant": FEW_CENTS})

    assert "no-such-tenant" in str(error)


def test_a_ceiling_above_the_committed_budget_is_refused_without_its_value(
    tmp_path: Path,
) -> None:
    error = refused(tmp_path, {EVALUATION: Decimal("12345.678901")})

    assert EVALUATION in str(error)
    assert "12345" not in str(error)
    # One micro-euro over the committed EUR 5 is over; the budget is the bound.
    refused(tmp_path, {EVALUATION: Decimal("5.000001")})


@pytest.mark.parametrize("ceiling", [Decimal(0), Decimal("-0.123456")])
def test_a_ceiling_of_zero_or_less_is_refused_without_its_value(
    tmp_path: Path, ceiling: Decimal
) -> None:
    error = refused(tmp_path, {CLAIMS_TRIAGE: ceiling})

    assert CLAIMS_TRIAGE in str(error)
    assert "0.123456" not in str(error)


@pytest.mark.parametrize(
    "ceiling", [Decimal("0.0000001"), Decimal("NaN"), Decimal("Infinity")]
)
def test_a_ceiling_the_ledger_cannot_count_is_refused(
    tmp_path: Path, ceiling: Decimal
) -> None:
    """The ledger counts micro-euros: a seventh decimal would not convert
    exactly, and a value that is not a number is no bound."""
    error = refused(tmp_path, {CLAIMS_TRIAGE: ceiling})

    assert CLAIMS_TRIAGE in str(error)


def test_one_bad_ceiling_among_good_ones_writes_nothing(tmp_path: Path) -> None:
    refused(tmp_path, {CLAIMS_TRIAGE: FEW_CENTS, EVALUATION: Decimal(0)})


def test_no_ceiling_at_all_is_refused(tmp_path: Path) -> None:
    refused(tmp_path, {})


def test_a_target_inside_the_repositorys_config_is_refused(tmp_path: Path) -> None:
    config = REPO_ROOT / "config"
    refused(tmp_path, {CLAIMS_TRIAGE: FEW_CENTS}, target=config / "registry-copy")
    refused(tmp_path, {CLAIMS_TRIAGE: FEW_CENTS}, target=config)
    # The same place reached through a link.
    link = tmp_path / "link"
    link.symlink_to(config, target_is_directory=True)
    through_link = refused(tmp_path, {CLAIMS_TRIAGE: FEW_CENTS}, target=link / "copy")
    assert "config" in str(through_link)
    assert not (config / "copy").exists()
    assert not (config / "registry-copy").exists()


def test_a_target_that_already_holds_files_is_refused(tmp_path: Path) -> None:
    """The source itself is the case that matters: a copy written over it would
    change a fingerprint."""
    target = tmp_path / "registry"
    target.mkdir()
    (target / "stray.txt").write_text("not a registry", encoding="utf-8")

    with pytest.raises(CeilingError):
        registry_with_ceilings(REGISTRY_DIR, target, {CLAIMS_TRIAGE: FEW_CENTS})
    assert sorted(p.name for p in target.iterdir()) == ["stray.txt"]
    with pytest.raises(CeilingError):
        registry_with_ceilings(REGISTRY_DIR, REGISTRY_DIR, {CLAIMS_TRIAGE: FEW_CENTS})


def test_an_empty_directory_is_a_target(tmp_path: Path) -> None:
    """``tmp_path`` itself is where a test would most naturally write."""
    target = registry_with_ceilings(REGISTRY_DIR, tmp_path, {CLAIMS_TRIAGE: FEW_CENTS})

    assert budget_of(load_registry(target), CLAIMS_TRIAGE) == FEW_CENTS


# ── the gateway holds it ────────────────────────────────────────────────────
class LargeModel:
    """The provider behind the recording gateway: it finds the claim from the
    description in the request, answers as the oracle's model does and reports
    the largest token counts the gateway accepts for every call, the judge's
    included. ``asked`` is what got past the gateway's checks."""

    def __init__(self) -> None:
        self.asked: list[str] = []
        self.judged = 0

    def chat(self, deployment, request: ChatRequest, *, timeout_seconds: float):
        document = json.loads(request.messages[1].content)
        if "statement" in document:  # the judge's question
            self.judged += 1
            text = json.dumps({"grounded": True, "reason": "The source states it."})
        else:
            (claim_id,) = [
                c
                for c, v in CLAIMS.items()
                if v["description"] == document["description"]
            ]
            self.asked.append(claim_id)
            text = golden_answer(claim_id)
        return ProviderReply(
            text=text,
            finish_reason="stop",
            model="fake-model",
            input_tokens=INPUT_TOKEN_FACTOR * estimate_input_tokens(request),
            output_tokens=MAX_OUTPUT_TOKENS,
        )

    def embed(self, deployment, request, *, timeout_seconds: float) -> EmbeddingReply:
        raise ProviderError("unavailable")


def refusals(db: DatabaseHandle) -> list[tuple]:
    """The gateway's refusal rows: event, outcome, tenant and reason."""
    return owner_rows(
        db,
        "SELECT event, outcome, tenant, reason FROM audit.events "
        "WHERE service = 'model-gateway' AND outcome = 'refused' "
        "ORDER BY recorded_at",
    )


def settled_charges(db: DatabaseHandle, tenant: str) -> list[int]:
    """The charge in micro-EUR of each settled call of a tenant on Azure, oldest
    first (the replay gateway's embeddings of the ingestion cost nothing)."""
    rows = owner_rows(
        db,
        "SELECT charged_micro_eur FROM gateway.usage "
        "WHERE tenant = %s AND state = 'settled' AND provider = 'azure-openai' "
        "ORDER BY reserved_at, attempt_id",
        (tenant,),
    )
    return [int(charge) for (charge,) in rows]


def run_with(db: DatabaseHandle, fake: LargeModel, **options):
    clock = FakeClock()
    return record_run(db, inner=fake, pace=clock.advance, clock=clock, **options)


def test_the_gateway_refuses_a_run_that_goes_past_its_ceiling(
    fresh_database: DatabaseHandle, tmp_path: Path
) -> None:
    registry_dir = registry_with_ceilings(
        REGISTRY_DIR,
        tmp_path / "registry",
        {CLAIMS_TRIAGE: FEW_CENTS, EVALUATION: FEW_CENTS},
    )
    fake = LargeModel()

    run = run_with(fresh_database, fake, registry_dir=registry_dir)

    asking = claims_that_ask_the_model()
    assert 0 < len(fake.asked) < len(asking - WITHHELD)
    charges = settled_charges(fresh_database, CLAIMS_TRIAGE)
    assert len(charges) == len(fake.asked)
    # Every call but the last was admitted with the counter under the ceiling;
    # the last one settled past it, and the gateway admitted nothing after it:
    # the provider was never asked again.
    assert sum(charges[:-1]) <= FEW_CENTS * MICRO < sum(charges)
    rows = refusals(fresh_database)
    # The refusal as the gateway names it: a model.call row, refused, with the
    # reason of the monthly cost budget (answered 429, the run's leg fails).
    assert ("model.call", "refused", CLAIMS_TRIAGE, "tenant-cost-budget") in rows
    assert {row[:2] + row[3:] for row in rows} == {
        ("model.call", "refused", "tenant-cost-budget")
    }
    # A refused call leaves nothing reserved behind, so the run is reported
    # incomplete by the claims that have no proposal, not by an unsettled call.
    assert run.unsettled == 0
    without_proposal = [c for c, p in run.evaluation.proposals.items() if p is None]
    assert len(without_proposal) >= len(asking - WITHHELD) - len(fake.asked) > 0
    problems = recording_problems(run)
    assert len(problems) == 1
    assert problems[0].startswith(f"{len(without_proposal)} claims have no proposal")
    # The recording holds what was answered and nothing else.
    assert len(run.recording.entries) == run.chat_calls == len(fake.asked) + fake.judged


def test_without_the_ceiling_the_same_run_is_not_refused(
    fresh_database: DatabaseHandle,
) -> None:
    """The control: the committed budgets (EUR 10 and 5) let the same fault go
    by, which is why a paid run carries a ceiling of its own."""
    fake = LargeModel()

    run = run_with(fresh_database, fake)

    assert refusals(fresh_database) == []
    assert recording_problems(run) == []
    assert len(fake.asked) == len(set(fake.asked))
    assert set(fake.asked) == claims_that_ask_the_model() - WITHHELD
    assert sum(settled_charges(fresh_database, CLAIMS_TRIAGE)) > FEW_CENTS * MICRO
