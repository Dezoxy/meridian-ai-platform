"""The helper that edits one registry entry by its key (S057).

It is test support, but the registry tests lean on it for every planted
violation, so it gets its own tests: it edits the named entry and no other,
survives a line added between an entry's fields, and fails loudly when what it
was asked to edit is not there (a test must not pass because its violation was
never planted).
"""

from pathlib import Path

import pytest
from registrysupport import (
    RegistryEditError,
    add_field,
    apply_changes,
    planted,
    remove_field,
    set_field,
)

HEAD = """\
# a header comment
deployments:
  - id: alpha
    provider: azure
    residency: eu-region
    price:
      currency: USD
      checked: 2026-09-30
    rate_limits:
      requests: 20
      tokens: 100

  # the comment of the next entry
  - id: alpha-b
    provider: azure
    residency: eu-region
    price:
      currency: USD
      checked: 2026-09-30
  - id: omega
    provider: replay
    residency: eu-region
    price:
      currency: USD
      checked: 2026-09-30
"""
# omega is the last entry of the file; this variant lists a key after it.
WITH_TAIL = HEAD + "replay:\n  - purpose: chat\n"


def write(directory: Path, text: str, name: str = "models.yaml") -> Path:
    (directory / name).write_text(text, encoding="utf-8")
    return directory


def read(directory: Path, name: str = "models.yaml") -> str:
    return (directory / name).read_text(encoding="utf-8")


def with_a_line_after_every_field(text: str) -> str:
    """The shape of a field added inside an entry: a comment after each line
    of an entry, nested lines included, even between a key and its children."""
    out: list[str] = []
    for line in text.splitlines(keepends=True):
        out.append(line)
        if line.startswith("    "):
            out.append("    # a line added inside the entry\n")
    return "".join(out)


# ── the entry is found by its key, and only it is edited ────────────────────
def test_the_same_line_in_two_entries_is_replaced_in_the_named_one_only(
    tmp_path: Path,
) -> None:
    write(tmp_path, HEAD)

    set_field("alpha-b", "residency", "global").apply(tmp_path)

    assert read(tmp_path) == HEAD.replace(
        "    residency: eu-region\n    price:\n      currency: USD\n      checked: "
        "2026-09-30\n  - id: omega",
        "    residency: global\n    price:\n      currency: USD\n      checked: "
        "2026-09-30\n  - id: omega",
    )
    assert read(tmp_path).count("residency: eu-region") == 2


def test_a_key_that_is_the_start_of_another_key_finds_its_own_entry(
    tmp_path: Path,
) -> None:
    write(tmp_path, HEAD)

    set_field("alpha", "provider", "other").apply(tmp_path)

    text = read(tmp_path)
    assert text.count("provider: other") == 1
    assert text.index("provider: other") < text.index("- id: alpha-b")


def test_a_field_of_a_later_entry_is_never_reached(tmp_path: Path) -> None:
    # `rate_limits` is only in alpha: asking for it in alpha-b must fail, not
    # run on to find another entry's.
    write(tmp_path, HEAD)

    with pytest.raises(RegistryEditError):
        remove_field("alpha-b", "rate_limits").apply(tmp_path)

    assert read(tmp_path) == HEAD


# ── a line added between an entry's fields ──────────────────────────────────
def test_an_entry_with_a_line_added_after_every_field_is_still_edited(
    tmp_path: Path,
) -> None:
    mutated = with_a_line_after_every_field(HEAD)
    write(tmp_path, mutated)

    apply_changes(
        tmp_path,
        set_field("alpha-b", "residency", "global"),
        set_field("alpha", "price.checked", "2027-01-01"),
        remove_field("alpha", "rate_limits"),
    )

    text = read(tmp_path)
    assert text.count("residency: global") == 1
    assert "      checked: 2027-01-01\n" in text
    assert "requests: 20" not in text
    assert "tokens: 100" not in text
    # nothing of the other entries went with it
    assert text.count("- id: ") == 3
    assert text.count("residency: eu-region") == 2
    assert text.count("      checked: 2026-09-30\n") == 2


def test_a_nested_block_is_replaced_whole_when_a_comment_sits_between_its_lines(
    tmp_path: Path,
) -> None:
    write(tmp_path, with_a_line_after_every_field(HEAD))

    set_field("omega", "price", "{currency: EUR}").apply(tmp_path)

    text = read(tmp_path)
    assert "    price: {currency: EUR}\n" in text
    # omega's own two price lines are gone; the other entries keep theirs
    assert text.count("      currency: USD\n") == 2


# ── the last entry, and the entry before a top-level key ────────────────────
def test_the_last_entry_of_the_file_is_edited(tmp_path: Path) -> None:
    write(tmp_path, HEAD)

    apply_changes(
        tmp_path,
        set_field("omega", "provider", "recorded"),
        add_field("omega", "recorded_from", "alpha"),
        remove_field("omega", "price"),
    )

    assert read(tmp_path).endswith(
        "  - id: omega\n    provider: recorded\n    residency: eu-region\n"
        "    recorded_from: alpha\n"
    )


def test_an_added_field_stays_inside_an_entry_that_a_top_level_key_follows(
    tmp_path: Path,
) -> None:
    write(tmp_path, WITH_TAIL)

    add_field("omega", "dimensions", "8").apply(tmp_path)

    assert read(tmp_path).endswith(
        "      checked: 2026-09-30\n    dimensions: 8\nreplay:\n  - purpose: chat\n"
    )


def test_an_added_field_lands_before_the_comment_of_the_next_entry(
    tmp_path: Path,
) -> None:
    write(tmp_path, HEAD)

    add_field("alpha", "dimensions", "8").apply(tmp_path)

    assert (
        "      tokens: 100\n    dimensions: 8\n\n  # the comment of the next entry\n"
        "  - id: alpha-b\n"
    ) in read(tmp_path)


def test_removing_a_trailing_block_leaves_the_next_entrys_comment(
    tmp_path: Path,
) -> None:
    write(tmp_path, HEAD)

    remove_field("alpha", "rate_limits").apply(tmp_path)

    assert read(tmp_path) == HEAD.replace(
        "    rate_limits:\n      requests: 20\n      tokens: 100\n", ""
    )


# ── what a nested field is ──────────────────────────────────────────────────
def test_a_nested_field_is_set_by_its_path_and_keeps_its_indent(
    tmp_path: Path,
) -> None:
    write(tmp_path, HEAD)

    set_field("alpha", "rate_limits.tokens", "99").apply(tmp_path)

    assert read(tmp_path) == HEAD.replace("      tokens: 100\n", "      tokens: 99\n")


def test_a_nested_field_is_removed_by_its_path(tmp_path: Path) -> None:
    write(tmp_path, HEAD)

    remove_field("alpha", "rate_limits.requests").apply(tmp_path)

    assert read(tmp_path) == HEAD.replace("      requests: 20\n", "")


def test_a_field_is_not_found_in_a_nested_block_by_the_name_of_a_nested_key(
    tmp_path: Path,
) -> None:
    # `currency` is a field of `price`, not of the entry.
    write(tmp_path, HEAD)

    with pytest.raises(RegistryEditError, match="no field 'currency'"):
        set_field("alpha", "currency", "EUR").apply(tmp_path)


# ── what it refuses to do silently ──────────────────────────────────────────
def test_an_entry_that_is_not_there_is_named_in_the_message(tmp_path: Path) -> None:
    write(tmp_path, HEAD)

    with pytest.raises(RegistryEditError) as raised:
        set_field("ghost", "residency", "global").apply(tmp_path)

    assert str(raised.value) == "models.yaml: no entry '- id: ghost'"


def test_a_field_that_is_not_there_is_named_in_the_message(tmp_path: Path) -> None:
    write(tmp_path, HEAD)

    with pytest.raises(RegistryEditError) as raised:
        remove_field("omega", "rate_limits").apply(tmp_path)

    assert str(raised.value) == "models.yaml: entry 'omega' has no field 'rate_limits'"
    assert read(tmp_path) == HEAD


def test_a_nested_field_that_is_not_there_is_named_in_the_message(
    tmp_path: Path,
) -> None:
    write(tmp_path, HEAD)

    with pytest.raises(RegistryEditError) as raised:
        set_field("alpha", "rate_limits.burst", "5").apply(tmp_path)

    assert str(raised.value) == (
        "models.yaml: entry 'alpha' has no field 'rate_limits.burst'"
    )


def test_a_field_found_twice_is_named_in_the_message(tmp_path: Path) -> None:
    provider = "    provider: azure\n"
    write(tmp_path, HEAD.replace(provider, provider + "    provider: b\n", 1))

    with pytest.raises(RegistryEditError) as raised:
        set_field("alpha", "provider", "c").apply(tmp_path)

    assert str(raised.value) == "models.yaml: entry 'alpha' has field 'provider' twice"


def test_an_entry_found_twice_is_named_in_the_message(tmp_path: Path) -> None:
    write(tmp_path, HEAD + "  - id: omega\n    provider: again\n")

    with pytest.raises(RegistryEditError) as raised:
        set_field("omega", "provider", "c").apply(tmp_path)

    assert str(raised.value) == "models.yaml: entry '- id: omega' is there twice"


def test_a_field_that_is_added_twice_is_refused(tmp_path: Path) -> None:
    write(tmp_path, HEAD)

    with pytest.raises(RegistryEditError) as raised:
        add_field("alpha", "provider", "again").apply(tmp_path)

    assert str(raised.value) == (
        "models.yaml: entry 'alpha' already has field 'provider'"
    )
    assert read(tmp_path) == HEAD


def test_a_file_without_the_entry_names_the_file(tmp_path: Path) -> None:
    write(tmp_path, "agents:\n  - id: a\n", name="agents.yaml")

    with pytest.raises(RegistryEditError, match=r"^agents\.yaml: no entry"):
        set_field("ghost", "x", "1", file="agents.yaml").apply(tmp_path)


# ── how it composes ─────────────────────────────────────────────────────────
def test_changes_are_applied_in_order_and_the_directory_is_returned(
    tmp_path: Path,
) -> None:
    write(tmp_path, HEAD)

    returned = apply_changes(
        tmp_path,
        set_field("alpha", "provider", "first"),
        set_field("alpha", "provider", "second"),
    )

    assert returned == tmp_path
    assert "provider: second\n" in read(tmp_path)
    assert "provider: first" not in read(tmp_path)


def test_planted_hands_the_text_edits_to_plant_and_applies_the_changes(
    tmp_path: Path,
) -> None:
    write(tmp_path, HEAD)
    seen: list[tuple[str, str, str]] = []

    def fake_plant(*edits: tuple[str, str, str]) -> Path:
        seen.extend(edits)
        return tmp_path

    text_edit = ("models.yaml", "# a header comment", "# changed")
    directory = planted(fake_plant, set_field("omega", "provider", "x"), text_edit)

    assert directory == tmp_path
    assert seen == [text_edit]
    assert "    provider: x\n" in read(tmp_path)


def test_a_change_to_another_file_edits_that_file(tmp_path: Path) -> None:
    write(tmp_path, HEAD)
    write(tmp_path, "tools:\n  - id: a\n    scope: x\n", name="tools.yaml")

    set_field("a", "scope", "y", file="tools.yaml").apply(tmp_path)

    assert read(tmp_path, "tools.yaml") == "tools:\n  - id: a\n    scope: y\n"
    assert read(tmp_path) == HEAD
