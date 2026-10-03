"""The repository's own synthetic text, which no guardrail may disturb."""

import json

import pytest
from servicesupport import REPO_ROOT

SYNTHETIC = REPO_ROOT / "data" / "synthetic"


@pytest.fixture(scope="session")
def claim_descriptions() -> dict[str, str]:
    """Every claim's description in the golden set, by claim id."""
    claims = json.loads((SYNTHETIC / "claims.json").read_text(encoding="utf-8"))
    return {claim["claim_id"]: claim["description"] for claim in claims}


@pytest.fixture(scope="session")
def wording_texts() -> dict[str, str]:
    """The full text of every policy wording file, by file name."""
    files = sorted((SYNTHETIC / "wordings").iterdir())
    return {path.name: path.read_text(encoding="utf-8") for path in files}
