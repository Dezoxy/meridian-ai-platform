"""``seed_policies``: the simulated policy store, loaded from the generator's
output (S013, hard rule 2)."""

import hashlib
import json
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import psycopg
import pytest
from dbsupport import OWNER, SEED_ROLE, DatabaseHandle
from servicesupport import REPO_ROOT

from meridian.platform.common.db import connect
from meridian.platform.policy_mcp.seed import SeedCounts, SeedError, seed_policies

REAL_SOURCE = REPO_ROOT / "data" / "synthetic"
DATA_FILES = ("policies.json", "claim-history.json")
CANARY = "CANARY-CONTENT-7f3a"

Source = Callable[..., Path]

SELECT_POLICIES = "SELECT * FROM policy.policies ORDER BY policy_number"
SELECT_HISTORY = "SELECT * FROM policy.claim_history ORDER BY history_id"


@pytest.fixture
def source(tmp_path: Path) -> Source:
    """A copy of the manifest and the two data files, edited by the test.

    ``edit`` receives each file's parsed content by name; the manifest's hashes
    are recomputed afterwards unless ``rehash`` is false, so a test that breaks
    a record is not stopped by the hash check first.
    """

    def build(
        edit: Callable[[dict[str, Any]], None] | None = None,
        *,
        rehash: bool = True,
        manifest_edit: Callable[[dict[str, Any]], None] | None = None,
    ) -> Path:
        target = tmp_path / "synthetic"
        target.mkdir()
        shutil.copy(REAL_SOURCE / "manifest.json", target)
        content = {
            name: json.loads((REAL_SOURCE / name).read_text(encoding="utf-8"))
            for name in DATA_FILES
        }
        if edit:
            edit(content)
        manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
        for name, records in content.items():
            data = json.dumps(records, indent=2).encode("utf-8")
            (target / name).write_bytes(data)
            if rehash:
                manifest["files"][name] = hashlib.sha256(data).hexdigest()
        if manifest_edit:
            manifest_edit(manifest)
        (target / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        return target

    return build


def seed(db: DatabaseHandle, directory: Path) -> SeedCounts:
    """Seed in one transaction of the seed's own role and commit, as the CLI does."""
    with connect(db.dsn(SEED_ROLE), "test-seed") as conn:
        counts = seed_policies(conn, directory)
        conn.commit()
        return counts


def table(db: DatabaseHandle, statement: str, params: tuple = ()) -> list[tuple]:
    with connect(db.dsn(OWNER), "test-read") as conn:
        return conn.execute(statement, params).fetchall()


def assert_nothing_written(db: DatabaseHandle) -> None:
    assert table(db, SELECT_POLICIES) == []
    assert table(db, SELECT_HISTORY) == []


def test_the_real_synthetic_data_loads_fifty_six_policies_and_fifty_one_rows(
    fresh_database: DatabaseHandle,
) -> None:
    counts = seed(fresh_database, REAL_SOURCE)

    assert counts == SeedCounts(policies=56, claim_history=51)
    assert len(table(fresh_database, SELECT_POLICIES)) == 56
    assert len(table(fresh_database, SELECT_HISTORY)) == 51


def test_the_limit_goes_to_cover_limit_and_a_lapsed_policy_keeps_its_date(
    fresh_database: DatabaseHandle,
) -> None:
    seed(fresh_database, REAL_SOURCE)
    records = json.loads((REAL_SOURCE / "policies.json").read_text(encoding="utf-8"))
    lapsed = next(r for r in records if r["status"] == "lapsed")

    with connect(fresh_database.dsn(OWNER), "test-read") as conn:
        rows = conn.execute(
            "SELECT cover_limit, lapsed_on::text, sum_insured FROM policy.policies "
            "WHERE policy_number = %s",
            (lapsed["policy_number"],),
        ).fetchall()

    assert rows == [(lapsed["limit"], lapsed["lapsed_on"], lapsed["sum_insured"])]


def test_the_rows_are_equal_after_a_second_run_and_the_counts_are_the_same(
    fresh_database: DatabaseHandle,
) -> None:
    first = seed(fresh_database, REAL_SOURCE)
    before = (
        table(fresh_database, SELECT_POLICIES),
        table(fresh_database, SELECT_HISTORY),
    )

    second = seed(fresh_database, REAL_SOURCE)

    assert second == first
    assert (
        table(fresh_database, SELECT_POLICIES),
        table(fresh_database, SELECT_HISTORY),
    ) == before


def test_a_changed_record_is_updated_in_place(
    fresh_database: DatabaseHandle, source: Source
) -> None:
    seed(fresh_database, REAL_SOURCE)

    def raise_the_limit(content: dict[str, Any]) -> None:
        content["policies.json"][0]["limit"] = 999
        content["claim-history.json"][0]["paid_amount"] = 777

    counts = seed(fresh_database, source(raise_the_limit))

    assert counts == SeedCounts(policies=56, claim_history=51)
    assert table(
        fresh_database,
        "SELECT cover_limit FROM policy.policies WHERE policy_number = 'POL-0001'",
    ) == [(999,)]
    assert len(table(fresh_database, SELECT_POLICIES)) == 56
    assert table(
        fresh_database,
        "SELECT paid_amount FROM policy.claim_history WHERE history_id = 'HIST-0001'",
    ) == [(777,)]


def test_a_policy_removed_from_the_source_is_removed_with_its_history(
    fresh_database: DatabaseHandle, source: Source
) -> None:
    seed(fresh_database, REAL_SOURCE)
    history = json.loads((REAL_SOURCE / "claim-history.json").read_text("utf-8"))
    gone = history[0]["policy_number"]
    gone_history = [h["history_id"] for h in history if h["policy_number"] == gone]
    assert gone_history

    def shrink(content: dict[str, Any]) -> None:
        content["policies.json"][:] = [
            p for p in content["policies.json"] if p["policy_number"] != gone
        ]
        content["claim-history.json"][:] = [
            h for h in content["claim-history.json"] if h["policy_number"] != gone
        ]

    counts = seed(fresh_database, source(shrink))

    assert counts == SeedCounts(policies=55, claim_history=51 - len(gone_history))
    assert table(
        fresh_database,
        "SELECT count(*) FROM policy.policies WHERE policy_number = %s",
        (gone,),
    ) == [(0,)]
    assert table(
        fresh_database,
        "SELECT count(*) FROM policy.claim_history WHERE policy_number = %s",
        (gone,),
    ) == [(0,)]
    assert len(table(fresh_database, SELECT_POLICIES)) == 55
    assert len(table(fresh_database, SELECT_HISTORY)) == 51 - len(gone_history)


def test_a_history_row_removed_from_the_source_is_removed_and_its_policy_stays(
    fresh_database: DatabaseHandle, source: Source
) -> None:
    seed(fresh_database, REAL_SOURCE)

    def drop_first_history(content: dict[str, Any]) -> None:
        del content["claim-history.json"][0]

    counts = seed(fresh_database, source(drop_first_history))

    assert counts == SeedCounts(policies=56, claim_history=50)
    assert table(
        fresh_database,
        "SELECT count(*) FROM policy.claim_history WHERE history_id = 'HIST-0001'",
    ) == [(0,)]
    assert len(table(fresh_database, SELECT_POLICIES)) == 56


def test_a_source_that_fails_validation_removes_nothing(
    fresh_database: DatabaseHandle, source: Source
) -> None:
    seed(fresh_database, REAL_SOURCE)

    def shrink_and_break(content: dict[str, Any]) -> None:
        del content["policies.json"][1:]
        del content["claim-history.json"][1:]
        del content["claim-history.json"][0]["peril"]

    with pytest.raises(SeedError):
        seed(fresh_database, source(shrink_and_break))

    assert len(table(fresh_database, SELECT_POLICIES)) == 56
    assert len(table(fresh_database, SELECT_HISTORY)) == 51


def test_no_column_holds_a_holders_name_email_or_street(
    fresh_database: DatabaseHandle,
) -> None:
    seed(fresh_database, REAL_SOURCE)
    first = json.loads((REAL_SOURCE / "policies.json").read_text(encoding="utf-8"))[0]
    personal = [
        first["holder"]["name"],
        first["holder"]["email"],
        first["holder"]["address"]["street"],
    ]

    rows = table(
        fresh_database,
        "SELECT row_to_json(t)::text FROM policy.policies t "
        "UNION ALL SELECT row_to_json(h)::text FROM policy.claim_history h",
    )
    assert len(rows) == 56 + 51
    for needle in personal:
        assert needle
        assert not [row for (row,) in rows if needle in row], needle


# ── what the seed refuses, and writes nothing for ───────────────────────────
def test_a_manifest_that_is_not_synthetic_is_refused(
    fresh_database: DatabaseHandle, source: Source
) -> None:
    directory = source(manifest_edit=lambda m: m.update(synthetic=False))

    with pytest.raises(SeedError, match="synthetic"):
        seed(fresh_database, directory)

    assert_nothing_written(fresh_database)


@pytest.mark.parametrize("value", ["true", 1, None])
def test_only_the_boolean_true_counts_as_synthetic(
    fresh_database: DatabaseHandle, source: Source, value: object
) -> None:
    directory = source(manifest_edit=lambda m: m.update(synthetic=value))

    with pytest.raises(SeedError, match="synthetic"):
        seed(fresh_database, directory)

    assert_nothing_written(fresh_database)


def test_a_manifest_without_the_synthetic_key_is_refused(
    fresh_database: DatabaseHandle, source: Source
) -> None:
    directory = source(manifest_edit=lambda m: m.pop("synthetic"))

    with pytest.raises(SeedError, match="synthetic"):
        seed(fresh_database, directory)

    assert_nothing_written(fresh_database)


def test_a_missing_manifest_is_refused(
    fresh_database: DatabaseHandle, source: Source
) -> None:
    directory = source()
    (directory / "manifest.json").unlink()

    with pytest.raises(SeedError, match=r"manifest\.json"):
        seed(fresh_database, directory)

    assert_nothing_written(fresh_database)


def test_a_manifest_that_is_not_json_is_refused(
    fresh_database: DatabaseHandle, source: Source
) -> None:
    directory = source()
    (directory / "manifest.json").write_text("{", encoding="utf-8")

    with pytest.raises(SeedError, match=r"manifest\.json"):
        seed(fresh_database, directory)

    assert_nothing_written(fresh_database)


@pytest.mark.parametrize("name", DATA_FILES)
def test_a_data_file_whose_hash_differs_is_refused(
    fresh_database: DatabaseHandle, source: Source, name: str
) -> None:
    directory = source(manifest_edit=lambda m: m["files"].update({name: "0" * 64}))

    with pytest.raises(SeedError, match=rf"{name}.*hash"):
        seed(fresh_database, directory)

    assert_nothing_written(fresh_database)


def test_a_data_file_changed_after_the_generator_is_refused(
    fresh_database: DatabaseHandle, source: Source
) -> None:
    def tamper(content: dict[str, Any]) -> None:
        content["policies.json"][0]["limit"] = 1

    directory = source(tamper, rehash=False)

    with pytest.raises(SeedError, match=r"policies\.json.*hash"):
        seed(fresh_database, directory)

    assert_nothing_written(fresh_database)


def test_a_manifest_that_does_not_list_a_data_file_is_refused(
    fresh_database: DatabaseHandle, source: Source
) -> None:
    directory = source(manifest_edit=lambda m: m["files"].pop("policies.json"))

    with pytest.raises(SeedError, match=r"policies\.json"):
        seed(fresh_database, directory)

    assert_nothing_written(fresh_database)


def test_a_missing_data_file_is_refused(
    fresh_database: DatabaseHandle, source: Source
) -> None:
    directory = source()
    (directory / "claim-history.json").unlink()

    with pytest.raises(SeedError, match=r"claim-history\.json"):
        seed(fresh_database, directory)

    assert_nothing_written(fresh_database)


def test_a_record_with_a_field_missing_names_the_file_and_the_index(
    fresh_database: DatabaseHandle, source: Source
) -> None:
    def drop_status(content: dict[str, Any]) -> None:
        del content["policies.json"][3]["status"]

    directory = source(drop_status)

    with pytest.raises(SeedError) as raised:
        seed(fresh_database, directory)

    assert "policies.json" in str(raised.value)
    assert "3" in str(raised.value)
    assert_nothing_written(fresh_database)


def test_a_malformed_history_record_writes_no_policy_either(
    fresh_database: DatabaseHandle, source: Source
) -> None:
    def drop_peril(content: dict[str, Any]) -> None:
        del content["claim-history.json"][5]["peril"]

    directory = source(drop_peril)

    with pytest.raises(SeedError, match=r"claim-history\.json.*5"):
        seed(fresh_database, directory)

    assert_nothing_written(fresh_database)


def bad_policy(**changes: Any) -> Callable[[dict[str, Any]], None]:
    def edit(content: dict[str, Any]) -> None:
        content["policies.json"][2].update(changes)

    return edit


@pytest.mark.parametrize(
    "changes",
    [
        pytest.param({"policy_number": f"{CANARY}"}, id="number-pattern"),
        pytest.param({"product": CANARY * 5}, id="product-too-long"),
        pytest.param({"wording_version": CANARY}, id="wording-version-too-long"),
        pytest.param({"status": CANARY}, id="unknown-status"),
        pytest.param({"deductible": CANARY}, id="deductible-not-a-number"),
        pytest.param({"deductible": -5}, id="negative-deductible"),
        pytest.param({"deductible": True}, id="deductible-boolean"),
        pytest.param({"limit": 1.5}, id="limit-not-an-integer"),
        pytest.param({"deductible": 1_000_000_001}, id="deductible-over-the-cap"),
        pytest.param({"limit": 1_000_000_001}, id="limit-over-the-cap"),
        pytest.param({"sum_insured": 1_000_000_001}, id="sum-insured-over-the-cap"),
        pytest.param({"start_date": CANARY}, id="date-not-a-date"),
        pytest.param(
            {"start_date": "2030-01-01", "end_date": "2029-01-01"},
            id="end-before-start",
        ),
        pytest.param({"status": "lapsed", "lapsed_on": None}, id="lapsed-no-date"),
        pytest.param({"status": "active", "lapsed_on": "2026-01-01"}, id="active-date"),
    ],
)
def test_an_invalid_policy_is_refused_without_quoting_it(
    fresh_database: DatabaseHandle, source: Source, changes: dict[str, Any]
) -> None:
    directory = source(bad_policy(**changes))

    with pytest.raises(SeedError) as raised:
        seed(fresh_database, directory)

    message = str(raised.value)
    assert "policies.json" in message
    assert CANARY not in message
    assert "2" in message  # the index of the record
    # Nothing chained: a cause would hold the record's values.
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert_nothing_written(fresh_database)


def test_an_amount_at_the_cap_of_the_tools_output_schemas_is_stored(
    fresh_database: DatabaseHandle, source: Source
) -> None:
    def at_the_cap(content: dict[str, Any]) -> None:
        content["policies.json"][0].update(
            deductible=1_000_000_000, limit=1_000_000_000, sum_insured=1_000_000_000
        )
        content["claim-history.json"][0]["paid_amount"] = 1_000_000_000

    seed(fresh_database, source(at_the_cap))

    assert table(
        fresh_database,
        "SELECT deductible, cover_limit, sum_insured FROM policy.policies "
        "WHERE policy_number = 'POL-0001'",
    ) == [(1_000_000_000,) * 3]
    assert table(
        fresh_database,
        "SELECT paid_amount FROM policy.claim_history WHERE history_id = 'HIST-0001'",
    ) == [(1_000_000_000,)]


def test_an_amount_over_the_cap_in_a_history_record_is_refused(
    fresh_database: DatabaseHandle, source: Source
) -> None:
    def over(content: dict[str, Any]) -> None:
        content["claim-history.json"][0]["paid_amount"] = 1_000_000_001

    with pytest.raises(SeedError, match=r"claim-history\.json.*0"):
        seed(fresh_database, source(over))

    assert_nothing_written(fresh_database)


def test_the_error_message_holds_no_holder_content(
    fresh_database: DatabaseHandle, source: Source
) -> None:
    holder = json.loads((REAL_SOURCE / "policies.json").read_text(encoding="utf-8"))[2][
        "holder"
    ]

    with pytest.raises(SeedError) as raised:
        seed(fresh_database, source(bad_policy(deductible="not a number")))

    for needle in (holder["name"], holder["email"], "not a number"):
        assert needle not in str(raised.value)


def test_a_history_row_for_an_unknown_policy_is_refused_and_rolls_back(
    fresh_database: DatabaseHandle, source: Source
) -> None:
    def orphan(content: dict[str, Any]) -> None:
        content["claim-history.json"][0]["policy_number"] = "POL-9999"

    directory = source(orphan)

    with (
        pytest.raises(psycopg.errors.ForeignKeyViolation),
        connect(fresh_database.dsn(SEED_ROLE), "test-seed") as conn,
    ):
        seed_policies(conn, directory)

    assert_nothing_written(fresh_database)
