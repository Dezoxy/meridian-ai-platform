"""The public registry and its snapshot carry no account details (T-12)."""

import json
import re
from pathlib import Path

import pytest

SNAPSHOT_FIELDS = {
    "capacity",
    "deployment_name",
    "location",
    "model_name",
    "model_version",
    "purpose",
    "sku_name",
}
FORBIDDEN = {
    "guid": re.compile(
        r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I
    ),
    "url": re.compile(r"https?://", re.I),
    "azure host": re.compile(r"\.azure\.", re.I),
    "windows.net": re.compile(r"\.windows\.net", re.I),
    "cognitiveservices": re.compile(r"cognitiveservices", re.I),
    "onmicrosoft.com": re.compile(r"onmicrosoft\.com", re.I),
    "ipv4": re.compile(r"\b\d{1,3}(\.\d{1,3}){3}\b"),
    "email": re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"),
}


def test_snapshot_entries_carry_exactly_the_seven_expected_fields(
    snapshot_path: Path,
) -> None:
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))

    assert snapshot
    for key, entry in snapshot.items():
        assert set(entry) == SNAPSHOT_FIELDS, key


def public_files(real_registry: Path, snapshot_path: Path) -> list[Path]:
    """Everything under config/registry that is published: YAML, schemas, README."""
    readme = real_registry / "README.md"
    return [
        *sorted(real_registry.glob("*.yaml")),
        snapshot_path,
        *sorted((real_registry / "schemas").glob("*.json")),
        *([readme] if readme.is_file() else []),
    ]


@pytest.mark.parametrize("kind", sorted(FORBIDDEN))
def test_registry_files_and_snapshot_hold_no_account_details(
    real_registry: Path, snapshot_path: Path, kind: str
) -> None:
    files = public_files(real_registry, snapshot_path)

    assert len(files) >= 13  # six YAML, the snapshot, six schemas
    for path in files:
        match = FORBIDDEN[kind].search(path.read_text(encoding="utf-8"))
        assert match is None, f"{path.name} contains a {kind}: {match and match[0]!r}"


@pytest.mark.parametrize(
    ("sample", "kind"),
    [
        ("11111111-2222-3333-4444-555555555555", "guid"),
        ("endpoint: https://x.example", "url"),
        ("acct.openai.azure.com", "azure host"),
        ("acct.privatelink.azure.us", "azure host"),
        ("acct.blob.core.windows.net", "windows.net"),
        ("acct.cognitiveservices.example", "cognitiveservices"),
        ("user@corp.onmicrosoft.com", "onmicrosoft.com"),
        ("host: 10.0.0.4", "ipv4"),
        ("someone@example.com", "email"),
    ],
)
def test_the_scan_patterns_detect_what_they_are_for(sample: str, kind: str) -> None:
    assert FORBIDDEN[kind].search(sample)


@pytest.mark.parametrize("sample", ["2024-11-20", "sdc/gpt-4o", "1.2.3", "version 1.0"])
def test_the_ipv4_pattern_ignores_versions_and_dates(sample: str) -> None:
    assert FORBIDDEN["ipv4"].search(sample) is None
