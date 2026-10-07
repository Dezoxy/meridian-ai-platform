"""The adjuster's list of the files a claimant sent (S070 F4a): a table on the
claim page with each file's kind, type, size, received time and SHA-256, and the
words "not scanned" on every row. There is no link and no file identifier in the
page: the download does not exist yet. The list is read with the tenant's
filter as the claim is. A ``claim.file_stored`` row in the audit trail reads as
every other event does.
"""

import hashlib
import logging
import re
import uuid
from datetime import UTC, datetime

import pytest
from dbsupport import DatabaseHandle
from servicesupport import claim_with_id, owner_rows
from workloads.claims_triage.test_adjuster_pages import (
    ESCAPED_MARKUP,
    MARKUP,
    SECURITY_HEADERS,
    Page,
    trail_cells,
    url_of,
)
from workloads.claims_triage.test_claim_uploads import (
    CANARY_BYTES,
    CANARY_NAME,
    CANARY_TYPE,
    CLAIM,
    JPEG,
    OTHER_CLAIM,
    PDF,
    PNG,
    blob,
    make_client,
    put_claim,
)

from meridian.workloads.claims_triage import adjuster
from meridian.workloads.claims_triage.claim_files import FileSummary

SAME_ORIGIN = {"Origin": "http://testserver"}
HEADING = "Files the claimant sent"
NO_FILES = "No file has been sent for this claim."
NOT_SCANNED = "not scanned"
SCAN_SENTENCE = (
    "Malware scanning is designed, not implemented: no file listed here has been "
    "scanned."
)
RECEIVED = re.compile(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d UTC")
MOMENT = datetime(2026, 10, 2, 8, 30, 15, tzinfo=UTC)


def twin(client, data: bytes, kind: str = "photos", claim_id: str = CLAIM, **names):
    return client.post(
        f"/claimant/claims/{claim_id}/files",
        data={"kind": kind},
        files={
            "file": (
                names.get("filename", "evidence.bin"),
                data,
                names.get("declared", "application/octet-stream"),
            )
        },
        headers=SAME_ORIGIN,
        follow_redirects=False,
    )


def file_table(page: Page) -> list[str]:
    """The rows of the files table, each its first line: the parser keeps adding
    the text after a table's last row to it."""
    return [r.strip().splitlines()[0] for r in page.rows if NOT_SCANNED in r]


def claim_view(files: tuple[FileSummary, ...]) -> adjuster.ClaimView:
    return adjuster.ClaimView(
        claim_id=CLAIM,
        state="awaiting_adjuster",
        since=MOMENT,
        received_at=MOMENT,
        facts=adjuster.facts_of(claim_with_id(CLAIM)),
        proposal=None,
        proposal_note="no proposal is stored",
        decision=None,
        trail=(),
        files=files,
    )


def summary(**changes) -> FileSummary:
    fields = {
        "file_id": uuid.UUID(int=7),
        "kind": "photos",
        "media_type": "image/png",
        "size_bytes": 1234,
        "sha256": "ab" * 32,
        "received_at": MOMENT,
    }
    return FileSummary(**(fields | changes))


# ── the list ────────────────────────────────────────────────────────────────
def test_a_claim_with_no_files_says_so_in_one_sentence_and_has_no_table(
    fresh_database: DatabaseHandle,
) -> None:
    put_claim(fresh_database)

    response = make_client(fresh_database).get(url_of(CLAIM))

    page = Page(response.text)
    assert response.status_code == 200
    assert HEADING in page.text
    assert response.text.count(NO_FILES) == 1
    assert file_table(page) == []
    assert "SHA-256" not in page.text
    assert SCAN_SENTENCE not in page.text


def test_each_file_is_a_row_with_kind_type_size_time_hash_and_not_scanned(
    fresh_database: DatabaseHandle,
) -> None:
    db = fresh_database
    put_claim(db)
    client = make_client(db)
    assert twin(client, PDF, "police_report").status_code == 303
    assert twin(client, PNG, "photos").status_code == 303
    assert twin(client, JPEG, "other").status_code == 303

    response = client.get(url_of(CLAIM))

    page = Page(response.text)
    (header,) = [r for r in page.rows if "SHA-256" in r]
    assert header.split() == [
        "Kind",
        "Media",
        "type",
        "Size",
        "Received",
        "SHA-256",
        "Scan",
    ]
    rows = file_table(page)
    assert len(rows) == 3
    for row, (kind, media, content) in zip(
        rows,
        [
            ("police_report", "application/pdf", PDF),
            ("photos", "image/png", PNG),
            ("other", "image/jpeg", JPEG),
        ],
        strict=True,
    ):
        digest = hashlib.sha256(content).hexdigest()
        assert row.split()[:2] == [kind, media]
        assert f"{len(content):,} bytes" in row
        assert RECEIVED.search(row)
        assert digest[:12] in row
        assert digest not in row  # the table shows it shortened ...
        assert row.rstrip().endswith(NOT_SCANNED)
        # ... with the whole of it in a title, as text.
        assert digest in [a.get("title") for a in page.attributes("span")]
    assert response.text.count(NOT_SCANNED) == 3
    # The one sentence about scanning is under the table.
    assert response.text.count(SCAN_SENTENCE) == 1
    assert response.text.index(SCAN_SENTENCE) > response.text.rindex(NOT_SCANNED)


def test_the_list_is_only_the_claims_own_files(
    fresh_database: DatabaseHandle,
) -> None:
    db = fresh_database
    put_claim(db)
    put_claim(db, OTHER_CLAIM)
    client = make_client(db)
    twin(client, PDF)
    twin(client, blob(40, 3), "repair_estimate", claim_id=OTHER_CLAIM)

    page = Page(client.get(url_of(CLAIM)).text)

    assert len(file_table(page)) == 1
    assert "repair_estimate" not in page.text


def test_the_page_has_no_link_to_a_file_and_no_file_identifier(
    fresh_database: DatabaseHandle,
) -> None:
    db = fresh_database
    put_claim(db)
    client = make_client(db)
    twin(client, PDF)
    twin(client, PNG)
    ids = [
        row[0] for row in owner_rows(db, "SELECT file_id::text FROM claims.claim_files")
    ]
    assert len(ids) == 2

    response = client.get(url_of(CLAIM))

    page = Page(response.text)
    for file_id in ids:
        assert file_id not in response.text
    assert "files" not in " ".join(page.links())
    assert not [h for h in page.links() if "file" in h]
    # Every attribute of every tag, not only the links: no path under files.
    attributes = [str(v) for _, attrs in page.tags for v in attrs.values()]
    assert not [v for v in attributes if "/files" in v]
    assert not [a for a in page.attributes("a") if "download" in a]


def test_the_page_is_the_pages_own_with_the_pages_headers(
    fresh_database: DatabaseHandle,
) -> None:
    put_claim(fresh_database)
    client = make_client(fresh_database)
    twin(client, PDF)

    response = client.get(url_of(CLAIM))

    assert {k: response.headers.get(k) for k in SECURITY_HEADERS} == SECURITY_HEADERS


def test_the_list_is_shown_with_uploads_off_for_files_that_were_stored_before(
    fresh_database: DatabaseHandle,
) -> None:
    db = fresh_database
    put_claim(db)
    twin(make_client(db), PDF)

    page = Page(make_client(db, enabled=False).get(url_of(CLAIM)).text)

    assert len(file_table(page)) == 1


def test_a_claim_of_another_tenant_is_the_404_page_and_shows_none_of_its_files(
    fresh_database: DatabaseHandle,
) -> None:
    db = fresh_database
    put_claim(db, OTHER_CLAIM, tenant="evaluation")
    owner_rows(
        db,
        "INSERT INTO claims.claim_files "
        "(file_id, claim_id, kind, media_type, size_bytes, sha256, content) "
        "VALUES (%s, %s, 'photos', 'application/pdf', %s, sha256(%s), %s) "
        "RETURNING 1",
        (uuid.uuid4(), OTHER_CLAIM, len(PDF), PDF, PDF),
    )
    digest = hashlib.sha256(PDF).hexdigest()

    response = make_client(db).get(url_of(OTHER_CLAIM))

    assert response.status_code == 404
    assert digest[:12] not in response.text
    assert HEADING not in response.text


# ── what the template is given is escaped ───────────────────────────────────
def test_a_kind_and_a_type_are_escaped_whatever_they_hold() -> None:
    """The table's CHECKs refuse markup, so a stored one cannot carry any; the
    template must not depend on that."""
    html = adjuster.render_claim(claim_view((summary(kind=MARKUP, media_type=MARKUP),)))

    assert MARKUP not in html
    assert ESCAPED_MARKUP in html


def test_the_hash_is_shortened_for_display_and_whole_in_a_title() -> None:
    digest = "0123456789abcdef" * 4

    html = adjuster.render_claim(claim_view((summary(sha256=digest),)))

    assert f'title="{digest}"' in html
    assert digest[:12] in html
    assert html.count(digest) == 1  # once, in the title


def test_the_list_of_a_view_holds_no_identifier_and_no_link() -> None:
    html = adjuster.render_claim(claim_view((summary(file_id=uuid.UUID(int=99)),)))

    assert str(uuid.UUID(int=99)) not in html
    assert "/files" not in html
    assert NOT_SCANNED in html


# ── the trail ───────────────────────────────────────────────────────────────
def test_a_stored_file_is_a_row_of_the_trail_like_any_other_event(
    fresh_database: DatabaseHandle, caplog: pytest.LogCaptureFixture
) -> None:
    db = fresh_database
    put_claim(db)
    client = make_client(db)

    with caplog.at_level(logging.DEBUG):
        response = twin(
            client,
            PDF + CANARY_BYTES,
            "police_report",
            filename=CANARY_NAME,
            declared=CANARY_TYPE,
        )
    assert response.status_code == 303
    page_response = client.get(url_of(CLAIM))

    page = Page(page_response.text)
    # The event as the trail writes every event: the role, the service, the
    # event's own name, its outcome and its reason.
    assert trail_cells(page, "claim.file_stored")[-5:] == [
        "claims_api",
        "claims-api",
        "claim.file_stored",
        "stored",
        "uploaded",
    ]
    # No hash, no size, no kind, no name, no type in the trail's row.
    row = next(r for r in page.rows if "claim.file_stored" in r)
    digest = hashlib.sha256(PDF + CANARY_BYTES).hexdigest()
    for text in (digest[:12], "police_report", CANARY_NAME, CANARY_TYPE, "bytes"):
        assert text not in row
    assert CANARY_NAME not in page_response.text + caplog.text


def test_two_stored_files_are_two_rows_of_the_trail(
    fresh_database: DatabaseHandle,
) -> None:
    db = fresh_database
    put_claim(db)
    client = make_client(db)
    twin(client, PDF)
    twin(client, PNG)

    page = Page(client.get(url_of(CLAIM)).text)

    assert len([r for r in page.rows if "claim.file_stored" in r.split()]) == 2
