"""SIMULATED VARIANT. The same 50 policies, claims, history and wordings, with
a seeded re-linking of who holds what and which asset is which. It exists to
show what the relational questions return when there is something to return.
It is built in memory from the committed files and written to nothing; no
number from it describes the data.

The re-linking is a pure function of the files and the seed. Policies are
ranked by a SHA-256 of the seed and the policy number (no random generator).

- Customers: the first 18 policies in that rank form 8 groups of 3, 3, 2, 2, 2,
  2, 2 and 2. A group's policies take the customer and the address of the
  group's first policy. The other 32 policies keep theirs.
- Assets: of the policies that have a claim or a history entry, in the same
  rank, 4 pairs of vehicle policies and 3 pairs of home policies each insure
  one asset: the second policy of a pair takes the asset of the first. A car
  or a home insured by two policies over the years is then one asset with
  events through two policies.
"""

import hashlib
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .build import Records, build_from, load_records
from .keys import Identity, asset_family, derived_identity
from .model import Graph

VARIANT_SEED = 38
HOLDER_GROUPS = (3, 3, 2, 2, 2, 2, 2, 2)
ASSET_PAIRS = {"vehicle": 4, "home": 3}


def _ranked(seed: int, policies: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    def key(policy: dict[str, Any]) -> str:
        material = f"claimgraph/variant/{seed}/{policy['policy_number']}"
        return hashlib.sha256(material.encode()).hexdigest()

    return sorted(policies, key=key)


def _regroup_holders(
    ranked: list[dict[str, Any]], identities: dict[str, Identity]
) -> None:
    start = 0
    for size in HOLDER_GROUPS:
        group = ranked[start : start + size]
        lead = identities[group[0]["policy_number"]]
        for policy in group[1:]:
            current = identities[policy["policy_number"]]
            identities[policy["policy_number"]] = Identity(
                lead.customer, lead.address, current.asset
            )
        start += size


def _share_assets(seed: int, records: Records, identities: dict[str, Identity]) -> None:
    with_events = {c["policy_number"] for c in records.claims}
    with_events |= {h["policy_number"] for h in records.history}
    eventful = [p for p in records.policies if p["policy_number"] in with_events]
    for family, pairs in ASSET_PAIRS.items():
        ranked = _ranked(seed, [p for p in eventful if asset_family(p) == family])
        firsts = ranked[0 : 2 * pairs : 2]
        seconds = ranked[1 : 2 * pairs : 2]
        for first, second in zip(firsts, seconds, strict=True):
            lead = identities[first["policy_number"]]
            current = identities[second["policy_number"]]
            identities[second["policy_number"]] = Identity(
                current.customer, current.address, lead.asset
            )


def relink(records: Records, seed: int) -> dict[str, Identity]:
    """The customer, address and asset IDs of each policy in the variant."""
    identities = {p["policy_number"]: derived_identity(p) for p in records.policies}
    _regroup_holders(_ranked(seed, records.policies), identities)
    _share_assets(seed, records, identities)
    return identities


def build_variant(data_dir: Path, seed: int = VARIANT_SEED) -> Graph:
    """The simulated variant of the data in ``data_dir``, labelled as such."""
    records = load_records(data_dir)
    return build_from(
        records, relink(records, seed), label=f"simulated variant (seed {seed})"
    )
