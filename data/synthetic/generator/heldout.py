"""Build the held-out injection cases: 72 sentences from a writer who read none
of the screen (S071).

A case is built by the rule of the injection set's own description cases
(``injection._description_case``): the sentence is appended to the description of
a golden claim, one space between, and the claim's other facts stay unchanged.
The base claims rotate over the claims the injection set's description cases
use: the 48 attacks over its attack bases with ``injection._attack_base``'s
rotation (it moves on by one base after each round of six, so each family of six
meets every base once), the 24 look-alikes over its benign bases in turn. Case
IDs run from CLM-5001 in the writer's order, a range no claim or case uses.

The cases and their manifest go to a folder of their own, beside
``injection/`` and not in it: the injection set's fingerprint refuses a file of
its folder that its manifest does not list, and these cases are a report, not a
fingerprint of the gate. The manifest keeps, per case, the writer's ID, the
language and the family; the writer's note on what each sentence is after is in
no data file.
"""

import hashlib
from typing import Any

from . import GENERATOR_VERSION, WORKLOAD
from .heldout_text import HELDOUT_TEXTS
from .injection import (
    ATTACK,
    BENIGN,
    BENIGN_BASES,
    CASES_FILE,
    _attack_base,
    _description_case,
    _Spec,
)
from .injection_text import APPEND
from .records import Record
from .scenarios import Dataset

FIRST_ID = 5001
HELD_OUT_LABELS = {"attack": ATTACK, "benign": BENIGN}


def _base_claim(label: str, index: int) -> str:
    """The base claim of the sentence at 0-based ``index`` among those of its
    label."""
    if label == ATTACK:
        return _attack_base(index)
    return BENIGN_BASES[index % len(BENIGN_BASES)]


def build_cases(dataset: Dataset) -> list[Record]:
    """One case per sentence, in the writer's order, which is also the order of
    the case IDs."""
    cases = []
    seen = {ATTACK: 0, BENIGN: 0}
    for position, entry in enumerate(HELDOUT_TEXTS):
        label = HELD_OUT_LABELS[entry.label]
        spec = _Spec(entry.family, entry.text, APPEND)
        base = _base_claim(label, seen[label])
        seen[label] += 1
        cases.append(_description_case(dataset, label, FIRST_ID + position, base, spec))
    return cases


def build_manifest(
    cases: list[Record], seed: int, golden_manifest: bytes, cases_bytes: bytes
) -> Record:
    families: dict[str, int] = {}
    for case in cases:
        key = f"{case['label']}/{case['family']}"
        families[key] = families.get(key, 0) + 1
    per_case: dict[str, Any] = {
        case["case"]: {
            "h_id": entry.h_id,
            "language": entry.language,
            "family": entry.family,
        }
        for case, entry in zip(cases, HELDOUT_TEXTS, strict=True)
    }
    return {
        "synthetic": True,
        "held_out": True,
        "workload": WORKLOAD,
        "generator_version": GENERATOR_VERSION,
        "seed": seed,
        "golden_set": hashlib.sha256(golden_manifest).hexdigest(),
        "counts": {
            "cases": len(cases),
            "attack": sum(case["label"] == ATTACK for case in cases),
            "benign": sum(case["label"] == BENIGN for case in cases),
            "description": sum(case["carrier"] == "description" for case in cases),
            "clause": sum(case["carrier"] == "clause" for case in cases),
        },
        "families": families,
        "files": {CASES_FILE: hashlib.sha256(cases_bytes).hexdigest()},
        "cases": per_case,
    }
