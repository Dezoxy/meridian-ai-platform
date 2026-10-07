"""``POST /claims/{claim_id}/files``: a claimant's file is stored (S070, T-38).

The order of the route's checks is the contract: the form's shape, the claim,
the file's size, its type from its first bytes, the claim's ceilings, the
global ceiling, then one transaction with the file and one audit row. Every
refusal stores nothing and writes no audit row; an upload moves no claim, makes
no document arrive and calls no runtime. Three of these tests are the ones that
a mutation of the code must turn red, and say so.
"""

import hashlib
import logging
import struct
import tempfile
import time
import uuid
import zlib
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from typing import Any

import psycopg
import pytest
from dbsupport import OWNER, DatabaseHandle
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from psycopg.types.json import Jsonb
from servicesupport import (
    assert_spans_hold_no_exception_and_no_canary,
    claim_with_id,
    multipart_body,
    owner_rows,
)

from meridian.platform.common.db import connect
from meridian.platform.common.telemetry import make_tracer_provider
from meridian.workloads.claims_triage import uploads
from meridian.workloads.claims_triage.app import create_app
from meridian.workloads.claims_triage.claim_files import list_files
from meridian.workloads.claims_triage.settings import ClaimsSettings
from meridian.workloads.claims_triage.uploads import (
    CLAIM_LOCK_CLASS,
    DEFAULT_CEILING_BYTES,
    DEFAULT_CEILING_ROWS,
    DEFAULT_RATE_PER_MINUTE,
    FILE_KINDS,
    MAX_CLAIM_BYTES,
    MAX_FILE_BYTES,
    MAX_FILES_PER_CLAIM,
    MIN_CEILING_BYTES,
    MIN_CEILING_ROWS,
    MIN_RATE_PER_MINUTE,
    STORE_LOCK_CLASS,
    UPLOAD_BODY_LIMIT_BYTES,
    file_content,
)

TENANT = "claims-triage"
CLAIM = "CLM-9301"
OTHER_CLAIM = "CLM-9302"
URL = f"/claims/{CLAIM}/files"
MIB = 1024 * 1024
FILE_EVENT = "claim.file_stored"


def png_bytes(extra: bytes = b"") -> bytes:
    """A real one-pixel PNG, with ``extra`` after its end marker."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(kind + data)
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", crc)

    header = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    pixel = zlib.compress(b"\x00\xff\x00\x00")
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", pixel)
        + chunk(b"IEND", b"")
        + extra
    )


PNG = png_bytes()
# A JPEG's own markers: start of image, a JFIF header, end of image.
JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xd9"
PDF = (
    b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\n"
    b"trailer<</Root 1 0 R>>\n%%EOF\nsynthetic file\n"
)
BY_TYPE = {"application/pdf": PDF, "image/jpeg": JPEG, "image/png": PNG}


def padded(head: bytes, size: int) -> bytes:
    """``head`` followed by zeros, ``size`` bytes in all."""
    assert len(head) <= size
    return head + b"\x00" * (size - len(head))


def blob(size: int, salt: int = 0) -> bytes:
    """A PDF-looking file of ``size`` bytes that differs from every other
    ``salt``: the table refuses the same file twice on one claim."""
    head = b"%PDF-" + salt.to_bytes(2, "big")
    assert len(head) <= size
    return head + b"\x00" * (size - len(head))


def put_claim(
    db: DatabaseHandle,
    claim_id: str = CLAIM,
    state: str = "documents_requested",
    *,
    tenant: str = TENANT,
    triages: int = 2,
    run_id: uuid.UUID | None = None,
) -> None:
    owner_rows(
        db,
        "INSERT INTO claims.claims "
        "(claim_id, tenant, submission, state, run_id, triages) "
        "VALUES (%s, %s, %s, %s, %s, %s) RETURNING 1",
        (claim_id, tenant, Jsonb(claim_with_id(claim_id)), state, run_id, triages),
    )


def owner_execute(db: DatabaseHandle, statement: str) -> None:
    with connect(db.dsn(OWNER), "test-write") as conn:
        conn.execute(statement)


class NoRuntime:
    """A runtime nobody may call: an upload never reaches it."""

    def __init__(self) -> None:
        import httpx

        self.requests: list[Any] = []
        self.client = httpx.Client(
            base_url="http://runtime.invalid",
            transport=httpx.MockTransport(self.refuse),
        )

    def refuse(self, request: Any) -> Any:
        self.requests.append(request)
        raise AssertionError("an upload called the runtime")


def make_client(
    db: DatabaseHandle | None = None,
    *,
    enabled: bool = True,
    ceiling: int = DEFAULT_CEILING_BYTES,
    rows: int = DEFAULT_CEILING_ROWS,
    rate: int = DEFAULT_RATE_PER_MINUTE,
    exporter: InMemorySpanExporter | None = None,
    runtime: NoRuntime | None = None,
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
            uploads_enabled=enabled,
            uploads_ceiling_bytes=ceiling,
            uploads_ceiling_rows=rows,
            uploads_rate_per_minute=rate,
        ),
        tracer_provider=make_tracer_provider("claims-api", exporter),
        http_client=(runtime or NoRuntime()).client,
    )
    return TestClient(app, raise_server_exceptions=False)


def upload(
    client: TestClient,
    data: bytes,
    *,
    kind: str = "photos",
    claim_id: str = CLAIM,
    filename: str = "evidence.bin",
    declared: str = "application/octet-stream",
):
    return client.post(
        f"/claims/{claim_id}/files",
        data={"kind": kind},
        files={"file": (filename, data, declared)},
    )


def stored_files(db: DatabaseHandle) -> list[tuple]:
    return owner_rows(
        db,
        "SELECT claim_id, kind, media_type, size_bytes, sha256, content "
        "FROM claims.claim_files ORDER BY received_at, file_id",
    )


def file_count(db: DatabaseHandle) -> int:
    ((count,),) = owner_rows(db, "SELECT count(*) FROM claims.claim_files")
    return count


def audit_rows(db: DatabaseHandle) -> list[tuple]:
    """Every row the Claims API wrote to the audit log."""
    return owner_rows(
        db,
        "SELECT event, outcome, reason, tenant, reference, run_id, db_role "
        "FROM audit.events WHERE service = 'claims-api' ORDER BY seq",
    )


def claim_world(db: DatabaseHandle) -> list[list[tuple]]:
    """Every row of every table an upload must leave alone, as text."""
    return [
        owner_rows(db, f"SELECT t::text FROM {table} AS t ORDER BY 1")  # noqa: S608
        for table in (
            "claims.claims",
            "claims.claim_documents",
            "claims.triage_proposals",
            "claims.decisions",
            "claims.briefs",
        )
    ]


def nothing_stored(db: DatabaseHandle, before: list[list[tuple]]) -> None:
    assert file_count(db) == 0
    assert audit_rows(db) == []
    assert claim_world(db) == before


def problem_text(response: Any) -> str:
    """What a 422 says: locations, fixed messages and types."""
    return str(response.json())


# ── the happy path: three types, each from its own first bytes ──────────────
@pytest.mark.parametrize("media_type", sorted(BY_TYPE))
def test_a_pdf_a_jpeg_and_a_png_are_stored_with_one_audit_row_and_move_nothing(
    fresh_database: DatabaseHandle, media_type: str
) -> None:
    db = fresh_database
    ended = uuid.uuid4()
    put_claim(db, run_id=ended)
    before = claim_world(db)
    runtime = NoRuntime()
    content = BY_TYPE[media_type]

    response = upload(make_client(db, runtime=runtime), content, kind="police_report")

    assert response.status_code == 201
    body = response.json()
    assert set(body) == {"file_id", "kind", "media_type", "size_bytes", "sha256"}
    assert uuid.UUID(body["file_id"]).version == 4
    assert body["kind"] == "police_report"
    assert body["media_type"] == media_type
    assert body["size_bytes"] == len(content)
    assert body["sha256"] == hashlib.sha256(content).hexdigest()
    ((claim_id, kind, stored_type, size, digest, stored),) = stored_files(db)
    assert (claim_id, kind, stored_type, size) == (
        CLAIM,
        "police_report",
        media_type,
        len(content),
    )
    assert bytes(digest) == hashlib.sha256(content).digest()
    assert bytes(stored) == content
    # One audit row: no content, no name, no run; the size and hash are the
    # file row's, which the role cannot change.
    assert audit_rows(db) == [
        (FILE_EVENT, "stored", "uploaded", TENANT, CLAIM, None, "claims_api")
    ]
    # An upload is not a document arrival: no move, no triage, no name.
    assert claim_world(db) == before
    assert runtime.requests == []
    ((state, run_id, triages),) = owner_rows(
        db, "SELECT state, run_id, triages FROM claims.claims"
    )
    assert (state, run_id, triages) == ("documents_requested", ended, 2)


@pytest.mark.parametrize("state", ["submitted", "triaging", "awaiting_adjuster"])
def test_an_upload_leaves_a_claim_in_any_state_where_it_is(
    fresh_database: DatabaseHandle, state: str
) -> None:
    put_claim(fresh_database, state=state, triages=1)
    before = claim_world(fresh_database)

    response = upload(make_client(fresh_database), PDF, kind="other")

    assert response.status_code == 201
    assert claim_world(fresh_database) == before


@pytest.mark.parametrize("kind", FILE_KINDS)
def test_each_of_the_five_kinds_is_stored(
    fresh_database: DatabaseHandle, kind: str
) -> None:
    put_claim(fresh_database)

    response = upload(make_client(fresh_database), PDF, kind=kind)

    assert response.status_code == 201
    assert [row[1] for row in stored_files(fresh_database)] == [kind]


# ── the type is decided by the first bytes alone ────────────────────────────
# mutation: the sniffing replaced by the declared type
def test_a_png_sent_as_a_pdf_is_stored_as_a_png(
    fresh_database: DatabaseHandle,
) -> None:
    put_claim(fresh_database)

    response = upload(make_client(fresh_database), PNG, declared="application/pdf")

    assert response.status_code == 201
    assert response.json()["media_type"] == "image/png"
    assert [row[2] for row in stored_files(fresh_database)] == ["image/png"]


def test_a_file_that_starts_as_a_png_and_holds_pdf_later_is_a_png(
    fresh_database: DatabaseHandle,
) -> None:
    put_claim(fresh_database)
    polyglot = png_bytes(extra=b"\n%PDF-1.4 hidden\n%%EOF")

    response = upload(make_client(fresh_database), polyglot, declared="application/pdf")

    assert response.status_code == 201
    assert response.json()["media_type"] == "image/png"


@pytest.mark.parametrize(
    "content",
    [
        pytest.param(b" %PDF-1.4", id="a space before the pdf"),
        pytest.param(b"\n%PDF-1.4", id="a line feed before the pdf"),
        pytest.param(b"\xef\xbb\xbf%PDF-1.4", id="a byte order mark before the pdf"),
        pytest.param(b"%PDF", id="the pdf signature one byte short"),
        pytest.param(b"%pdf-1.4", id="the pdf signature in lower case"),
        pytest.param(b"\xff\xd8", id="the jpeg signature one byte short"),
        pytest.param(b"\xd8\xff\xe0", id="the jpeg signature one byte late"),
        pytest.param(PNG[:7], id="the png signature one byte short"),
        pytest.param(b"\x89PNG\r\n\x1a\r", id="a png signature that is not one"),
        pytest.param(b"GIF89a\x01\x00", id="a gif"),
        pytest.param(b"PK\x03\x04", id="a zip"),
        pytest.param(b"<html><script>alert(1)</script>", id="html"),
        pytest.param(b"MZ\x90\x00", id="a program"),
        pytest.param(b"x", id="one byte of anything"),
    ],
)
def test_a_file_that_is_none_of_the_three_types_is_415_and_stores_nothing(
    fresh_database: DatabaseHandle, content: bytes
) -> None:
    put_claim(fresh_database)
    before = claim_world(fresh_database)

    # Declared as a PDF: what the client says is no help.
    response = upload(make_client(fresh_database), content, declared="application/pdf")

    assert response.status_code == 415
    assert response.json() == {"detail": "the file is not a PDF, a JPEG or a PNG"}
    nothing_stored(fresh_database, before)


# ── the form's shape, checked before anything is looked up ──────────────────
def kind_part(value: bytes = b"photos") -> tuple[str, None, None, bytes]:
    return ("kind", None, None, value)


def file_part(
    name: str = "file", data: bytes = PDF, filename: str | None = "f.pdf"
) -> tuple[str, str | None, str | None, bytes]:
    return (name, filename, "application/pdf", data)


@pytest.mark.parametrize(
    "parts",
    [
        pytest.param([kind_part(), file_part(), file_part()], id="a second file"),
        pytest.param(
            [kind_part(), file_part(), file_part(name="other")],
            id="a second file under another name",
        ),
        pytest.param([kind_part(), kind_part(), file_part()], id="kind twice"),
        pytest.param(
            [kind_part(), ("note", None, None, b"x"), file_part()],
            id="an unknown field",
        ),
        pytest.param([kind_part()], id="no file"),
        pytest.param([file_part()], id="no kind"),
        pytest.param(
            [kind_part(), file_part(name="upload")], id="a file part named otherwise"
        ),
        pytest.param(
            [kind_part(), ("file", None, None, PDF)], id="a file part with no file name"
        ),
        pytest.param(
            [("note", None, None, b"photos"), file_part()],
            id="the kind field under another name",
        ),
        pytest.param([kind_part(b"invoice"), file_part()], id="an unknown kind"),
        pytest.param([kind_part(b"Photos"), file_part()], id="a kind in capitals"),
        pytest.param([kind_part(b""), file_part()], id="an empty kind"),
        pytest.param([kind_part(b"photos "), file_part()], id="a kind with a space"),
        pytest.param([], id="no part at all"),
    ],
)
def test_a_form_that_is_not_one_kind_and_one_file_is_422_and_stores_nothing(
    fresh_database: DatabaseHandle, parts: list
) -> None:
    put_claim(fresh_database)
    before = claim_world(fresh_database)
    body, headers = multipart_body(parts)

    response = make_client(fresh_database).post(URL, content=body, headers=headers)

    assert response.status_code == 422
    assert set(response.json()) == {"detail"}
    nothing_stored(fresh_database, before)


@pytest.mark.parametrize(
    ("content_type", "content"),
    [
        pytest.param("application/json", b'{"kind": "photos"}', id="json"),
        pytest.param("application/x-www-form-urlencoded", b"kind=photos", id="form"),
        pytest.param("text/plain", b"kind=photos", id="text"),
        pytest.param("multipart/form-data", b"--b\r\n", id="no boundary"),
        pytest.param("multipart/mixed; boundary=b", b"--b--\r\n", id="mixed"),
    ],
)
def test_a_body_that_is_not_multipart_form_data_is_422_and_stores_nothing(
    fresh_database: DatabaseHandle, content_type: str, content: bytes
) -> None:
    put_claim(fresh_database)
    before = claim_world(fresh_database)

    response = make_client(fresh_database).post(
        URL, content=content, headers={"Content-Type": content_type}
    )

    assert response.status_code == 422
    nothing_stored(fresh_database, before)


def test_the_form_is_checked_before_the_claim_is_looked_up(
    fresh_database: DatabaseHandle,
) -> None:
    # No claim CLM-9301: a bad form is still the form's 422, not a 404.
    body, headers = multipart_body([kind_part(b"invoice"), file_part()])

    response = make_client(fresh_database).post(URL, content=body, headers=headers)

    assert response.status_code == 422


def test_a_422_names_where_and_what_never_the_input(
    fresh_database: DatabaseHandle,
) -> None:
    put_claim(fresh_database)
    body, headers = multipart_body(
        [
            ("canary-field-4471", None, None, b"canary-value-4471"),
            kind_part(b"canary-kind-4471"),
            file_part(filename="canary-name-4471.pdf"),
        ]
    )

    response = make_client(fresh_database).post(URL, content=body, headers=headers)

    assert response.status_code == 422
    assert "4471" not in response.text
    (problem,) = response.json()["detail"]
    assert set(problem) == {"loc", "msg", "type"}


# ── the claim: its own, the deployment's tenant's ───────────────────────────
def test_an_unknown_claim_is_the_404_the_documents_route_gives(
    fresh_database: DatabaseHandle,
) -> None:
    client = make_client(fresh_database)
    documents = client.post(
        f"/claims/{OTHER_CLAIM}/documents", json={"documents": ["photos"]}
    )

    response = upload(client, PDF, claim_id=OTHER_CLAIM)

    assert response.status_code == documents.status_code == 404
    assert response.json() == documents.json()
    nothing_stored(fresh_database, claim_world(fresh_database))


def test_a_claim_of_another_tenant_is_the_same_404_and_stores_nothing(
    fresh_database: DatabaseHandle,
) -> None:
    db = fresh_database
    put_claim(db, OTHER_CLAIM, tenant="evaluation")
    client = make_client(db)
    unknown = upload(client, PDF, claim_id="CLM-9399")
    before = claim_world(db)

    response = upload(client, PDF, claim_id=OTHER_CLAIM)

    assert response.status_code == 404
    assert response.json() == unknown.json()
    nothing_stored(db, before)


@pytest.mark.parametrize("claim_id", ["CLM-1", "clm-9301", "CLM-93011", "x"])
def test_a_claim_id_that_is_not_one_is_422(
    fresh_database: DatabaseHandle, claim_id: str
) -> None:
    response = upload(make_client(fresh_database), PDF, claim_id=claim_id)

    assert response.status_code == 422


def test_a_file_for_an_unknown_claim_is_a_404_before_its_size_or_type_is_judged(
    fresh_database: DatabaseHandle,
) -> None:
    client = make_client(fresh_database)

    empty = upload(client, b"", claim_id=OTHER_CLAIM)
    large = upload(client, padded(PDF, MAX_FILE_BYTES + 1), claim_id=OTHER_CLAIM)
    unsupported = upload(client, b"GIF89a", claim_id=OTHER_CLAIM)

    assert [r.status_code for r in (empty, large, unsupported)] == [404] * 3


# ── the size: 1 byte to 1 MiB ───────────────────────────────────────────────
def test_an_empty_file_is_422_and_stores_nothing(
    fresh_database: DatabaseHandle,
) -> None:
    put_claim(fresh_database)
    before = claim_world(fresh_database)

    response = upload(make_client(fresh_database), b"")

    assert response.status_code == 422
    nothing_stored(fresh_database, before)


def test_a_file_of_one_mebibyte_is_stored_and_one_byte_more_is_413(
    fresh_database: DatabaseHandle,
) -> None:
    db = fresh_database
    put_claim(db)
    client = make_client(db)
    before = claim_world(db)

    over = upload(client, padded(PDF, MAX_FILE_BYTES + 1))

    assert over.status_code == 413
    nothing_stored(db, before)
    exact = upload(client, padded(PDF, MAX_FILE_BYTES))
    assert exact.status_code == 201
    assert exact.json()["size_bytes"] == MAX_FILE_BYTES
    assert [row[3] for row in stored_files(db)] == [MAX_FILE_BYTES]


def test_the_size_is_judged_before_the_type(fresh_database: DatabaseHandle) -> None:
    put_claim(fresh_database)

    response = upload(make_client(fresh_database), b"G" * (MAX_FILE_BYTES + 1))

    assert response.status_code == 413


# ── the form parser never spools to disk ────────────────────────────────────
def refuse_a_temporary_file(calls: list[str]) -> Any:
    def refuse(*args: Any, **kwargs: Any) -> Any:
        calls.append("a temporary file was asked for")
        raise OSError("the pod's /tmp is 16 Mi: no upload may touch it")

    return refuse


def test_a_body_at_the_routes_limit_makes_no_file_under_the_temporary_directory(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The worst case the body limit lets through: the smallest envelope and a
    file part that takes the rest, which is over the 1 MiB at which a form
    parser rolls a part over to a file on disk. A claim exists, so the route
    gets as far as it can."""
    put_claim(fresh_database)
    asked: list[str] = []
    for factory in ("TemporaryFile", "NamedTemporaryFile", "mkstemp"):
        monkeypatch.setattr(tempfile, factory, refuse_a_temporary_file(asked))
    envelope = len(multipart_body([kind_part(), file_part(data=b"")])[0])
    data = padded(PDF, UPLOAD_BODY_LIMIT_BYTES - envelope)
    body, headers = multipart_body([kind_part(), file_part(data=data)])
    assert len(body) == UPLOAD_BODY_LIMIT_BYTES
    assert len(data) > MAX_FILE_BYTES

    response = make_client(fresh_database).post(URL, content=body, headers=headers)

    assert asked == []
    assert response.status_code == 413
    assert file_count(fresh_database) == 0


def test_a_chunked_body_at_the_routes_limit_makes_no_file_either(
    fresh_database: DatabaseHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    put_claim(fresh_database)
    asked: list[str] = []
    for factory in ("TemporaryFile", "NamedTemporaryFile", "mkstemp"):
        monkeypatch.setattr(tempfile, factory, refuse_a_temporary_file(asked))
    envelope = len(multipart_body([kind_part(), file_part(data=b"")])[0])
    data = padded(PDF, UPLOAD_BODY_LIMIT_BYTES - envelope)
    body, headers = multipart_body([kind_part(), file_part(data=data)])

    def chunks() -> Any:
        for start in range(0, len(body), 64 * 1024):
            yield body[start : start + 64 * 1024]

    response = make_client(fresh_database).post(URL, content=chunks(), headers=headers)

    assert asked == []
    assert response.status_code == 413


# ── the claim's ceilings: five files and 3 MiB, sums of the size column ─────
def test_a_claim_holds_five_files_and_the_sixth_is_refused(
    fresh_database: DatabaseHandle,
) -> None:
    db = fresh_database
    put_claim(db)
    client = make_client(db)

    first = [upload(client, PDF + str(n).encode()) for n in range(MAX_FILES_PER_CLAIM)]
    sixth = upload(client, PDF + b"6")

    assert [r.status_code for r in first] == [201] * MAX_FILES_PER_CLAIM
    assert sixth.status_code == 409
    assert sixth.json() == {"detail": "the claim already holds five files"}
    assert file_count(db) == MAX_FILES_PER_CLAIM
    assert len(audit_rows(db)) == MAX_FILES_PER_CLAIM


def test_a_claim_holds_3_mib_and_one_byte_more_is_refused(
    fresh_database: DatabaseHandle,
) -> None:
    db = fresh_database
    put_claim(db)
    client = make_client(db)
    sizes = [MIB, MIB, MIB - 7, 7]  # exactly 3 MiB in four files

    accepted = [upload(client, blob(size, n)) for n, size in enumerate(sizes)]
    over = upload(client, blob(7, 99))  # the fifth file: over by seven

    assert [r.status_code for r in accepted] == [201] * 4
    assert sum(row[3] for row in stored_files(db)) == MAX_CLAIM_BYTES
    assert over.status_code == 409
    assert over.json() == {"detail": "the claim would hold more than 3 MiB of files"}
    assert file_count(db) == 4


def test_the_last_byte_of_a_claims_3_mib_is_the_boundary(
    fresh_database: DatabaseHandle,
) -> None:
    db = fresh_database
    put_claim(db)
    client = make_client(db)
    # 13 bytes are left: the smallest file that has a type here is 7.
    for n, size in enumerate((MIB, MIB, MIB - 13)):
        assert upload(client, blob(size, n)).status_code == 201

    over = upload(client, blob(14, 90))
    exact = upload(client, blob(13, 91))

    assert (over.status_code, exact.status_code) == (409, 201)


def test_two_claims_do_not_share_a_count_or_a_sum(
    fresh_database: DatabaseHandle,
) -> None:
    db = fresh_database
    put_claim(db)
    put_claim(db, OTHER_CLAIM)
    client = make_client(db)
    for n in range(MAX_FILES_PER_CLAIM):
        assert upload(client, PDF + str(n).encode()).status_code == 201

    full = upload(client, PDF)
    other = upload(client, PDF, claim_id=OTHER_CLAIM)

    assert (full.status_code, other.status_code) == (409, 201)
    assert [row[0] for row in stored_files(db)].count(OTHER_CLAIM) == 1


def test_posts_at_the_same_moment_cannot_take_a_claim_past_five_files(
    fresh_database: DatabaseHandle,
) -> None:
    db = fresh_database
    put_claim(db)
    clients = [make_client(db) for _ in range(8)]

    with ThreadPoolExecutor(max_workers=8) as pool:
        answers = list(
            pool.map(
                lambda n: upload(clients[n], PDF + str(n).encode()).status_code,
                range(8),
            )
        )

    assert sorted(answers) == [201] * 5 + [409] * 3
    assert file_count(db) == 5


# ── the global ceiling: 507 for everyone ────────────────────────────────────
# mutation: the global ceiling's check removed
def test_the_global_ceiling_is_exact_and_past_it_every_claim_gets_507(
    fresh_database: DatabaseHandle,
) -> None:
    db = fresh_database
    put_claim(db)
    put_claim(db, OTHER_CLAIM)
    client = make_client(db, ceiling=MIN_CEILING_BYTES)
    assert MIN_CEILING_BYTES == 3 * MIB

    # Three files of 1 MiB reach the ceiling exactly: all three are stored.
    exact = [upload(client, blob(MIB, n)) for n in range(3)]
    over = upload(client, PDF, claim_id=OTHER_CLAIM)

    assert [r.status_code for r in exact] == [201] * 3
    assert over.status_code == 507
    assert over.json() == {"detail": "uploads are stopped: the store is full"}
    assert file_count(db) == 3
    assert len(audit_rows(db)) == 3


def test_the_global_ceiling_one_byte_short_takes_one_byte_and_no_more(
    fresh_database: DatabaseHandle,
) -> None:
    db = fresh_database
    put_claim(db)
    put_claim(db, OTHER_CLAIM)
    client = make_client(db, ceiling=MIN_CEILING_BYTES)
    for n, size in enumerate((MIB, MIB, MIB - 13)):
        assert upload(client, blob(size, n)).status_code == 201

    over = upload(client, blob(14, 90), claim_id=OTHER_CLAIM)
    exact = upload(client, blob(13, 91), claim_id=OTHER_CLAIM)

    assert (over.status_code, exact.status_code) == (507, 201)


def test_a_claims_own_ceiling_is_judged_before_the_global_one(
    fresh_database: DatabaseHandle,
) -> None:
    db = fresh_database
    put_claim(db)
    client = make_client(db, ceiling=MIN_CEILING_BYTES)
    for n in range(3):
        assert upload(client, blob(MIB, n)).status_code == 201

    # Both are full: the claim's 3 MiB and the store's.
    response = upload(client, PDF)

    assert response.status_code == 409


def test_the_ceiling_counts_the_size_column_of_every_claim_of_every_tenant(
    fresh_database: DatabaseHandle,
) -> None:
    db = fresh_database
    put_claim(db)
    put_claim(db, OTHER_CLAIM, tenant="evaluation")
    held = blob(MIB, 500)
    owner_rows(
        db,
        "INSERT INTO claims.claim_files "
        "(file_id, claim_id, kind, media_type, size_bytes, sha256, content) "
        "VALUES (%s, %s, 'other', 'application/pdf', %s, %s, %s) RETURNING 1",
        (uuid.uuid4(), OTHER_CLAIM, MIB, hashlib.sha256(held).digest(), held),
    )
    client = make_client(db, ceiling=MIN_CEILING_BYTES)

    taken = [upload(client, blob(MIB, n)) for n in range(2)]
    over = upload(client, PDF)

    assert [r.status_code for r in taken] == [201, 201]
    assert over.status_code == 507


# ── one transaction ─────────────────────────────────────────────────────────
def test_a_failing_audit_insert_leaves_no_file_row(
    fresh_database: DatabaseHandle,
) -> None:
    db = fresh_database
    put_claim(db)
    owner_execute(db, "REVOKE INSERT ON audit.events FROM claims_api")

    response = upload(make_client(db), PDF)

    assert response.status_code == 500
    assert response.json() == {"detail": "internal error", "claim_id": CLAIM}
    assert file_count(db) == 0


def test_a_file_row_the_database_refuses_leaves_no_audit_row(
    fresh_database: DatabaseHandle,
) -> None:
    db = fresh_database
    put_claim(db)
    owner_execute(db, "REVOKE INSERT ON claims.claim_files FROM claims_api")

    response = upload(make_client(db), PDF)

    assert response.status_code == 500
    assert audit_rows(db) == []


def test_an_unreachable_database_is_the_shaped_503_with_the_claim(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.DEBUG):
        response = upload(make_client(), PDF, filename="canary-name-5521.pdf")

    assert response.status_code == 503
    assert response.json() == {
        "detail": "the database is unavailable",
        "claim_id": CLAIM,
    }
    assert "5521" not in caplog.text + response.text


# ── the same file twice on a claim ──────────────────────────────────────────
DUPLICATE_ANSWER = {"detail": "the claim already holds this file"}


def test_the_same_file_twice_on_a_claim_is_409_and_stores_nothing_more(
    fresh_database: DatabaseHandle,
) -> None:
    db = fresh_database
    put_claim(db)
    put_claim(db, OTHER_CLAIM)
    client = make_client(db)
    assert upload(client, PDF).status_code == 201

    again = upload(client, PDF, kind="other", filename="renamed.pdf")
    another_byte = upload(client, PDF + b"!")
    elsewhere = upload(client, PDF, claim_id=OTHER_CLAIM)

    assert again.status_code == 409
    assert again.json() == DUPLICATE_ANSWER
    assert (another_byte.status_code, elsewhere.status_code) == (201, 201)
    assert file_count(db) == 3
    assert len(audit_rows(db)) == 3


def test_a_duplicate_the_read_misses_is_the_constraints_409_and_leaks_no_detail(
    fresh_database: DatabaseHandle,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    db = fresh_database
    put_claim(db)
    client = make_client(db)
    assert upload(client, PDF + CANARY_BYTES).status_code == 201
    # A read that sees nothing: a second upload that raced the first one.
    monkeypatch.setattr(uploads, "DUPLICATE_SQL", "SELECT 1 WHERE %s <> %s AND false")

    with caplog.at_level(logging.DEBUG):
        again = upload(client, PDF + CANARY_BYTES)

    assert again.status_code == 409
    assert again.json() == DUPLICATE_ANSWER
    assert file_count(db) == 1
    assert len(audit_rows(db)) == 1
    for text in (caplog.text, again.text):
        assert "duplicate key" not in text
        assert "Key (" not in text
        assert CANARY_BYTES.decode() not in text


def test_a_clash_of_identifiers_is_not_a_duplicate_file_and_is_a_500(
    fresh_database: DatabaseHandle,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    db = fresh_database
    put_claim(db)
    put_claim(db, OTHER_CLAIM)
    client = make_client(db)
    taken = upload(client, PDF).json()["file_id"]
    # The next file draws the identifier of the first: the primary key refuses
    # it, which is a unique violation of another constraint than the duplicate.
    monkeypatch.setattr(uploads.uuid, "uuid4", lambda: uuid.UUID(taken))

    with caplog.at_level(logging.DEBUG):
        response = upload(client, PDF + b"!", claim_id=OTHER_CLAIM)

    assert response.status_code == 500
    assert response.json() == {"detail": "internal error", "claim_id": OTHER_CLAIM}
    assert "23505" in caplog.text
    assert "duplicate key" not in caplog.text + response.text
    assert file_count(db) == 1


# ── the row ceiling beside the byte ceiling ─────────────────────────────────
def test_the_row_ceiling_is_exact_and_refuses_the_next_file_though_bytes_remain(
    fresh_database: DatabaseHandle,
) -> None:
    db = fresh_database
    put_claim(db)
    put_claim(db, OTHER_CLAIM)
    client = make_client(db, rows=MIN_CEILING_ROWS)

    # Five rows reach the ceiling exactly: all five are stored.
    exact = [upload(client, blob(10, n)) for n in range(MIN_CEILING_ROWS)]
    over = upload(client, blob(10, 99), claim_id=OTHER_CLAIM)

    assert [r.status_code for r in exact] == [201] * MIN_CEILING_ROWS
    assert over.status_code == 507
    assert over.json() == {"detail": "uploads are stopped: the store is full"}
    assert file_count(db) == MIN_CEILING_ROWS
    assert len(audit_rows(db)) == MIN_CEILING_ROWS


# ── the ceilings are exact: a second upload waits for the first ─────────────
def waiting_on_a_lock(db: DatabaseHandle) -> int:
    with psycopg.connect(db.admin_dsn) as admin:
        row = admin.execute(
            "SELECT count(*) FROM pg_stat_activity "
            "WHERE datname = %s AND wait_event_type = 'Lock'",
            (db.name,),
        ).fetchone()
    assert row is not None
    return row[0]


# mutation: the advisory locks removed
def test_an_upload_waits_for_the_one_before_it_and_is_refused_at_the_ceiling(
    fresh_database: DatabaseHandle,
) -> None:
    """Two connections: the first holds the store's lock with three files of
    1 MiB inserted and not yet committed (the byte ceiling is full once they
    are); the second upload, to another claim, must wait, and then read what the
    first committed. Without the lock it reads an empty store and is stored."""
    db = fresh_database
    put_claim(db)
    put_claim(db, OTHER_CLAIM)
    holder = connect(db.dsn("claims_api"), "test-holder")
    try:
        holder.execute(
            "SELECT pg_advisory_xact_lock(%s::int, hashtext(%s))",
            (CLAIM_LOCK_CLASS, OTHER_CLAIM),
        )
        holder.execute("SELECT pg_advisory_xact_lock(%s::int, 0)", (STORE_LOCK_CLASS,))
        for n in range(3):
            content = blob(MIB, n)
            holder.execute(
                "INSERT INTO claims.claim_files (file_id, claim_id, kind, "
                "media_type, size_bytes, sha256, content) "
                "VALUES (%s, %s, 'other', 'application/pdf', %s, %s, %s)",
                (
                    uuid.uuid4(),
                    OTHER_CLAIM,
                    MIB,
                    hashlib.sha256(content).digest(),
                    content,
                ),
            )
        client = make_client(db, ceiling=MIN_CEILING_BYTES)
        with ThreadPoolExecutor(max_workers=1) as pool:
            second = pool.submit(upload, client, blob(20, 7))
            give_up = time.monotonic() + 30
            while waiting_on_a_lock(db) < 1:
                assert time.monotonic() < give_up, "the upload did not wait"
                time.sleep(0.02)
            assert not second.done()
            holder.commit()
            response = second.result(timeout=30)
    finally:
        holder.close()

    assert response.status_code == 507
    assert file_count(db) == 3
    assert audit_rows(db) == []  # the holder wrote its rows by hand


# mutation: the claim lock removed (the store's lock stays)
def test_an_upload_to_a_claim_waits_for_the_one_before_it_on_that_claim(
    fresh_database: DatabaseHandle,
) -> None:
    """The first connection holds the claim's lock with five files of the claim
    inserted and not committed; a second upload to the SAME claim must wait and
    then read five files. Without the claim's lock (the store's lock is not held
    here) it reads none, and is stored as a sixth."""
    db = fresh_database
    put_claim(db)
    holder = connect(db.dsn("claims_api"), "test-holder")
    try:
        holder.execute(
            "SELECT pg_advisory_xact_lock(%s::int, hashtext(%s))",
            (CLAIM_LOCK_CLASS, CLAIM),
        )
        for n in range(MAX_FILES_PER_CLAIM):
            content = blob(10, n)
            holder.execute(
                "INSERT INTO claims.claim_files (file_id, claim_id, kind, "
                "media_type, size_bytes, sha256, content) "
                "VALUES (%s, %s, 'other', 'application/pdf', %s, %s, %s)",
                (uuid.uuid4(), CLAIM, 10, hashlib.sha256(content).digest(), content),
            )
        client = make_client(db)
        with ThreadPoolExecutor(max_workers=1) as pool:
            second = pool.submit(upload, client, blob(20, 77))
            give_up = time.monotonic() + 10
            while waiting_on_a_lock(db) < 1:
                assert time.monotonic() < give_up, "the upload did not wait"
                time.sleep(0.02)
            holder.commit()
            response = second.result(timeout=30)
    finally:
        holder.close()

    assert response.status_code == 409
    assert response.json() == {"detail": "the claim already holds five files"}
    assert file_count(db) == MAX_FILES_PER_CLAIM


# ── a lock wait that times out is "busy", not a database that is down ───────
class ShortLockWait:
    """A connection whose advisory-lock statements, and only those, have a short
    statement timeout (``SET LOCAL``, in the transaction the statement is in): the
    waiting statement is cancelled as the real 10 s bound cancels it, and the
    cheap statements around it keep the real bound, so that a slow machine cannot
    cancel one of them into the same answer. The production code is unchanged."""

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    def execute(self, query: Any, params: Any = None) -> Any:
        if "pg_advisory_xact_lock" in str(query):
            self._conn.execute("SET LOCAL statement_timeout = 300")
        return self._conn.execute(query, params)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)


def short_lock_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give the upload's lock waits a 300 ms bound (see ``ShortLockWait``)."""
    real = uploads.connect

    @contextmanager
    def connect_with_short_wait(dsn: str, name: str) -> Iterator[ShortLockWait]:
        with real(dsn, name) as conn:
            yield ShortLockWait(conn)

    monkeypatch.setattr(uploads, "connect", connect_with_short_wait)


@pytest.mark.parametrize("which", ["the claim's lock", "the store's lock"])
def test_a_lock_wait_that_times_out_is_503_busy_with_a_wait_and_stores_nothing(
    fresh_database: DatabaseHandle,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    which: str,
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
        short_lock_wait(monkeypatch)
        client = make_client(db)

        with caplog.at_level(logging.DEBUG):
            response = upload(client, blob(20, 5), filename=CANARY_NAME)
        holder.commit()
    finally:
        holder.close()

    assert response.status_code == 503
    assert response.json() == {"detail": uploads.LOCK_BUSY_DETAIL, "claim_id": CLAIM}
    assert response.headers["Retry-After"] == "5"
    # Contention is normal, not a database fault: one WARNING with the class and
    # the SQLSTATE, and no ERROR line at all.
    assert [r for r in caplog.records if r.levelno >= logging.ERROR] == []
    (line,) = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert "QueryCanceled" in line.getMessage()
    assert "57014" in line.getMessage()
    assert CANARY_NAME not in caplog.text + response.text
    assert file_count(db) == 0
    assert audit_rows(db) == []


# ── the app's own rate limit: a second ceiling, for the whole store ─────────
# mutation: the rate check removed
def test_the_app_refuses_an_upload_at_the_rate_and_counts_only_the_last_minute(
    fresh_database: DatabaseHandle,
) -> None:
    db = fresh_database
    put_claim(db)
    put_claim(db, OTHER_CLAIM)
    client = make_client(db, rate=MIN_RATE_PER_MINUTE)  # 6 a minute
    stored = [upload(client, blob(10, n)) for n in range(MAX_FILES_PER_CLAIM)]
    stored.append(upload(client, blob(10, 50), claim_id=OTHER_CLAIM))
    assert [r.status_code for r in stored] == [201] * MIN_RATE_PER_MINUTE

    over = upload(client, blob(10, 51), claim_id=OTHER_CLAIM)

    assert over.status_code == 429
    assert over.json() == {"detail": uploads.RATE_DETAIL}
    assert over.headers["Retry-After"] == "60"
    assert file_count(db) == MIN_RATE_PER_MINUTE
    assert len(audit_rows(db)) == MIN_RATE_PER_MINUTE
    # One file older than a minute no longer counts: the count is five, under 6.
    owner_execute(
        db,
        "UPDATE claims.claim_files SET received_at = received_at - interval "
        "'2 minutes' WHERE file_id = (SELECT file_id FROM claims.claim_files "
        "ORDER BY received_at LIMIT 1)",
    )
    again = upload(client, blob(10, 52), claim_id=OTHER_CLAIM)
    assert again.status_code == 201
    assert upload(client, blob(10, 53), claim_id=OTHER_CLAIM).status_code == 429


def test_the_rate_is_for_the_whole_store_not_for_a_claim_or_a_caller(
    fresh_database: DatabaseHandle,
) -> None:
    db = fresh_database
    ids = [f"CLM-93{n:02d}" for n in range(MIN_RATE_PER_MINUTE)]
    for claim_id in ids:
        put_claim(db, claim_id)
    client = make_client(db, rate=MIN_RATE_PER_MINUTE)

    # One file to each of six claims, as six callers would: the store's count.
    stored = [upload(client, blob(10, n), claim_id=c) for n, c in enumerate(ids)]
    over = upload(client, blob(10, 99), claim_id=ids[0])

    assert [r.status_code for r in stored] == [201] * MIN_RATE_PER_MINUTE
    assert over.status_code == 429


def test_the_rate_check_comes_after_the_ceilings_and_a_full_claim_is_409_first(
    fresh_database: DatabaseHandle,
) -> None:
    db = fresh_database
    put_claim(db)
    client = make_client(db, rate=MIN_RATE_PER_MINUTE)
    for n in range(MAX_FILES_PER_CLAIM):
        assert upload(client, blob(10, n)).status_code == 201
    assert upload(client, blob(10, 60), claim_id=CLAIM).status_code == 409
    put_claim(db, OTHER_CLAIM)
    assert upload(client, blob(10, 61), claim_id=OTHER_CLAIM).status_code == 201

    # Six in the minute: the rate is reached; the claim's own 409 still comes
    # first for a claim that is full, and the 429 for one that is not.
    assert upload(client, blob(10, 62), claim_id=CLAIM).status_code == 409
    assert upload(client, blob(10, 63), claim_id=OTHER_CLAIM).status_code == 429


# ── a database error's text is never kept or told ───────────────────────────
def test_a_refused_insert_leaves_no_content_byte_and_no_detail_in_a_log_or_answer(
    fresh_database: DatabaseHandle,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    db = fresh_database
    put_claim(db)
    # The canary is at the start: PostgreSQL quotes only the first bytes.
    content = b"%PDF-" + CANARY_BYTES + PDF
    # An insert whose size disagrees with its content: the table's CHECK refuses
    # it, and PostgreSQL's DETAIL quotes the whole failing row, content included.
    broken = uploads.INSERT_FILE_SQL.replace(
        "VALUES (%s, %s, %s, %s, %s,", "VALUES (%s, %s, %s, %s, %s + 1,"
    )
    assert broken != uploads.INSERT_FILE_SQL
    params = (
        uuid.uuid4(),
        CLAIM,
        "photos",
        "application/pdf",
        len(content),
        hashlib.sha256(content).digest(),
        content,
    )
    with (
        connect(db.dsn("claims_api"), "test-probe") as probe,
        pytest.raises(psycopg.errors.CheckViolation) as raised,
    ):
        probe.execute(broken, params)
    detail = raised.value.diag.message_detail or ""
    assert "Failing row contains" in detail
    assert CANARY_BYTES.hex() in detail  # the DETAIL does quote the content
    monkeypatch.setattr(uploads, "INSERT_FILE_SQL", broken)
    exporter = InMemorySpanExporter()

    with caplog.at_level(logging.DEBUG):
        response = upload(make_client(db, exporter=exporter), content)

    assert response.status_code == 500
    assert response.json() == {"detail": "internal error", "claim_id": CLAIM}
    for text in (caplog.text, response.text):
        assert "Failing row" not in text
        assert CANARY_BYTES.hex() not in text
        assert CANARY_BYTES.decode() not in text
    assert "CheckViolation" in caplog.text
    assert "23514" in caplog.text
    assert_spans_hold_no_exception_and_no_canary(exporter, CANARY_BYTES.hex())
    assert file_count(db) == 0
    assert audit_rows(db) == []


# ── nothing of the file but its bytes is kept or told ───────────────────────
CANARY_NAME = "canary-file-name-7421.pdf"
CANARY_TYPE = "application/x-canary-type-9913"
CANARY_BYTES = b"canary-content-bytes-5530"


@pytest.mark.parametrize(
    ("data", "status"),
    [
        pytest.param(PDF + CANARY_BYTES, 201, id="stored"),
        pytest.param(b"GIF89a" + CANARY_BYTES, 415, id="refused as a type"),
        pytest.param(b"", 422, id="refused as empty"),
    ],
)
def test_no_row_log_span_or_answer_holds_a_file_name_its_declared_type_or_content(
    fresh_database: DatabaseHandle,
    caplog: pytest.LogCaptureFixture,
    data: bytes,
    status: int,
) -> None:
    db = fresh_database
    put_claim(db)
    exporter = InMemorySpanExporter()
    canaries = (CANARY_NAME, CANARY_TYPE, CANARY_BYTES.decode())

    with caplog.at_level(logging.DEBUG):
        response = upload(
            make_client(db, exporter=exporter),
            data,
            filename=CANARY_NAME,
            declared=CANARY_TYPE,
        )

    assert response.status_code == status
    for canary in canaries:
        assert canary not in caplog.text
        assert canary not in response.text
        assert_spans_hold_no_exception_and_no_canary(exporter, canary)
        # Every table the request could have written, but the file's own bytes.
        for table in ("audit.events", "claims.claims", "claims.claim_documents"):
            rows = owner_rows(db, f"SELECT t::text FROM {table} AS t")  # noqa: S608
            assert canary not in str(rows), table
    kept = owner_rows(
        db,
        "SELECT file_id, claim_id, kind, media_type, size_bytes, sha256, received_at "
        "FROM claims.claim_files",
    )
    assert CANARY_NAME not in str(kept) + response.text
    assert CANARY_TYPE not in str(kept)


def test_a_server_error_after_the_form_was_read_logs_no_file_name(
    fresh_database: DatabaseHandle, caplog: pytest.LogCaptureFixture
) -> None:
    db = fresh_database
    put_claim(db)
    owner_execute(db, "REVOKE INSERT ON audit.events FROM claims_api")

    with caplog.at_level(logging.DEBUG):
        response = upload(
            make_client(db),
            PDF + CANARY_BYTES,
            filename=CANARY_NAME,
            declared=CANARY_TYPE,
        )

    assert response.status_code == 500
    for canary in (CANARY_NAME, CANARY_TYPE, CANARY_BYTES.decode()):
        assert canary not in caplog.text + response.text


# ── what the pages will read ────────────────────────────────────────────────
def test_the_files_of_a_claim_are_listed_in_arrival_order_and_read_back(
    fresh_database: DatabaseHandle,
) -> None:
    db = fresh_database
    put_claim(db)
    put_claim(db, OTHER_CLAIM)
    client = make_client(db)
    first = upload(client, PDF, kind="police_report").json()
    second = upload(client, PNG, kind="photos").json()
    third = upload(client, JPEG, kind="other").json()
    upload(client, PDF, claim_id=OTHER_CLAIM)

    with connect(db.dsn("claims_api"), "test-read") as conn:
        listed = list_files(conn, TENANT, CLAIM)
        content = file_content(conn, TENANT, CLAIM, uuid.UUID(second["file_id"]))
        wrong_claim = file_content(
            conn, TENANT, OTHER_CLAIM, uuid.UUID(first["file_id"])
        )
        wrong_tenant = file_content(
            conn, "evaluation", CLAIM, uuid.UUID(first["file_id"])
        )
        nothing = file_content(conn, TENANT, CLAIM, uuid.uuid4())
        other_tenant_list = list_files(conn, "evaluation", CLAIM)

    assert [str(f.file_id) for f in listed] == [
        first["file_id"],
        second["file_id"],
        third["file_id"],
    ]
    assert [(f.kind, f.media_type, f.size_bytes) for f in listed] == [
        ("police_report", "application/pdf", len(PDF)),
        ("photos", "image/png", len(PNG)),
        ("other", "image/jpeg", len(JPEG)),
    ]
    assert [f.sha256 for f in listed] == [
        hashlib.sha256(x).hexdigest() for x in (PDF, PNG, JPEG)
    ]
    assert all(f.received_at is not None for f in listed)
    assert content is not None
    assert (content.media_type, content.content) == ("image/png", PNG)
    assert (wrong_claim, wrong_tenant, nothing) == (None, None, None)
    assert other_tenant_list == ()
