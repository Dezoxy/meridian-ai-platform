"""The adjuster's download of a stored file (S070 F4b, T-38, H-2).

``GET`` and ``HEAD`` ``/adjuster/claims/{claim_id}/files/{file_id}``, with the
switch on. The bytes are served as stored, as an attachment, under a policy of
the download's own that sandboxes them; the type is the stored one and nothing
the uploader sent reaches a header, a page, a log or an audit row. A GET writes
one audit row, committed before a byte is sent; a HEAD, a 404 and a refusal
write none. Three of these tests are the ones a mutation must turn red: the
sandbox, the tenant in the lookup and the order of the audit write.
"""

import json
import logging
import re
import uuid
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
    NO_SUCH_CLAIM,
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
    owner_execute,
    put_claim,
    upload,
)

from meridian.platform.common.telemetry import make_tracer_provider
from meridian.workloads.claims_triage.app import create_app
from meridian.workloads.claims_triage.file_download import (
    DOWNLOAD_EVENT,
    DOWNLOAD_POLICY,
    DOWNLOAD_REASON,
)
from meridian.workloads.claims_triage.settings import ClaimsSettings

TENANT = "claims-triage"
SAMPLES = REPO_ROOT / "data" / "synthetic" / "upload-samples"
POLICY = "content-security-policy"
EXTENSIONS = {"application/pdf": "pdf", "image/jpeg": "jpg", "image/png": "png"}
NOT_FOUND_PAGE = "no such claim"


def accepted_samples() -> list[Path]:
    manifest = json.loads((SAMPLES / "manifest.json").read_text(encoding="utf-8"))
    return [SAMPLES / f["name"] for f in manifest["files"] if f["upload_status"] == 201]


def download_client(
    db: DatabaseHandle | None,
    *,
    downloads: bool = True,
    exporter: InMemorySpanExporter | None = None,
) -> TestClient:
    app = create_app(
        ClaimsSettings(
            runtime_url="http://runtime.invalid",
            database_url=(
                "postgresql://claims_api@db.invalid/meridian"
                if db is None
                else db.dsn("claims_api")
            ),
            tenant=TENANT,
            uploads_enabled=True,
            downloads_enabled=downloads,
        ),
        tracer_provider=make_tracer_provider("claims-api", exporter),
        http_client=NoRuntime().client,
    )
    return TestClient(app, raise_server_exceptions=False)


def link(claim_id: str, file_id: str) -> str:
    return f"/adjuster/claims/{claim_id}/files/{file_id}"


def stored(
    client: TestClient, data: bytes, claim_id: str = CLAIM, **names: str
) -> dict:
    response = upload(client, data, claim_id=claim_id, **names)
    assert response.status_code == 201, response.text
    return response.json()


def headers_of(response) -> dict[str, str]:
    """The response's headers by lower-case name, without the ones a server adds
    on its own."""
    return {k: v for k, v in response.headers.items() if k not in {"date", "server"}}


def download_rows(db: DatabaseHandle) -> list[tuple]:
    return [r for r in audit_rows(db) if r[0] == DOWNLOAD_EVENT]


# ── every sample, byte for byte, with every header ──────────────────────────
@pytest.mark.parametrize("path", accepted_samples(), ids=lambda p: p.name)
def test_each_sample_is_served_byte_for_byte_with_every_header_of_the_policy(
    fresh_database: DatabaseHandle, path: Path
) -> None:
    db = fresh_database
    put_claim(db)
    client = download_client(db)
    data = path.read_bytes()
    body = stored(client, data, filename="evidence.html", declared="text/html")

    response = client.get(link(CLAIM, body["file_id"]))

    assert response.status_code == 200
    assert response.content == data
    extension = EXTENSIONS[body["media_type"]]
    name = f'attachment; filename="{CLAIM}-{body["file_id"]}.{extension}"'
    assert re.fullmatch(
        r'attachment; filename="CLM-[0-9]{4}-[0-9a-f-]{36}\.(pdf|jpg|png)"', name
    )
    seen = headers_of(response)
    print(f"{path.name}: {seen}")  # the report shows the headers as a test saw them
    assert seen == {
        "content-type": body["media_type"],
        "content-disposition": name,
        "content-length": str(len(data)),
        "x-content-type-options": "nosniff",
        "cache-control": "no-store",
        "cross-origin-resource-policy": "same-origin",
        "x-frame-options": "DENY",
        "referrer-policy": "same-origin",
        POLICY: "default-src 'none'; frame-ancestors 'none'; sandbox",
    }
    assert "filename*" not in response.headers["content-disposition"]
    assert "allow-" not in response.headers[POLICY]
    assert response.headers[POLICY] == DOWNLOAD_POLICY != SECURITY_HEADERS[POLICY]


def test_a_pdf_whose_body_is_html_is_served_as_a_pdf_attachment_in_a_sandbox(
    fresh_database: DatabaseHandle,
) -> None:
    # The mutation "the sandbox policy removed" turns this red.
    db = fresh_database
    put_claim(db)
    client = download_client(db)
    html = b"%PDF-1.4\n<html><script>alert(document.domain)</script></html>"
    body = stored(client, html, filename="page.html", declared="text/html")

    response = client.get(link(CLAIM, body["file_id"]))

    assert response.content == html
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["content-disposition"].startswith("attachment;")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers[POLICY].endswith("; sandbox")
    assert response.headers[POLICY] != SECURITY_HEADERS[POLICY]


def test_the_stored_type_is_the_one_served_whatever_was_declared(
    fresh_database: DatabaseHandle,
) -> None:
    db = fresh_database
    put_claim(db)
    client = download_client(db)
    body = stored(client, PNG, filename="x.pdf", declared="application/pdf")

    response = client.get(link(CLAIM, body["file_id"]))

    assert body["media_type"] == "image/png"
    assert response.headers["content-type"] == "image/png"
    assert response.headers["content-disposition"].endswith('.png"')


def test_a_stored_type_outside_the_closed_list_is_the_pages_500_and_no_bytes(
    fresh_database: DatabaseHandle,
) -> None:
    db = fresh_database
    put_claim(db)
    client = download_client(db)
    body = stored(client, PDF)
    # The table's CHECK would refuse another type: the closed list is also the
    # route's, for a table that someone widened.
    owner_execute(
        db,
        "ALTER TABLE claims.claim_files "
        "DROP CONSTRAINT claim_files_media_type_is_known",
    )
    owner_rows(
        db,
        "UPDATE claims.claim_files SET media_type = 'text/html' "
        "WHERE file_id = %s RETURNING 1",
        (body["file_id"],),
    )

    response = client.get(link(CLAIM, body["file_id"]))

    assert response.status_code == 500
    assert "internal error" in response.text
    assert PDF not in response.content
    assert response.headers[POLICY] == SECURITY_HEADERS[POLICY]
    assert download_rows(db) == []


# ── one answer for everything that is not a file of the tenant's claim ──────
def test_unknown_claim_other_tenant_unknown_file_and_malformed_ids_are_one_404(
    fresh_database: DatabaseHandle,
) -> None:
    # The mutation "the tenant check removed from the lookup" turns the
    # other-tenant case red.
    db = fresh_database
    put_claim(db)
    put_claim(db, OTHER_CLAIM, tenant="another-tenant")
    client = download_client(db)
    mine = stored(client, PDF)
    own_file = mine["file_id"]
    with_other = owner_rows(
        db,
        "INSERT INTO claims.claim_files (file_id, claim_id, kind, media_type, "
        "size_bytes, sha256, content) VALUES (%s, %s, 'photos', 'application/pdf', "
        "%s, sha256(%s::bytea), %s) RETURNING file_id",
        (uuid.uuid4(), OTHER_CLAIM, len(PDF), PDF, PDF),
    )
    foreign_file = str(with_other[0][0])
    pages_own = client.get(url_of("CLM-0404"))
    assert pages_own.status_code == 404
    assert NO_SUCH_CLAIM in pages_own.text

    answers = {
        "unknown claim": client.get(link("CLM-0404", own_file)),
        "other tenant's claim": client.get(link(OTHER_CLAIM, foreign_file)),
        "unknown file": client.get(link(CLAIM, str(uuid.uuid4()))),
        "file of another claim": client.get(link(OTHER_CLAIM, own_file)),
        "file id of the other tenant on my claim": client.get(
            link(CLAIM, foreign_file)
        ),
        "malformed file id": client.get(link(CLAIM, "not-a-uuid")),
        "uppercase file id": client.get(link(CLAIM, own_file.upper())),
        "urn form": client.get(link(CLAIM, "urn:uuid:" + own_file)),
        "no hyphens": client.get(link(CLAIM, own_file.replace("-", ""))),
        "malformed claim id": client.get(link("CLM-12", own_file)),
        "lowercase claim id": client.get(link("clm-9301", own_file)),
    }

    for what, response in answers.items():
        assert response.status_code == 404, what
        assert response.text == answers["unknown claim"].text, what
        assert response.headers["content-type"].startswith("text/html"), what
        assert response.headers[POLICY] == SECURITY_HEADERS[POLICY], what
        assert PDF not in response.content, what
    assert NO_SUCH_CLAIM in answers["unknown claim"].text
    assert NOT_FOUND_PAGE == NO_SUCH_CLAIM
    assert download_rows(db) == []


def test_a_path_with_an_encoded_character_is_the_frameworks_404_before_any_read(
    fresh_database: DatabaseHandle,
) -> None:
    db = fresh_database
    put_claim(db)
    client = download_client(db)
    body = stored(client, PDF)

    for path in (
        f"/adjuster/claims/CLM-%39301/files/{body['file_id']}",
        f"/adjuster/claims/{CLAIM}/files/{body['file_id'][:-1]}%32",
        link(CLAIM, "{" + body["file_id"] + "}"),
    ):
        response = client.get(path)
        assert response.status_code == 404
        assert response.json() == {"detail": "Not Found"}
    assert download_rows(db) == []


# ── HEAD ────────────────────────────────────────────────────────────────────
def test_a_head_sends_the_same_headers_as_the_get_and_no_body_and_no_audit_row(
    fresh_database: DatabaseHandle,
) -> None:
    db = fresh_database
    put_claim(db)
    client = download_client(db)
    body = stored(client, JPEG)
    url = link(CLAIM, body["file_id"])

    head = client.head(url)
    before = download_rows(db)
    got = client.get(url)

    assert before == []
    assert head.status_code == 200
    assert head.content == b""
    assert headers_of(head) == headers_of(got)
    assert head.headers["content-length"] == str(len(JPEG))
    assert head.headers[POLICY] == DOWNLOAD_POLICY
    assert len(download_rows(db)) == 1


def test_a_head_of_a_file_that_is_not_there_is_the_same_404_without_a_body_of_bytes(
    fresh_database: DatabaseHandle,
) -> None:
    db = fresh_database
    put_claim(db)
    client = download_client(db)

    head = client.head(link(CLAIM, str(uuid.uuid4())))
    get = client.get(link(CLAIM, str(uuid.uuid4())))

    assert head.status_code == get.status_code == 404
    assert head.headers[POLICY] == SECURITY_HEADERS[POLICY]
    assert download_rows(db) == []


# ── the audit row ───────────────────────────────────────────────────────────
def test_a_get_writes_one_row_with_the_claim_and_the_tenant_and_nothing_of_the_file(
    fresh_database: DatabaseHandle,
) -> None:
    db = fresh_database
    put_claim(db)
    client = download_client(db)
    body = stored(client, PDF)

    client.get(link(CLAIM, body["file_id"]))
    client.get(link(CLAIM, body["file_id"]))

    rows = download_rows(db)
    assert len(rows) == 2
    event, outcome, reason, tenant, reference, run_id, db_role = rows[0]
    assert (event, outcome, reason) == (DOWNLOAD_EVENT, "served", DOWNLOAD_REASON)
    assert (tenant, reference, run_id, db_role) == (TENANT, CLAIM, None, "claims_api")
    # The model has no column for a file: no identifier, size or hash anywhere.
    text = str(
        owner_rows(
            db,
            "SELECT e::text FROM audit.events AS e WHERE event = %s",
            (DOWNLOAD_EVENT,),
        )
    )
    assert body["file_id"] not in text
    assert body["sha256"] not in text
    # The adjuster's page counts the rows in one line and does not list them (F4d).
    page = Page(download_client(db).get(url_of(CLAIM)).text)
    assert "Downloads of this claim's files: 2, the latest at" in page.text
    assert not [row for row in page.rows if DOWNLOAD_EVENT in row]


def test_the_row_is_committed_before_the_first_byte_is_sent(
    fresh_database: DatabaseHandle,
) -> None:
    # The mutation "the audit write moved after the response" turns this red:
    # a failing audit write must leave the caller with no byte of the file.
    db = fresh_database
    put_claim(db)
    client = download_client(db)
    body = stored(client, PDF + CANARY_BYTES)
    owner_execute(db, "REVOKE INSERT ON audit.events FROM claims_api")

    response = client.get(link(CLAIM, body["file_id"]))

    assert response.status_code == 503
    assert response.text.count(CANARY_BYTES.decode()) == 0
    assert len(response.content) < len(PDF + CANARY_BYTES) * 100
    assert b"%PDF-" not in response.content
    assert "audit log is unavailable" in response.text
    assert "content-disposition" not in response.headers
    assert response.headers[POLICY] == SECURITY_HEADERS[POLICY]


def test_a_stream_of_the_503_yields_none_of_the_files_bytes(
    fresh_database: DatabaseHandle,
) -> None:
    db = fresh_database
    put_claim(db)
    client = download_client(db)
    body = stored(client, PDF + CANARY_BYTES)
    owner_execute(db, "REVOKE INSERT ON audit.events FROM claims_api")

    with client.stream("GET", link(CLAIM, body["file_id"])) as response:
        chunks = list(response.iter_bytes())

    assert response.status_code == 503
    assert sum(chunk.count(CANARY_BYTES) for chunk in chunks) == 0


def test_a_database_that_cannot_be_read_is_the_pages_503_and_no_row() -> None:
    response = download_client(None).get(link(CLAIM, str(uuid.uuid4())))

    assert response.status_code == 503
    assert "database is unavailable" in response.text
    assert response.headers[POLICY] == SECURITY_HEADERS[POLICY]


# ── nothing the uploader sent comes back ────────────────────────────────────
def test_a_canary_name_and_declared_type_appear_in_no_header_page_log_or_row(
    fresh_database: DatabaseHandle, caplog: pytest.LogCaptureFixture
) -> None:
    db = fresh_database
    put_claim(db)
    exporter = InMemorySpanExporter()
    client = download_client(db, exporter=exporter)
    body = stored(client, PDF, filename=CANARY_NAME, declared=CANARY_TYPE)
    exporter.clear()

    with caplog.at_level(logging.DEBUG):
        get = client.get(link(CLAIM, body["file_id"]))
        head = client.head(link(CLAIM, body["file_id"]))
        missing = client.get(link(CLAIM, str(uuid.uuid4())))
        listing = client.get(url_of(CLAIM))

    assert get.status_code == head.status_code == 200
    canaries = (CANARY_NAME, CANARY_TYPE)
    seen = " ".join(
        [
            *(f"{k}: {v}" for r in (get, head, missing) for k, v in r.headers.items()),
            missing.text,
            listing.text,
            caplog.text,
            str(owner_rows(db, "SELECT e::text FROM audit.events AS e")),
        ]
    )
    for canary in canaries:
        assert canary not in seen
    for canary in canaries:
        assert_spans_hold_no_exception_and_no_canary(exporter, canary)
    # The route's own span names the claim and the tenant, never the file. (The
    # framework's server span holds the request's URL, as for every route, and
    # the URL holds the identifier.)
    own = [
        str(dict(s.attributes))
        for s in exporter.get_finished_spans()
        if s.name == "claims.adjuster.download"
    ]
    assert len(own) == 3  # the GET, the HEAD and the GET of the file not there
    assert all(body["file_id"] not in text for text in own)


# ── the list's links, with the switch on and off ────────────────────────────
def test_the_adjusters_list_links_each_file_when_downloads_are_on(
    fresh_database: DatabaseHandle,
) -> None:
    db = fresh_database
    put_claim(db)
    client = download_client(db)
    first = stored(client, PDF)
    second = stored(client, PNG)

    page = Page(client.get(url_of(CLAIM)).text)

    hrefs = [h for h in page.links() if "/files/" in h]
    assert hrefs == [link(CLAIM, first["file_id"]), link(CLAIM, second["file_id"])]
    assert {a["rel"] for a in page.attributes("a") if "/files/" in str(a["href"])} == {
        "noopener noreferrer"
    }
    # The link works from the page: the policy and the bytes.
    assert client.get(hrefs[1]).content == PNG


def test_the_adjusters_list_has_no_link_and_no_identifier_when_they_are_off(
    fresh_database: DatabaseHandle,
) -> None:
    db = fresh_database
    put_claim(db)
    on = download_client(db)
    off = download_client(db, downloads=False)
    body = stored(on, PDF)

    page = off.get(url_of(CLAIM))

    assert body["file_id"] not in page.text
    assert not [h for h in Page(page.text).links() if "/files" in h]
    assert off.get(link(CLAIM, body["file_id"])).status_code == 404


def test_the_claimants_pages_never_show_a_link_or_an_identifier(
    fresh_database: DatabaseHandle,
) -> None:
    db = fresh_database
    put_claim(db)
    client = download_client(db)
    body = stored(client, PDF)

    status = client.get(f"/claimant/claims/{CLAIM}")
    start = client.get("/claimant/claims")

    for response in (status, start):
        assert response.status_code == 200
        assert body["file_id"] not in response.text
        assert not [h for h in Page(response.text).links() if "/files/" in h]
        assert "/adjuster/claims" not in response.text
    # The route's path under the claimant's own prefix is no route.
    under_theirs = client.get(f"/claimant/claims/{CLAIM}/files/{body['file_id']}")
    assert under_theirs.status_code == 404
    assert PDF not in under_theirs.content


def test_the_download_route_does_not_accept_a_post_a_put_or_a_delete(
    fresh_database: DatabaseHandle,
) -> None:
    db = fresh_database
    put_claim(db)
    client = download_client(db)
    body = stored(client, PDF)
    url = link(CLAIM, body["file_id"])

    for method in ("POST", "PUT", "DELETE", "PATCH"):
        response = client.request(method, url)
        assert response.status_code == 405, method
        assert PDF not in response.content
    assert download_rows(db) == []
