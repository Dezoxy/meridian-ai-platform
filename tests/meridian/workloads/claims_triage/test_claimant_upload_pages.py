"""The claimant's upload form and its HTML twin (S070 F4a): the form on the status
page when uploads are on, a file stored through ``POST
/claimant/claims/{claim_id}/files`` and listed back to the claimant as kind, size
and received time only, and each refusal as the claimant's error page with the
route's fixed sentence and its status. The twin runs the JSON route's code: the
tests here pin that the results are the same, and that nothing of a file but its
bytes is kept or shown (T-03).
"""

import hashlib
import html
import logging
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from dbsupport import DatabaseHandle
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from servicesupport import (
    REPO_ROOT,
    assert_spans_hold_no_exception_and_no_canary,
    owner_rows,
)
from workloads.claims_triage.test_adjuster_pages import (
    ESCAPED_MARKUP,
    MARKUP,
    SECURITY_HEADERS,
    Page,
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
    NoRuntime,
    audit_rows,
    blob,
    claim_world,
    file_count,
    make_client,
    nothing_stored,
    owner_execute,
    put_claim,
    stored_files,
)

from meridian.platform.common import db as db_module
from meridian.platform.common.db import connect
from meridian.workloads.claims_triage import claimant
from meridian.workloads.claims_triage.claim_files import FileSummary
from meridian.workloads.claims_triage.lifecycle import LifecycleState
from meridian.workloads.claims_triage.uploads import (
    CLAIM_BYTES_DETAIL,
    CLAIM_FULL_DETAIL,
    CLAIM_LOCK_CLASS,
    CONTENT_TYPE_PROBLEM,
    DUPLICATE_DETAIL,
    EMPTY_PROBLEM,
    FILE_KINDS,
    KIND_PROBLEM,
    LOCK_BUSY_DETAIL,
    MAX_FILE_BYTES,
    MIN_CEILING_ROWS,
    MIN_RATE_PER_MINUTE,
    RATE_DETAIL,
    STORE_FULL_DETAIL,
    STORE_LOCK_CLASS,
    TOO_LARGE_DETAIL,
    UNSUPPORTED_DETAIL,
)

SAMPLES = REPO_ROOT / "data" / "synthetic" / "upload-samples"
STATUS_URL = f"/claimant/claims/{CLAIM}"
TWIN_URL = f"{STATUS_URL}/files"
SAME_ORIGIN = {"Origin": "http://testserver"}
ERROR_PAGE = re.compile(r'<h1>(\d+)</h1>\s*<p role="alert">(.*?)</p>', re.DOTALL)
BANNER = "Synthetic data only: never upload a real document."
LIMITS = (
    "Send one PDF, JPEG or PNG file at a time, at most 1 MiB, "
    "and at most five files for a claim."
)
NO_FILES = "You have not sent a file for this claim."
NO_SUCH_CLAIM = "no such claim"
NO_SUCH_PAGE = "There is no such page."
CROSS_SITE = "the request came from another site"
DATABASE_DOWN = "the database is unavailable"
INTERNAL = "internal error"
STATES: tuple[LifecycleState, ...] = (
    "submitted",
    "triaging",
    "awaiting_adjuster",
    "triage_failed",
    "documents_requested",
    "approved",
    "rejected",
    "withdrawn",
)
SAMPLE_TYPES = {
    "synthetic-document.pdf": "application/pdf",
    "synthetic-photo.jpg": "image/jpeg",
    "synthetic-photo.png": "image/png",
}


def sample(name: str) -> bytes:
    return Path(SAMPLES / name).read_bytes()


def post_file(
    client: TestClient,
    data: bytes,
    *,
    kind: str = "photos",
    claim_id: str = CLAIM,
    filename: str = "evidence.bin",
    declared: str = "application/octet-stream",
    headers: dict[str, str] | None = None,
):
    """What the form posts, as a browser sends it from the page itself."""
    return client.post(
        f"/claimant/claims/{claim_id}/files",
        data={"kind": kind},
        files={"file": (filename, data, declared)},
        headers=SAME_ORIGIN if headers is None else headers,
        follow_redirects=False,
    )


def refusal_of(response) -> tuple[int, str]:
    """The status and sentence of the claimant's error page, which carries the
    pages' headers."""
    found = ERROR_PAGE.search(response.text)
    assert found, f"not the claimant's error page: {response.text[:200]}"
    assert "Back to your claims" in response.text
    assert "Back to the queue" not in response.text
    assert {k: response.headers.get(k) for k in SECURITY_HEADERS} == SECURITY_HEADERS
    assert int(found.group(1)) == response.status_code
    return response.status_code, html.unescape(found.group(2))


def rows_of(page_html: str) -> list[list[str]]:
    """The data rows of the page's tables, each as its cells' text (unescaped),
    header rows out."""
    rows = re.findall(r"<tr>(.*?)</tr>", page_html, re.DOTALL)
    cells = [re.findall(r"<td>(.*?)</td>", row, re.DOTALL) for row in rows]
    return [
        [html.unescape(re.sub(r"<[^>]+>", "", cell)) for cell in row]
        for row in cells
        if row
    ]


# ── the form: only when uploads are on ──────────────────────────────────────
def test_the_status_page_has_the_upload_form_when_uploads_are_on(
    fresh_database: DatabaseHandle,
) -> None:
    put_claim(fresh_database)

    response = make_client(fresh_database).get(STATUS_URL)

    assert response.status_code == 200
    page = Page(response.text)
    (form,) = [f for f in page.attributes("form") if f["action"] == TWIN_URL]
    assert form["method"] == "post"
    assert form["enctype"] == "multipart/form-data"
    (file_input,) = [i for i in page.attributes("input") if i.get("name") == "file"]
    assert file_input["type"] == "file"
    assert [s for s in page.attributes("select") if s.get("name") == "kind"]
    options = [o["value"] for o in page.attributes("option")]
    assert options == list(FILE_KINDS)
    assert [b for b in page.attributes("button") if b.get("type") == "submit"]
    assert BANNER in page.text
    assert LIMITS in page.text
    assert NO_FILES in page.text


def test_the_form_is_the_same_claims_own_form_not_another_claims(
    fresh_database: DatabaseHandle,
) -> None:
    put_claim(fresh_database)
    put_claim(fresh_database, OTHER_CLAIM)

    page = Page(make_client(fresh_database).get(f"/claimant/claims/{OTHER_CLAIM}").text)

    actions = [f["action"] for f in page.attributes("form")]
    assert f"/claimant/claims/{OTHER_CLAIM}/files" in actions
    assert TWIN_URL not in actions


@pytest.mark.parametrize("state", STATES)
def test_the_form_is_shown_in_every_state_a_claim_can_be_in(
    fresh_database: DatabaseHandle, state: LifecycleState
) -> None:
    put_claim(fresh_database, state=state)

    page = Page(make_client(fresh_database).get(STATUS_URL).text)

    assert TWIN_URL in [f["action"] for f in page.attributes("form")]


def test_with_uploads_off_the_page_has_no_form_no_list_and_no_word_of_files(
    fresh_database: DatabaseHandle,
) -> None:
    db = fresh_database
    put_claim(db)
    assert post_file(make_client(db), PDF).status_code == 303

    response = make_client(db, enabled=False).get(STATUS_URL)

    page = Page(response.text)
    assert response.status_code == 200
    assert TWIN_URL not in [f.get("action") for f in page.attributes("form")]
    assert not [i for i in page.attributes("input") if i.get("type") == "file"]
    assert not page.attributes("select")
    for sentence in (BANNER, LIMITS, NO_FILES, "Files you sent", "Send a file"):
        assert sentence not in page.text


# ── a file stored through the twin ──────────────────────────────────────────
@pytest.mark.parametrize("name", sorted(SAMPLE_TYPES))
def test_a_synthetic_sample_is_stored_redirected_and_listed_on_both_pages(
    fresh_database: DatabaseHandle, name: str
) -> None:
    db = fresh_database
    put_claim(db)
    content = sample(name)
    runtime = NoRuntime()
    client = make_client(db, runtime=runtime)
    before = claim_world(db)

    response = post_file(client, content, kind="police_report")

    assert response.status_code == 303
    assert response.headers["location"] == STATUS_URL
    assert {k: response.headers.get(k) for k in SECURITY_HEADERS} == SECURITY_HEADERS
    ((claim_id, kind, media_type, size, digest, stored),) = stored_files(db)
    assert (claim_id, kind, media_type, size) == (
        CLAIM,
        "police_report",
        SAMPLE_TYPES[name],
        len(content),
    )
    assert bytes(digest) == hashlib.sha256(content).digest()
    assert bytes(stored) == content
    assert claim_world(db) == before  # moves nothing, as the JSON route
    assert runtime.requests == []
    assert audit_rows(db)[-1][:3] == ("claim.file_stored", "stored", "uploaded")
    # The claimant's page, as the redirect leads to it: kind, size, received.
    page = client.get(response.headers["location"])
    assert "Files you sent" in Page(page.text).text
    ((kind_cell, size_cell, received_cell),) = rows_of(page.text)
    assert (kind_cell, size_cell) == ("police_report", f"{len(content):,} bytes")
    assert re.fullmatch(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d UTC", received_cell)
    # The adjuster's page lists the same file.
    files_listed = [
        row
        for row in rows_of(client.get(url_of(CLAIM)).text)
        if row[-1] == "not scanned"
    ]
    assert [row[:3] for row in files_listed] == [
        ["police_report", SAMPLE_TYPES[name], f"{len(content):,} bytes"]
    ]


def test_what_the_twin_stores_is_what_the_json_route_stores(
    fresh_database: DatabaseHandle,
) -> None:
    db = fresh_database
    put_claim(db)
    put_claim(db, OTHER_CLAIM)
    client = make_client(db)

    json_answer = client.post(
        f"/claims/{CLAIM}/files",
        data={"kind": "photos"},
        files={"file": ("a.png", PNG, "image/png")},
    )
    twin_answer = post_file(client, PNG, claim_id=OTHER_CLAIM)

    assert json_answer.status_code == 201
    assert twin_answer.status_code == 303
    first, second = stored_files(db)
    # Same kind, type, size and hash: the code is the one.
    assert first[1:5] == second[1:5]
    assert bytes(first[4]) == bytes(second[4])
    assert audit_rows(db)[0][:3] == audit_rows(db)[1][:3]


def test_the_claimant_sees_the_files_of_their_claim_in_arrival_order_only(
    fresh_database: DatabaseHandle,
) -> None:
    db = fresh_database
    put_claim(db)
    put_claim(db, OTHER_CLAIM)
    client = make_client(db)
    post_file(client, PDF, kind="police_report")
    post_file(client, PNG, kind="photos")
    post_file(client, JPEG, kind="other")
    post_file(client, blob(30), kind="repair_estimate", claim_id=OTHER_CLAIM)

    rows = rows_of(client.get(STATUS_URL).text)

    assert [row[0] for row in rows] == ["police_report", "photos", "other"]
    assert [row[1] for row in rows] == [f"{len(x):,} bytes" for x in (PDF, PNG, JPEG)]


def test_the_claimants_list_shows_no_hash_no_identifier_and_no_link(
    fresh_database: DatabaseHandle,
) -> None:
    db = fresh_database
    put_claim(db)
    client = make_client(db)
    post_file(client, PDF)
    ((file_id, digest),) = owner_rows(
        db, "SELECT file_id::text, encode(sha256, 'hex') FROM claims.claim_files"
    )

    response = client.get(STATUS_URL)

    page = Page(response.text)
    assert file_id not in response.text
    assert digest not in response.text
    assert digest[:12] not in response.text
    assert "title" not in {k for _, attrs in page.tags for k in attrs}
    # The links of the page are the nav's, nothing under a files path.
    assert page.links() == ["/claimant/claims"]
    assert "files" not in " ".join(
        str(v) for _, attrs in page.tags for k, v in attrs.items() if k == "href"
    )


def test_the_claimants_page_never_builds_a_row_from_a_hash_or_an_identifier() -> None:
    """The view the page is rendered from holds the file's kind, size and time,
    in that order, and nothing else: the template cannot show what it is not
    given."""
    view = claimant.StatusView(
        claim_id=CLAIM,
        state="awaiting_adjuster",
        received_at=datetime(2026, 10, 1, tzinfo=UTC),
        missing=(),
        arrived=(),
        files=(
            FileSummary(
                uuid.UUID(int=7),
                "photos",
                "image/png",
                1234,
                "ab" * 32,
                datetime(2026, 10, 2, tzinfo=UTC),
            ),
        ),
    )

    page = claimant.render_status(view, uploads=True)

    assert rows_of(page) == [["photos", "1,234 bytes", "2026-10-02 00:00:00 UTC"]]
    assert "ab" * 6 not in page
    assert str(uuid.UUID(int=7)) not in page


def test_a_claimant_page_escapes_a_kind_it_is_given() -> None:
    view = claimant.StatusView(
        claim_id=CLAIM,
        state="awaiting_adjuster",
        received_at=datetime(2026, 10, 1, tzinfo=UTC),
        missing=(),
        arrived=(),
        files=(
            FileSummary(
                uuid.UUID(int=7),
                MARKUP,
                "image/png",
                1,
                "ab" * 32,
                datetime(2026, 10, 2, tzinfo=UTC),
            ),
        ),
    )

    page = claimant.render_status(view, uploads=True)

    assert MARKUP not in page
    assert ESCAPED_MARKUP in page


# ── each refusal is the claimant's page with the route's sentence ───────────
def test_the_refused_sample_is_the_415_page_and_nothing_is_stored(
    fresh_database: DatabaseHandle,
) -> None:
    db = fresh_database
    put_claim(db)
    before = claim_world(db)

    response = post_file(
        make_client(db), sample("not-a-pdf.pdf"), declared="application/pdf"
    )

    assert refusal_of(response) == (415, UNSUPPORTED_DETAIL)
    nothing_stored(db, before)


def test_a_file_of_one_mebibyte_is_stored_and_one_byte_more_is_the_413_page(
    fresh_database: DatabaseHandle,
) -> None:
    db = fresh_database
    put_claim(db)
    client = make_client(db)

    over = post_file(client, blob(MAX_FILE_BYTES + 1))
    exact = post_file(client, blob(MAX_FILE_BYTES))

    assert refusal_of(over) == (413, TOO_LARGE_DETAIL)
    assert exact.status_code == 303
    assert file_count(db) == 1


def test_an_empty_file_is_the_422_page(fresh_database: DatabaseHandle) -> None:
    db = fresh_database
    put_claim(db)
    before = claim_world(db)

    response = post_file(make_client(db), b"")

    assert refusal_of(response) == (422, EMPTY_PROBLEM)
    nothing_stored(db, before)


def test_an_unknown_kind_is_the_422_page_before_the_claim_is_looked_up(
    fresh_database: DatabaseHandle,
) -> None:
    db = fresh_database  # no claim at all
    response = post_file(make_client(db), PDF, kind="invoice")

    assert refusal_of(response) == (422, KIND_PROBLEM)


def test_a_post_that_is_not_multipart_is_the_422_page(
    fresh_database: DatabaseHandle,
) -> None:
    response = make_client(fresh_database).post(
        TWIN_URL,
        content=b"kind=photos",
        headers=SAME_ORIGIN | {"Content-Type": "application/x-www-form-urlencoded"},
        follow_redirects=False,
    )

    assert refusal_of(response) == (422, CONTENT_TYPE_PROBLEM)


def test_an_unknown_claim_and_another_tenants_claim_are_the_same_404_page(
    fresh_database: DatabaseHandle,
) -> None:
    db = fresh_database
    put_claim(db, OTHER_CLAIM, tenant="evaluation")
    client = make_client(db)

    unknown = post_file(client, PDF)
    foreign = post_file(client, PDF, claim_id=OTHER_CLAIM)

    assert refusal_of(unknown) == (404, NO_SUCH_CLAIM)
    assert refusal_of(foreign) == (404, NO_SUCH_CLAIM)
    assert file_count(db) == 0


def test_a_claim_of_five_files_gets_the_409_page_for_a_sixth(
    fresh_database: DatabaseHandle,
) -> None:
    db = fresh_database
    put_claim(db)
    client = make_client(db)
    for salt in range(5):
        assert post_file(client, blob(30, salt)).status_code == 303

    response = post_file(client, blob(30, 9))

    assert refusal_of(response) == (409, CLAIM_FULL_DETAIL)
    assert file_count(db) == 5


def test_a_claim_past_3_mib_gets_the_409_page(fresh_database: DatabaseHandle) -> None:
    db = fresh_database
    put_claim(db)
    client = make_client(db)
    for salt in range(3):
        assert post_file(client, blob(MAX_FILE_BYTES, salt)).status_code == 303

    response = post_file(client, blob(30, 9))

    assert refusal_of(response) == (409, CLAIM_BYTES_DETAIL)


def test_the_same_file_twice_gets_the_409_page(fresh_database: DatabaseHandle) -> None:
    db = fresh_database
    put_claim(db)
    client = make_client(db)
    assert post_file(client, PDF).status_code == 303

    response = post_file(client, PDF, kind="other")

    assert refusal_of(response) == (409, DUPLICATE_DETAIL)
    assert file_count(db) == 1


def test_a_full_store_gets_the_507_page_for_everyone(
    fresh_database: DatabaseHandle,
) -> None:
    db = fresh_database
    put_claim(db)
    put_claim(db, OTHER_CLAIM)
    client = make_client(db, rows=MIN_CEILING_ROWS)
    for salt in range(3):
        assert post_file(client, blob(30, salt)).status_code == 303
    for salt in range(2):
        post = post_file(client, blob(30, salt), claim_id=OTHER_CLAIM)
        assert post.status_code == 303

    response = post_file(client, blob(30, 9), claim_id=OTHER_CLAIM)

    assert refusal_of(response) == (507, STORE_FULL_DETAIL)
    assert file_count(db) == MIN_CEILING_ROWS


def test_a_store_past_its_rate_gets_the_429_page_with_a_wait(
    fresh_database: DatabaseHandle,
) -> None:
    db = fresh_database
    put_claim(db)
    put_claim(db, OTHER_CLAIM)
    client = make_client(db, rate=MIN_RATE_PER_MINUTE)
    for salt in range(3):
        assert post_file(client, blob(30, salt)).status_code == 303
        post = post_file(client, blob(30, salt), claim_id=OTHER_CLAIM)
        assert post.status_code == 303

    response = post_file(client, blob(30, 9))

    assert refusal_of(response) == (429, RATE_DETAIL)
    assert response.headers["Retry-After"] == "60"
    assert file_count(db) == MIN_RATE_PER_MINUTE


@pytest.mark.parametrize("which", ["the claim's lock", "the store's lock"])
def test_a_lock_that_does_not_come_in_time_gets_the_503_page_with_a_wait(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch, which: str
) -> None:
    db = fresh_database
    put_claim(db)
    holder = connect(db.dsn("claims_api"), "test-holder")
    try:
        if which == "the claim's lock":
            holder.execute(
                "SELECT pg_advisory_xact_lock(%s::int, hashtext(%s))",
                (CLAIM_LOCK_CLASS, CLAIM),
            )
        else:
            holder.execute(
                "SELECT pg_advisory_xact_lock(%s::int, 0)", (STORE_LOCK_CLASS,)
            )
        monkeypatch.setattr(db_module, "STATEMENT_TIMEOUT_MS", 300)
        response = post_file(make_client(db), blob(20, 5))
        holder.commit()
    finally:
        holder.close()

    assert refusal_of(response) == (503, LOCK_BUSY_DETAIL)
    assert response.headers["Retry-After"] == "5"
    assert file_count(db) == 0
    assert audit_rows(db) == []


def test_a_database_that_is_down_is_the_503_page_without_a_wait() -> None:
    response = post_file(make_client(), PDF)

    assert refusal_of(response) == (503, DATABASE_DOWN)
    assert "Retry-After" not in response.headers


def test_a_failing_audit_row_is_the_500_page_and_stores_nothing(
    fresh_database: DatabaseHandle,
) -> None:
    db = fresh_database
    put_claim(db)
    owner_execute(db, "REVOKE INSERT ON audit.events FROM claims_api")

    response = post_file(make_client(db), PDF)

    assert refusal_of(response) == (500, INTERNAL)
    assert file_count(db) == 0


def test_a_post_from_another_site_is_the_403_page_and_stores_nothing(
    fresh_database: DatabaseHandle,
) -> None:
    db = fresh_database
    put_claim(db)

    response = post_file(make_client(db), PDF, headers={"Sec-Fetch-Site": "cross-site"})

    assert refusal_of(response) == (403, CROSS_SITE)
    assert file_count(db) == 0


def test_a_refusal_leaves_the_claimants_page_as_it_was(
    fresh_database: DatabaseHandle,
) -> None:
    db = fresh_database
    put_claim(db)
    client = make_client(db)
    before = client.get(STATUS_URL).text

    post_file(client, sample("not-a-pdf.pdf"))

    assert client.get(STATUS_URL).text == before


def test_a_refused_page_says_nothing_about_the_file(
    fresh_database: DatabaseHandle,
) -> None:
    db = fresh_database
    put_claim(db)

    response = post_file(
        make_client(db),
        b"GIF89a" + CANARY_BYTES,
        filename=CANARY_NAME,
        declared=CANARY_TYPE,
    )

    assert response.status_code == 415
    for canary in (CANARY_NAME, CANARY_TYPE, CANARY_BYTES.decode()):
        assert canary not in response.text


# ── the name, the declared type and the bytes appear nowhere ────────────────
@pytest.mark.parametrize(
    ("data", "status"),
    [
        pytest.param(PDF + CANARY_BYTES, 303, id="stored"),
        pytest.param(b"GIF89a" + CANARY_BYTES, 415, id="refused as a type"),
        pytest.param(b"", 422, id="refused as empty"),
    ],
)
def test_no_page_log_span_audit_row_or_answer_holds_a_name_a_type_or_the_bytes(
    fresh_database: DatabaseHandle,
    caplog: pytest.LogCaptureFixture,
    data: bytes,
    status: int,
) -> None:
    db = fresh_database
    put_claim(db)
    exporter = InMemorySpanExporter()
    client = make_client(db, exporter=exporter)
    canaries = (CANARY_NAME, CANARY_TYPE, CANARY_BYTES.decode())

    with caplog.at_level(logging.DEBUG):
        response = post_file(client, data, filename=CANARY_NAME, declared=CANARY_TYPE)
        pages = [
            response.text,
            client.get(STATUS_URL).text,
            client.get(url_of(CLAIM)).text,
        ]

    assert response.status_code == status
    for canary in canaries:
        assert canary not in caplog.text
        assert canary not in " ".join(pages)
        assert_spans_hold_no_exception_and_no_canary(exporter, canary)
        for table in ("audit.events", "claims.claims", "claims.claim_documents"):
            rows = owner_rows(db, f"SELECT t::text FROM {table} AS t")  # noqa: S608
            assert canary not in str(rows), table
    kept = owner_rows(
        db,
        "SELECT file_id, claim_id, kind, media_type, size_bytes, sha256, received_at "
        "FROM claims.claim_files",
    )
    assert CANARY_NAME not in str(kept)
    assert CANARY_TYPE not in str(kept)


def test_a_server_error_logs_no_file_name_and_shows_none(
    fresh_database: DatabaseHandle, caplog: pytest.LogCaptureFixture
) -> None:
    db = fresh_database
    put_claim(db)
    owner_execute(db, "REVOKE INSERT ON audit.events FROM claims_api")

    with caplog.at_level(logging.DEBUG):
        response = post_file(
            make_client(db),
            PDF + CANARY_BYTES,
            filename=CANARY_NAME,
            declared=CANARY_TYPE,
        )

    assert response.status_code == 500
    for canary in (CANARY_NAME, CANARY_TYPE, CANARY_BYTES.decode()):
        assert canary not in caplog.text + response.text
