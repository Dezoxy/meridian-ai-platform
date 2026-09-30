"""policies.yaml agrees with the table in security/data-classification.md."""

import re
from pathlib import Path

from meridian.platform.registry import load_registry

DOC = Path("docs") / "architecture" / "security" / "data-classification.md"
LABELS = re.compile(r"`([a-z-]+)`")
ALLOWED_LABELS_COLUMN = 3


def classes_in_document(repo_root: Path) -> dict[str, tuple[str, ...]]:
    """Class to allowed labels, from the rows under ``### Classes``."""
    lines = (repo_root / DOC).read_text(encoding="utf-8").splitlines()
    start = lines.index("### Classes")
    classes: dict[str, tuple[str, ...]] = {}
    for line in lines[start + 1 :]:
        if line.startswith("#"):
            break
        if not line.startswith("| `"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        name = cells[0].strip("`")
        classes[name] = tuple(LABELS.findall(cells[ALLOWED_LABELS_COLUMN]))
    return classes


def test_document_table_lists_all_four_classes(repo_root: Path) -> None:
    documented = classes_in_document(repo_root)

    assert list(documented) == ["synthetic", "internal", "personal", "special"]
    assert documented["special"] == ()


def test_policies_yaml_equals_the_document_table(
    repo_root: Path, real_registry: Path
) -> None:
    registry = load_registry(real_registry)

    from_policies = {c.id: tuple(c.residency) for c in registry.data_classes}

    assert from_policies == classes_in_document(repo_root)


def test_a_narrowed_class_in_policies_yaml_disagrees_with_the_document(
    repo_root: Path, registry_copy: Path
) -> None:
    policies = registry_copy / "policies.yaml"
    text = policies.read_text(encoding="utf-8")
    narrowed = text.replace(
        "  - id: personal\n    residency: [eu-region, eu-zone]",
        "  - id: personal\n    residency: [eu-region]",
    )
    assert narrowed != text
    policies.write_text(narrowed, encoding="utf-8")

    registry = load_registry(registry_copy)
    from_policies = {c.id: tuple(c.residency) for c in registry.data_classes}

    assert from_policies != classes_in_document(repo_root)
