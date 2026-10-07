"""Write the dataset: JSON, Markdown and the manifest, always the same bytes."""

import hashlib
import json
from pathlib import Path

from . import GENERATOR_VERSION, WORKLOAD, catalogue, heldout, injection
from .scenarios import Dataset
from .wording import render_wording

MANIFEST = "manifest.json"
INJECTION_DIR = "injection"
# The held-out cases (S071) are a report, not a fingerprint of the gate, so they
# have a folder of their own: the injection set's fingerprint refuses a file of
# its folder that its manifest does not list.
HELDOUT_DIR = "injection-heldout"


def render_json(value: object) -> str:
    """Two-space indent, real UTF-8, key order as built, one trailing newline."""
    return json.dumps(value, indent=2, ensure_ascii=False) + "\n"


def render_files(dataset: Dataset) -> dict[str, str]:
    """Every output file except the manifest, keyed by relative path."""
    files = {
        "policies.json": render_json(dataset.policies),
        "claim-history.json": render_json(dataset.history),
        "claims.json": render_json(dataset.claims),
        "expected-outcomes.json": render_json(dataset.outcomes),
    }
    for code, product in catalogue.PRODUCTS.items():
        files[f"wordings/{code}.md"] = render_wording(product)
    return files


def build_manifest(dataset: Dataset, seed: int, files: dict[str, bytes]) -> dict:
    reasons = [outcome["reason"] for outcome in dataset.outcomes]
    return {
        "synthetic": True,
        "workload": WORKLOAD,
        "generator_version": GENERATOR_VERSION,
        "seed": seed,
        "reference_date": catalogue.REFERENCE_DATE.isoformat(),
        "currency": catalogue.CURRENCY,
        "auto_approval_limit": catalogue.AUTO_APPROVAL_LIMIT,
        "counts": {
            "policies": len(dataset.policies),
            "claim_history": len(dataset.history),
            "claims": len(dataset.claims),
            "expected_outcomes": len(dataset.outcomes),
            "wordings": len(catalogue.PRODUCTS),
        },
        "reasons": {reason: reasons.count(reason) for reason in catalogue.REASONS},
        "files": {
            path: hashlib.sha256(files[path]).hexdigest() for path in sorted(files)
        },
    }


def write_dataset(dataset: Dataset, seed: int, out_dir: Path) -> list[str]:
    """Write every file under ``out_dir``, the manifest last, then the injection
    case set and its own manifest under ``injection/`` and the held-out cases and
    theirs under ``injection-heldout/``; return their paths."""
    encoded = {
        path: text.encode("utf-8") for path, text in render_files(dataset).items()
    }
    encoded[MANIFEST] = render_json(build_manifest(dataset, seed, encoded)).encode(
        "utf-8"
    )
    order = [path for path in sorted(encoded) if path != MANIFEST] + [MANIFEST]
    injection_files = render_injection_files(dataset, seed, encoded[MANIFEST])
    for path, data in injection_files.items():
        encoded[f"{INJECTION_DIR}/{path}"] = data
        order.append(f"{INJECTION_DIR}/{path}")
    for path, data in render_heldout_files(dataset, seed, encoded[MANIFEST]).items():
        encoded[f"{HELDOUT_DIR}/{path}"] = data
        order.append(f"{HELDOUT_DIR}/{path}")
    for path in order:
        target = out_dir / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(encoded[path])
    return order


def render_injection_files(
    dataset: Dataset, seed: int, golden_manifest: bytes
) -> dict[str, bytes]:
    """The injection case set, relative to its folder, the manifest last."""
    cases = injection.build_cases(dataset)
    cases_bytes = injection.render_cases(cases).encode("ascii")
    manifest = injection.build_manifest(cases, seed, golden_manifest, cases_bytes)
    return {
        injection.CASES_FILE: cases_bytes,
        MANIFEST: render_json(manifest).encode("utf-8"),
    }


def render_heldout_files(
    dataset: Dataset, seed: int, golden_manifest: bytes
) -> dict[str, bytes]:
    """The held-out cases, relative to their folder, the manifest last."""
    cases = heldout.build_cases(dataset)
    cases_bytes = injection.render_cases(cases).encode("ascii")
    manifest = heldout.build_manifest(cases, seed, golden_manifest, cases_bytes)
    return {
        injection.CASES_FILE: cases_bytes,
        MANIFEST: render_json(manifest).encode("utf-8"),
    }
