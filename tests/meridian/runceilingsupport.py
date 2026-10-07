"""A copy of the registry whose tenants have a lower monthly budget (S071).

A paid evaluation run builds its gateway from the copy, so the Model Gateway,
not the session, refuses the run that goes past its ceiling: the gateway
checks a call's reservation against the tenant's ``cost_per_month_eur``
(``gateway/budget.py``) and answers 429 once the month's counter would pass
it. The ledger of a run lives in a database dropped afterwards, so the ceiling
bounds that run and no other.

The copy keeps every byte of every file but one line per named tenant: the
line is rewritten in place, never loaded and dumped, so comments and the
schema header stay. A ceiling may only lower the budget the committed file
holds; the committed registry is never written. Errors name the tenant and no
value from the file.
"""

import re
import shutil
from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path

from servicesupport import REPO_ROOT

from meridian.platform.registry import load_registry
from meridian.platform.registry.loader import RegistryError

TENANTS_FILE = "tenants.yaml"
CONFIG_DIR = REPO_ROOT / "config"
# The ledger counts micro-euros, so a budget has at most six decimals.
LEDGER_PLACES = 6


class CeilingError(ValueError):
    """A ceiling or a target ``registry_with_ceilings`` refuses. The message
    names a tenant or a path, never a value from the registry."""


def registry_with_ceilings(
    source: Path, target: Path, ceilings: Mapping[str, Decimal]
) -> Path:
    """Copy the registry directory ``source`` to ``target`` and set the monthly
    cost budget, in euros, of each tenant in ``ceilings`` to its ceiling.

    Refused, before anything is written: no ceiling at all; a tenant the file
    does not hold; a ceiling above that tenant's committed budget, of zero or
    less, not a finite number, or with more than six decimals; a ``target``
    inside the repository's ``config/``, inside ``source``, or holding files.
    The copy must load (``load_registry``, which ``meridian registry validate``
    runs). Returns ``target``."""
    if not ceilings:
        raise CeilingError("no ceiling was named: a run without one has no bound")
    committed = _committed_budgets(source)
    for tenant, ceiling in ceilings.items():
        _check_ceiling(tenant, ceiling, committed)
    _check_target(source, target)
    shutil.copytree(source, target, symlinks=True, dirs_exist_ok=True)
    path = target / TENANTS_FILE
    text = path.read_bytes().decode("utf-8")
    for tenant, ceiling in ceilings.items():
        text = _with_budget(text, tenant, ceiling)
    path.write_bytes(text.encode("utf-8"))
    _check_copy(target, committed, ceilings)
    return target


def _committed_budgets(source: Path) -> dict[str, Decimal]:
    try:
        registry = load_registry(source)
    except RegistryError:
        raise CeilingError("the source registry does not load") from None
    return {t.id: t.limits.cost_per_month_eur for t in registry.tenants}


def _check_ceiling(
    tenant: str, ceiling: Decimal, committed: Mapping[str, Decimal]
) -> None:
    if tenant not in committed:
        raise CeilingError(f"tenant {tenant!r} is not in the registry")
    if not ceiling.is_finite():
        raise CeilingError(f"the ceiling for tenant {tenant!r} is not a number")
    if ceiling <= 0:
        raise CeilingError(f"the ceiling for tenant {tenant!r} is zero or less")
    exponent = ceiling.as_tuple().exponent
    if isinstance(exponent, int) and exponent < -LEDGER_PLACES:
        raise CeilingError(
            f"the ceiling for tenant {tenant!r} has more than {LEDGER_PLACES} decimals"
        )
    if ceiling > committed[tenant]:
        raise CeilingError(
            f"the ceiling for tenant {tenant!r} is above its committed budget: "
            "a run may only lower it"
        )


def _check_target(source: Path, target: Path) -> None:
    where = target.resolve()
    if where.is_relative_to(CONFIG_DIR.resolve()):
        raise CeilingError("the target is inside the repository's config/")
    if where.is_relative_to(source.resolve()):
        raise CeilingError("the target is the source registry or inside it")
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        raise CeilingError("the target exists and is not an empty directory")


def _with_budget(text: str, tenant: str, ceiling: Decimal) -> str:
    """``text`` with the budget line of ``tenant`` rewritten, nothing else: the
    first ``cost_per_month_eur`` after the tenant's ``- id:`` line and before the
    next one."""
    block = re.compile(
        r"(?P<head>^[ \t]*-[ \t]+id:[ \t]*"
        + re.escape(tenant)
        + r"[ \t]*\r?\n(?:(?![ \t]*-[ \t]+id:)[^\n]*\n)*?"
        r"[ \t]*cost_per_month_eur:[ \t]*)(?P<value>[^\s#]+)",
        re.MULTILINE,
    )
    found = block.findall(text)
    if len(found) != 1:
        raise CeilingError(f"the budget line of tenant {tenant!r} was not found once")
    return block.sub(lambda m: m.group("head") + format(ceiling, "f"), text)


def _check_copy(
    target: Path, committed: Mapping[str, Decimal], ceilings: Mapping[str, Decimal]
) -> None:
    """The copy loads, holds each ceiling and every other budget as committed."""
    try:
        registry = load_registry(target)
    except RegistryError:
        raise CeilingError("the registry copy does not load") from None
    expected = {**committed, **ceilings}
    if {t.id: t.limits.cost_per_month_eur for t in registry.tenants} != expected:
        raise CeilingError("the registry copy does not hold the budgets asked for")
