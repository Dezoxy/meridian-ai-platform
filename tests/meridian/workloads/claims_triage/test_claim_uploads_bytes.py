"""The upload route's byte budget (S070 F4d, the second security review's M-B).

The rate counted files, and a file may be a megabyte: at 30 a minute a caller who
had made 43 claims filled the 128 MiB ceiling in 4.3 minutes, for good. The store
now takes at most ``MAX_BYTES_PER_MINUTE`` bytes in any minute, and answers the
rate's 429 past it.
"""

from dbsupport import DatabaseHandle
from fastapi.testclient import TestClient
from servicesupport import owner_rows
from workloads.claims_triage.test_claim_uploads import (
    MAX_FILE_BYTES,
    PDF,
    audit_rows,
    blob,
    file_count,
    make_client,
    put_claim,
    upload,
)

from meridian.workloads.claims_triage.uploads import (
    DEFAULT_CEILING_BYTES,
    MAX_BYTES_PER_MINUTE,
    MAX_CEILING_BYTES,
    MIB,
    RATE_DETAIL,
)

CLAIMS = ("CLM-9311", "CLM-9312", "CLM-9313", "CLM-9314", "CLM-9315")


def test_the_budget_is_eight_mib_a_minute_and_the_arithmetic_holds() -> None:
    assert MAX_BYTES_PER_MINUTE == 8 * MIB
    # A full-size file fits, so a single upload is never refused for the budget.
    assert MAX_FILE_BYTES <= MAX_BYTES_PER_MINUTE
    # The default ceiling takes 16 minutes to fill, the cap 32.
    assert DEFAULT_CEILING_BYTES // MAX_BYTES_PER_MINUTE == 16
    assert MAX_CEILING_BYTES // MAX_BYTES_PER_MINUTE == 32


def store(client: TestClient, claim: str, size: int, salt: int) -> int:
    return upload(client, blob(size, salt), claim_id=claim, kind="photos").status_code


def fill_the_minute(client: TestClient, db: DatabaseHandle) -> None:
    """Eight files of 1 MiB on four claims (a claim holds 3 MiB): the budget."""
    for index, claim in enumerate(CLAIMS[:4]):
        put_claim(db, claim)
        for part in range(2):
            assert store(client, claim, MIB, index * 2 + part) == 201


def test_the_file_that_makes_exactly_eight_mib_is_taken_and_the_next_is_not(
    fresh_database: DatabaseHandle,
) -> None:
    db = fresh_database
    client = make_client(db)
    fill_the_minute(client, db)
    put_claim(db, CLAIMS[4])
    rows_before = len(audit_rows(db))

    over = upload(client, blob(10, 99), claim_id=CLAIMS[4])

    assert file_count(db) == 8
    assert over.status_code == 429
    assert over.headers["retry-after"] == "60"
    assert over.json()["detail"] == RATE_DETAIL
    assert file_count(db) == 8
    assert len(audit_rows(db)) == rows_before


def test_a_file_that_would_pass_eight_mib_is_refused_whole_and_one_that_fits_is_not(
    fresh_database: DatabaseHandle,
) -> None:
    db = fresh_database
    client = make_client(db)
    for claim in CLAIMS[:4]:
        put_claim(db, claim)
    # Seven MiB: six files on three claims and one on the fourth.
    sizes = [(CLAIMS[i // 2], MIB) for i in range(6)] + [(CLAIMS[3], MIB)]
    for salt, (claim, size) in enumerate(sizes):
        assert store(client, claim, size, salt) == 201

    half = MIB // 2
    first = store(client, CLAIMS[3], half, 70)  # 7.5 MiB
    too_big = store(client, CLAIMS[3], MIB, 71)  # would make 8.5 MiB
    exact = store(client, CLAIMS[3], half, 72)  # exactly 8 MiB
    over_by_a_little = store(client, CLAIMS[3], 8, 73)  # 8 MiB and 8 bytes

    assert (first, too_big, exact, over_by_a_little) == (201, 429, 201, 429)
    assert file_count(db) == 9


def test_the_minute_moves_and_the_store_takes_files_again(
    fresh_database: DatabaseHandle,
) -> None:
    db = fresh_database
    client = make_client(db)
    fill_the_minute(client, db)
    put_claim(db, CLAIMS[4])
    assert upload(client, PDF, claim_id=CLAIMS[4]).status_code == 429

    owner_rows(
        db,
        "UPDATE claims.claim_files "
        "SET received_at = received_at - interval '2 minutes' RETURNING 1",
    )
    after = upload(client, PDF, claim_id=CLAIMS[4])

    assert after.status_code == 201


def test_the_store_ceiling_is_answered_before_the_budget(
    fresh_database: DatabaseHandle,
) -> None:
    # A full store says so (507) rather than "slow down".
    db = fresh_database
    client = make_client(db, ceiling=3 * MIB, rows=5)
    put_claim(db, CLAIMS[0])
    put_claim(db, CLAIMS[1])
    for part in range(3):
        assert store(client, CLAIMS[0], MIB, part) == 201

    assert store(client, CLAIMS[1], MIB, 50) == 507
