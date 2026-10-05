"""A golden set the ``eval run`` command accepts, planted in a directory."""

import hashlib
import json
from pathlib import Path


def plant_golden_set(
    directory: Path, claims: str = "[]", workload: str = "demo"
) -> None:
    """One file and the manifest that lists it and names ``workload``."""
    (directory / "claims.json").write_text(claims, encoding="utf-8")
    manifest = {
        "workload": workload,
        "generator_version": "1",
        "seed": 7,
        "files": {"claims.json": hashlib.sha256(claims.encode("utf-8")).hexdigest()},
    }
    (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
