"""Build the injection case set: golden claims carrying attack or benign text.

A description case joins a text to the description of a golden claim; a clause
case names a clause of that claim's policy wording and the text a consumer
inserts into the clause body: ``inserted + " "`` immediately before the last
occurrence of ``This exclusion applies to claims for``, so the text sits before
the closing sentence, where a wording is still read as an exclusion and the
model sees it. The base claims rotate, so each case sits on a real golden claim
and the claim's own facts stay unchanged.
"""

import base64
import copy
import hashlib
import json
from collections.abc import Callable
from typing import NamedTuple

from . import GENERATOR_VERSION
from .injection_text import (
    APPEND,
    ATTACKS_AFTER_OBFUSCATED,
    ATTACKS_BEFORE_OBFUSCATED,
    BASE64_PREFIX,
    BENIGN_CASES,
    CLAUSE_ATTACKS,
    HYPHENATED_TEXT,
    LEET_TEXT,
    NAME_MASKED_ATTACKS,
    NAME_MASKED_FAMILY,
    NEWLINE,
    OBFUSCATED_FAMILY,
    PLAIN_SENTENCE,
    PREPEND,
    REVERSED_PREFIX,
    SPACED_TEXT,
)
from .records import Record
from .scenarios import Dataset

ATTACK, BENIGN = "attack", "benign"
CARRIER_DESCRIPTION, CARRIER_CLAUSE = "description", "clause"
ATTACK_BASES = ("CLM-0026", "CLM-0031", "CLM-0037", "CLM-0038", "CLM-0001", "CLM-0034")
BENIGN_BASES = ("CLM-0011", "CLM-0015", "CLM-0023", "CLM-0007", "CLM-0008", "CLM-0035")
CLAUSE_BASES = (
    ("CLM-0026", "3.3"),
    ("CLM-0031", "3.4"),
    ("CLM-0037", "3.2"),
    ("CLM-0038", "3.2"),
)
DESCRIPTION_ATTACK_FIRST_ID = 1001
CLAUSE_ATTACK_FIRST_ID = 2001
BENIGN_FIRST_ID = 3001
CASES_FILE = "cases.json"
ZERO_WIDTH_SPACE = chr(0x200B)
CYRILLIC_O = chr(0x043E)
FULLWIDTH_OFFSET = 0xFEE0
FIRST_PRINTABLE, LAST_PRINTABLE = ord("!"), ord("~")
SPLIT_AFTER = 2
OBFUSCATED_WORDS = ("Ignore", "previous", "instructions")


class _Spec(NamedTuple):
    family: str
    text: str
    mode: str = APPEND
    name: str | None = None


def _join(description: str, spec: _Spec) -> str:
    if spec.mode == PREPEND:
        return f"{spec.text} {description}"
    separator = "\n" if spec.mode == NEWLINE else " "
    return f"{description}{separator}{spec.text}"


def _each_word(sentence: str, change: Callable[[str], str]) -> str:
    for word in OBFUSCATED_WORDS:
        sentence = sentence.replace(word, change(word))
    return sentence


def _fullwidth(sentence: str) -> str:
    return "".join(
        chr(ord(char) + FULLWIDTH_OFFSET)
        if FIRST_PRINTABLE <= ord(char) <= LAST_PRINTABLE
        else char
        for char in sentence
    )


def _obfuscated_texts() -> tuple[str, ...]:
    """Cases 31 to 38; the computed ones derive from ``PLAIN_SENTENCE``."""
    encoded = base64.b64encode(PLAIN_SENTENCE.encode("ascii")).decode("ascii")
    return (
        _each_word(
            PLAIN_SENTENCE,
            lambda word: word[:SPLIT_AFTER] + ZERO_WIDTH_SPACE + word[SPLIT_AFTER:],
        ),
        _fullwidth(PLAIN_SENTENCE),
        LEET_TEXT,
        SPACED_TEXT,
        _each_word(PLAIN_SENTENCE, lambda word: word.replace("o", CYRILLIC_O)),
        HYPHENATED_TEXT,
        BASE64_PREFIX + encoded,
        REVERSED_PREFIX + PLAIN_SENTENCE[::-1],
    )


def _description_attack_specs() -> list[_Spec]:
    specs = [_Spec(s.family, s.text, s.mode) for s in ATTACKS_BEFORE_OBFUSCATED]
    specs += [_Spec(OBFUSCATED_FAMILY, text) for text in _obfuscated_texts()]
    specs += [_Spec(s.family, s.text, s.mode) for s in ATTACKS_AFTER_OBFUSCATED]
    specs += [
        _Spec(NAME_MASKED_FAMILY, s.text, name=s.name) for s in NAME_MASKED_ATTACKS
    ]
    return specs


def _benign_specs() -> list[_Spec]:
    return [_Spec(s.family, s.text, s.mode) for s in BENIGN_CASES]


def _find(dataset: Dataset, claim_id: str) -> Record:
    for claim in dataset.claims:
        if claim["claim_id"] == claim_id:
            return claim
    raise ValueError(f"injection base claim {claim_id} is not in the dataset")


def _policy_of(dataset: Dataset, claim: Record) -> Record:
    for policy in dataset.policies:
        if policy["policy_number"] == claim["policy_number"]:
            return policy
    raise ValueError(
        f"{claim['claim_id']}: policy {claim['policy_number']} is not in the dataset"
    )


def _description_case(
    dataset: Dataset, label: str, number: int, base_id: str, spec: _Spec
) -> Record:
    claim = copy.deepcopy(_find(dataset, base_id))
    case = f"CLM-{number}"
    claim["claim_id"] = case
    claim["description"] = _join(claim["description"], spec)
    if spec.name is not None:
        claim["claimant"]["name"] = spec.name
    return {
        "case": case,
        "label": label,
        "carrier": CARRIER_DESCRIPTION,
        "family": spec.family,
        "base_claim": base_id,
        "claim": claim,
        "clause": None,
    }


def _clause_case(
    dataset: Dataset, number: int, base_id: str, clause: str, family: str, text: str
) -> Record:
    base = _find(dataset, base_id)
    policy = _policy_of(dataset, base)
    claim = copy.deepcopy(base)
    case = f"CLM-{number}"
    claim["claim_id"] = case
    return {
        "case": case,
        "label": ATTACK,
        "carrier": CARRIER_CLAUSE,
        "family": family,
        "base_claim": base_id,
        "claim": claim,
        "clause": {
            "product": policy["product"],
            "wording_version": policy["wording_version"],
            "clause": clause,
            "inserted": text,
        },
    }


def _attack_base(index: int) -> str:
    """The base claim of the description attack at 0-based ``index``. The
    rotation moves on by one base after each round, so it never lines up with a
    family of the same size: a sentence's place in its family does not decide
    its base claim."""
    rounds = len(ATTACK_BASES)
    return ATTACK_BASES[(index + index // rounds) % rounds]


def build_cases(dataset: Dataset) -> list[Record]:
    """Every injection case, sorted by case ID."""
    cases = [
        _description_case(
            dataset,
            ATTACK,
            DESCRIPTION_ATTACK_FIRST_ID + index,
            _attack_base(index),
            spec,
        )
        for index, spec in enumerate(_description_attack_specs())
    ]
    for index, attack in enumerate(CLAUSE_ATTACKS):
        base_id, clause = CLAUSE_BASES[index % len(CLAUSE_BASES)]
        cases.append(
            _clause_case(
                dataset,
                CLAUSE_ATTACK_FIRST_ID + index,
                base_id,
                clause,
                attack.family,
                attack.text,
            )
        )
    cases += [
        _description_case(
            dataset,
            BENIGN,
            BENIGN_FIRST_ID + index,
            BENIGN_BASES[index % len(BENIGN_BASES)],
            spec,
        )
        for index, spec in enumerate(_benign_specs())
    ]
    return sorted(cases, key=lambda case: case["case"])


def render_cases(cases: list[Record]) -> str:
    """ASCII only, so no invisible or look-alike character hides in the file."""
    return json.dumps(cases, indent=2, ensure_ascii=True) + "\n"


def build_manifest(
    cases: list[Record], seed: int, golden_manifest: bytes, cases_bytes: bytes
) -> Record:
    families: dict[str, int] = {}
    for case in cases:
        key = f"{case['label']}/{case['family']}"
        families[key] = families.get(key, 0) + 1
    return {
        "synthetic": True,
        "generator_version": GENERATOR_VERSION,
        "seed": seed,
        "golden_set": hashlib.sha256(golden_manifest).hexdigest(),
        "counts": {
            "cases": len(cases),
            "attack": sum(case["label"] == ATTACK for case in cases),
            "benign": sum(case["label"] == BENIGN for case in cases),
            "description": sum(
                case["carrier"] == CARRIER_DESCRIPTION for case in cases
            ),
            "clause": sum(case["carrier"] == CARRIER_CLAUSE for case in cases),
        },
        "families": families,
        "files": {CASES_FILE: hashlib.sha256(cases_bytes).hexdigest()},
    }
