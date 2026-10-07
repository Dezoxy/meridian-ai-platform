"""A claimant's uploaded file: ``POST /claims/{claim_id}/files`` (S070, T-38).

A file's bytes are stored with its claim (``claims.claim_files``). The claimant's
form and the adjuster's list of a claim's files (F4a) show its kind, size and
time, and for the adjuster its type and hash, "not scanned". The download that
serves the bytes back to the adjuster is ``file_download``'s (F4b): its own
route, behind its own switch (off by default, and the chart turns it on only for
a local host name), and ``file_content`` below is its read. Nothing reads the
file: no text is extracted, nothing is sent to a model, no rule changes. An
upload is NOT a document arrival (the design's advisor reading): it moves no
claim, stores no document name, starts no triage and calls no runtime, so no
upload can spend the model budget. A scanner is designed, not built.

The route is unauthenticated, as every route of the app is until S021, so every
bound is the app's own and the order of its checks is the contract. Each check
is a refusal that stores nothing and writes no audit row (the app's other
refusals write none either; only the one late-documents event is a refusal's
row):

1. the form: exactly one ``kind`` field (one of five) and one ``file`` part,
   nothing else (422). The form is read by a parser that cannot spool to disk.
2. the claim exists and is the deployment's tenant's (404, the documents
   route's own answer).
3. the file is 1 byte to 1 MiB (422 empty, 413 over).
4. the type, from the file's first bytes alone: a PDF, a JPEG or a PNG (415).
   The declared content type and the file name are ignored: never stored,
   logged, put on a span or echoed.
5. the claim's ceilings: five files and 3 MiB, sums of the ``size_bytes``
   column and never of the content (409), and the same file twice is refused
   (409).
6. the global ceilings on the table: stored bytes and rows (507).

The body is read before any connection is opened, so no lock is held while
bytes arrive. It is read under a deadline (408), and a client that goes away
mid-body is no error: a line at INFO and an answer nobody receives. Then one
transaction takes an advisory lock for the claim, then one for the store (always
in that order), reads the sums and holds the insert and its one audit row. The
sums are exact because every upload takes the locks before it reads them: under
READ COMMITTED an unlocked sum lets N uploads at the same moment overshoot a
ceiling by N-1 files.
"""

import asyncio
import hashlib
import logging
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from http import HTTPStatus
from typing import Annotated, Literal, get_args

import psycopg
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, Response
from opentelemetry.trace import Tracer
from pydantic import Field
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser
from starlette.requests import ClientDisconnect

from meridian.platform.common.audit import AuditEvent, record_event
from meridian.platform.common.db import connect
from meridian.platform.common.env import SettingsError
from meridian.platform.common.http import ErrorBody, error_responses
from meridian.platform.common.telemetry import (
    mark_error,
    set_span_attributes,
    start_span,
)
from meridian.platform.common.wire import WireModel
from meridian.workloads.claims_triage.adjuster import (
    CROSS_SITE_DETAIL,
    NO_SUCH_CLAIM_DETAIL,
    ClaimId,
    is_cross_site,
)
from meridian.workloads.claims_triage.lifecycle import SERVICE_NAME
from meridian.workloads.claims_triage.rules import Document
from meridian.workloads.claims_triage.triaging import answer, claim_database_failure

logger = logging.getLogger(__name__)

# ── the settings: three variables, which the chart sets ─────────────────────
# The chart sets this one (``route.uploads.enabled``, F3) to ``on``; the other two
# it leaves to their defaults.
UPLOADS_ENABLED_ENV = "MERIDIAN_CLAIMS_UPLOADS"
UPLOADS_CEILING_ENV = "MERIDIAN_UPLOADS_CEILING_BYTES"
UPLOADS_ROWS_ENV = "MERIDIAN_UPLOADS_CEILING_ROWS"
UPLOADS_RATE_ENV = "MERIDIAN_UPLOADS_RATE_PER_MINUTE"

MIB = 1024 * 1024
# The most one file is: the table's CHECK holds the same number.
MAX_FILE_BYTES = MIB
MAX_FILES_PER_CLAIM = 5
MAX_CLAIM_BYTES = 3 * MIB
# The global ceiling on the bytes of every stored file: 128 MiB by default (the
# 2 Gi volume is shared with the audit trail and its write-ahead log, which was
# not measured). The floor is one claim's worth (a lower ceiling would refuse a
# claim its own ceiling allows); the cap is 256 MiB.
DEFAULT_CEILING_BYTES = 128 * MIB
MIN_CEILING_BYTES = MAX_CLAIM_BYTES
MAX_CEILING_BYTES = 256 * MIB
# The global ceiling on rows, beside the bytes: a row costs more than its
# ``size_bytes`` (a header, a pointer to the stored content, three index
# entries: about 150 bytes), which the byte ceiling does not count. The smallest
# file that has a type is 3 bytes, so 128 MiB of them would be 44 million rows
# and over 6 GB of overhead; 2,000 rows are at most about 0.3 MB of overhead,
# and a mean file of 64 KiB fills the byte ceiling at the same moment. The floor
# is one claim's five files; the cap is 100,000 rows (15 MB of overhead).
DEFAULT_CEILING_ROWS = 2_000
MIN_CEILING_ROWS = MAX_FILES_PER_CLAIM
MAX_CEILING_ROWS = 100_000
# The app's own rate limit: the files stored in the last minute, in the whole
# store, may not reach this. It is a second ceiling and not a per-caller limit
# (no caller has an identity before S021): the edge's is 6 a minute for the
# route as one bucket per proxy pod, and the app's must not depend on which edge
# route served the path or whether its policy attached. So the default is five
# times the edge's allowance (room for several proxy pods and for a burst the
# edge lets through), 30 a minute, which still takes 67 minutes to fill 2,000
# rows where an unbraked caller fills them in seconds. The floor is the edge's 6
# (a lower app limit would refuse what the edge allows); the cap is 600, ten a
# second, where it stops being a brake (the 2,000 rows go in about 3 minutes).
DEFAULT_RATE_PER_MINUTE = 30
MIN_RATE_PER_MINUTE = 6
MAX_RATE_PER_MINUTE = 600
RATE_WINDOW_SECONDS = 60
# How many uploads the route reads and stores at once. The pod has 192 Mi, of
# which the Claims API itself takes about 66 MB (measured), leaving about 126 MB.
# One upload peaks at about 2.1 MiB in the form parser and 3 to 4 MiB through to
# the database (measured, the security review). Four at once is at most 16 MiB,
# an eighth of that room, which leaves the rest to the other routes' triages and
# to the interpreter's own growth; the review's 35 stalled uploads that take the
# pod down is nine times four. An upload past the four is refused at once,
# before a byte of its body is read, so a slow body cannot queue anything.
MAX_CONCURRENT_UPLOADS = 4
# What a refused caller is told to wait, in seconds (``Retry-After``): a permit
# or a lock is free in moments; the rate's window is a minute.
BUSY_RETRY_SECONDS = 5
RATE_RETRY_SECONDS = RATE_WINDOW_SECONDS
# The most time the route gives a body to arrive, while it holds one of the four
# permits (a slow or stalled body would otherwise hold it for as long as the
# client likes, and four of them would refuse the route to everyone). A 1 MiB file
# is whole in well under a second on any link the pages expect; 20 s is a link of
# about 50 KB a second, which is slow and still served. Past it: 408, the permit
# freed. Envoy buffers the whole request first, so this bounds a caller that goes
# round the edge (in the cluster, or through a port-forward).
READ_DEADLINE_SECONDS = 20
# The first number of each advisory lock's key (the second is the claim's hash,
# or zero for the store): two lock spaces that cannot meet. A claim's lock is
# always taken before the store's, so two uploads cannot wait on each other.
CLAIM_LOCK_CLASS = 70_001
STORE_LOCK_CLASS = 70_002

# The body limit of the upload route: the file and the multipart envelope. The
# envelope is the three delimiters (each CRLF, two hyphens, a boundary of at most
# 70 characters, and CRLF or two hyphens: 76 bytes, 228 in all), the ``kind``
# part (its header of 45 bytes, a blank line, a value of at most 18 characters:
# about 70 bytes) and the file part's headers (a disposition of 56 bytes and a
# file name of up to 255, a declared type of up to 127 characters, a blank line:
# about 460 bytes), about 760 bytes in all. The allowance is 76 KiB, far more than
# that: it is the number the edge's second route buffers (the chart's
# ``route.uploads.requestBufferLimit``, 1,126,400, which a test holds equal to
# this limit), so that the edge and the app refuse at one size. The surplus costs
# nothing stored (the name and the type are never kept, and a file past 1 MiB is
# refused by the route itself) and about 75 KiB more of memory a request.
ENVELOPE_ALLOWANCE_BYTES = 76 * 1024
UPLOAD_BODY_LIMIT_BYTES = MAX_FILE_BYTES + ENVELOPE_ALLOWANCE_BYTES
UPLOAD_PATH = "/claims/{claim_id}/files"
# The ``kind`` field is at most this long: the longest kind has 18 characters.
KIND_FIELD_MAX_BYTES = 64

# One definition of the kinds: the four codes the rules know, and ``other``. The
# table's CHECK lists the same five, and its test holds the two equal.
FILE_KINDS: tuple[str, ...] = (*get_args(Document), "other")
FileKind = Literal[*FILE_KINDS]
MediaType = Literal["application/pdf", "image/jpeg", "image/png"]
ConflictReason = Literal["claim_full", "claim_bytes", "duplicate"]
# The first bytes of each type; none is the start of another.
SIGNATURES: tuple[tuple[bytes, MediaType], ...] = (
    (b"%PDF-", "application/pdf"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
)

# What is audited, from closed lists. The event is the claim's, in the form the
# others have; the reason is the one word. The size and the hash are the file
# row's, which the role cannot change or delete; the name is not kept at all.
UPLOAD_EVENT = "claim.file_stored"
UPLOAD_OUTCOME = "stored"
UPLOAD_REASON = "uploaded"

FORM_PROBLEM = "send exactly one kind field and one file part named file, and no more"
KIND_PROBLEM = f"kind must be one of: {', '.join(FILE_KINDS)}"
CONTENT_TYPE_PROBLEM = "send the file as multipart/form-data"
EMPTY_PROBLEM = "the file is empty"
TOO_LARGE_DETAIL = f"the file is larger than {MAX_FILE_BYTES} bytes"
UNSUPPORTED_DETAIL = "the file is not a PDF, a JPEG or a PNG"
CLAIM_FULL_DETAIL = "the claim already holds five files"
CLAIM_BYTES_DETAIL = "the claim would hold more than 3 MiB of files"
DUPLICATE_DETAIL = "the claim already holds this file"
READ_TIMEOUT_DETAIL = "the file did not arrive in time"
STORE_FULL_DETAIL = "uploads are stopped: the store is full"
RATE_DETAIL = "too many uploads in the last minute; try again shortly"
SATURATED_DETAIL = "uploads are busy: too many at once; try again shortly"
LOCK_BUSY_DETAIL = (
    "uploads are busy: the claim or the store is locked; try again shortly"
)
# The two ways a database statement ends that are not a database that is down.
QUERY_CANCELED_SQLSTATE = "57014"

# The table's constraint (migration 0032): one file per claim and hash.
DUPLICATE_CONSTRAINT = "claim_files_one_per_claim_and_hash"

CLAIM_EXISTS_SQL = (
    "SELECT claim_id FROM claims.claims WHERE claim_id = %s AND tenant = %s"
)
LOCK_CLAIM_SQL = "SELECT pg_advisory_xact_lock(%s::int, hashtext(%s))"
LOCK_STORE_SQL = "SELECT pg_advisory_xact_lock(%s::int, 0)"
CLAIM_FILES_SQL = (
    "SELECT count(*), COALESCE(sum(size_bytes), 0)::bigint "
    "FROM claims.claim_files WHERE claim_id = %s"
)
DUPLICATE_SQL = (
    "SELECT file_id FROM claims.claim_files WHERE claim_id = %s AND sha256 = %s"
)
# The rows, the bytes and the files of the last minute in one statement, so the
# ceilings and the rate see one state. The plan is a sequential scan of the table:
# no index of migration 0032 leads with ``received_at`` (they are the key, the
# unique ``(claim_id, sha256)`` and ``(claim_id, received_at)``), and none is
# needed, since the scan reads at most the row ceiling's rows of the heap (the
# content is stored out of line) and the statement already read every row for the
# sum. At the row cap of 100,000 it is still milliseconds, under the 10 s bound.
STORE_TOTALS_SQL = (
    "SELECT count(*), COALESCE(sum(size_bytes), 0)::bigint, "
    "count(*) FILTER (WHERE received_at >= "
    "clock_timestamp() - make_interval(secs => %s)) "
    "FROM claims.claim_files"
)
# ``received_at`` is taken after the claim's lock, not when the transaction
# began, so the arrival order is the order the locks were taken in.
INSERT_FILE_SQL = (
    "INSERT INTO claims.claim_files "
    "(file_id, claim_id, kind, media_type, size_bytes, sha256, content, received_at) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s, clock_timestamp())"
)
# The one query that selects ``content``: by the claim (found with its tenant
# first) and the file's identifier, never by the identifier alone.
FILE_CONTENT_SQL = (
    "SELECT media_type, content FROM claims.claim_files "
    "WHERE claim_id = %s AND file_id = %s"
)


def uploads_enabled_of(raw: str | None) -> bool:
    """The switch from the variable's value: off when it is unset, else exactly
    ``on`` or ``off`` (the chart renders ``on``); any other word (a typo among
    them) stops the start rather than turning an upload door on or off by
    guess."""
    if raw is None or raw == "off":
        return False
    if raw == "on":
        return True
    raise SettingsError(f"{UPLOADS_ENABLED_ENV} must be on or off")


def ceiling_bytes_of(raw: str | None) -> int:
    """The global ceiling from the variable's value: the default when it is
    unset, else a whole number of bytes from the floor to the cap, in ASCII
    digits. Raises ``SettingsError`` naming the variable, never its value."""
    if raw is None:
        return DEFAULT_CEILING_BYTES
    # ASCII digits only: ``\d`` and ``int`` accept other scripts' digits.
    if re.fullmatch(r"[0-9]{1,12}", raw):
        value = int(raw)
        if MIN_CEILING_BYTES <= value <= MAX_CEILING_BYTES:
            return value
    raise SettingsError(
        f"{UPLOADS_CEILING_ENV} must be a whole number of bytes "
        f"from {MIN_CEILING_BYTES} to {MAX_CEILING_BYTES}"
    )


def ceiling_rows_of(raw: str | None) -> int:
    """The global row ceiling from the variable's value, as ``ceiling_bytes_of``:
    the default when unset, else whole ASCII digits from the floor to the cap."""
    if raw is None:
        return DEFAULT_CEILING_ROWS
    if re.fullmatch(r"[0-9]{1,7}", raw):
        value = int(raw)
        if MIN_CEILING_ROWS <= value <= MAX_CEILING_ROWS:
            return value
    raise SettingsError(
        f"{UPLOADS_ROWS_ENV} must be a whole number of rows "
        f"from {MIN_CEILING_ROWS} to {MAX_CEILING_ROWS}"
    )


def rate_per_minute_of(raw: str | None) -> int:
    """The app's rate limit from the variable's value, as ``ceiling_bytes_of``:
    the default when unset, else whole ASCII digits from the floor to the cap."""
    if raw is None:
        return DEFAULT_RATE_PER_MINUTE
    if re.fullmatch(r"[0-9]{1,5}", raw):
        value = int(raw)
        if MIN_RATE_PER_MINUTE <= value <= MAX_RATE_PER_MINUTE:
            return value
    raise SettingsError(
        f"{UPLOADS_RATE_ENV} must be a whole number of uploads a minute "
        f"from {MIN_RATE_PER_MINUTE} to {MAX_RATE_PER_MINUTE}"
    )


def sniff_media_type(content: bytes) -> MediaType | None:
    """The type the file's first bytes name, or ``None``. Nothing else decides."""
    for prefix, media_type in SIGNATURES:
        if content.startswith(prefix):
            return media_type
    return None


class StoredFile(WireModel):
    """The answer to an upload: the file's identifier, kind, type, size and hash
    in hex. Nothing of the content and nothing of the name."""

    file_id: uuid.UUID
    kind: FileKind
    media_type: MediaType
    size_bytes: Annotated[int, Field(ge=1, le=MAX_FILE_BYTES)]
    sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class UploadErrorBody(ErrorBody):
    """An error answer of the upload route. The route's own answers (a refusal
    after the claim was looked up, a busy or a failed store) say which claim; the
    middleware's 500 for a failure nobody handled, and the audit log's 503, say
    only ``detail``. No run: an upload starts none."""

    claim_id: str | None = None


class UploadConflict(HTTPException):
    """A 409 that says which of the three it is, so that nothing parses the
    sentence: the claim holds five files, it would hold over 3 MiB, or it holds
    this file already. The JSON route answers all three alike, the claimant's form
    takes the last as the success it is (a retry after a lost answer)."""

    def __init__(self, reason: ConflictReason, detail: str) -> None:
        super().__init__(409, detail)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class FileContent:
    """One file's bytes and the type it is to be served as."""

    media_type: str
    content: bytes


def file_content(
    conn: psycopg.Connection, tenant: str, claim_id: str, file_id: uuid.UUID
) -> FileContent | None:
    """One file's bytes and type, by claim and identifier: ``None`` unless the
    tenant's claim holds that file. The caller is the download (``file_download``,
    F4b), which exists when its switch is on; this is where the one query that
    selects ``content`` is written, once, with its tenant filter."""
    if conn.execute(CLAIM_EXISTS_SQL, (claim_id, tenant)).fetchone() is None:
        return None
    row = conn.execute(FILE_CONTENT_SQL, (claim_id, file_id)).fetchone()
    if row is None:
        return None
    media_type, content = row
    return FileContent(media_type, bytes(content))


# ── reading the form ────────────────────────────────────────────────────────
def problem(location: tuple[str, ...], message: str) -> HTTPException:
    """A 422 in the shape of the app's other 422s: where and what, fixed text,
    never anything the caller sent."""
    detail = [{"loc": list(location), "msg": message, "type": "upload_form"}]
    return HTTPException(422, detail)


class MemoryOnlyParser(MultiPartParser):
    """Starlette's multipart parser with the point at which a file part rolls
    over to a temporary file moved to the body's own limit.

    The parser keeps a file part in a ``SpooledTemporaryFile`` that rolls over to
    disk past 1 MiB, and the route's body limit lets a part a little past 1 MiB
    through (the envelope's allowance). The pod's only writable path is 16 Mi of
    ``/tmp``, and no upload may touch it. No part can be larger than the body, so
    nothing rolls over, and the route then refuses a file past 1 MiB itself.

    This leans on Starlette internals that are not documented API: the class
    attribute ``spool_max_size``, which the parser reads as ``self.spool_max_size``
    when it makes each file part's ``SpooledTemporaryFile``; and ``_rolled``, which
    that file's own test reads. Starlette is not a pin of this repository: it
    arrives with ``fastapi``, so a refresh of the lock can rename or move them,
    and the override would then do nothing, silently. A test with no database
    (``test_claim_uploads_body_read.py``) feeds a part of 1.05 MiB and fails if it
    is rolled to disk, and feeds the stock parser the same part to show it would
    be. The public parts used are the constructor's keywords, ``parse()``,
    ``FormData.multi_items()`` and ``MultiPartException``."""

    spool_max_size = UPLOAD_BODY_LIMIT_BYTES


async def read_upload(request: Request) -> tuple[str, bytes]:
    """The kind and the file's bytes of a form of exactly one ``kind`` field and
    one ``file`` part, or the 422 that says it is not. The file name and the part's
    declared type are not read. A body over the limit raises the 413 where the
    stream is read."""
    media = request.headers.get("content-type", "").partition(";")[0].strip().lower()
    if media != "multipart/form-data":
        raise problem(("header", "content-type"), CONTENT_TYPE_PROBLEM)
    parser = MemoryOnlyParser(
        request.headers,
        request.stream(),
        max_files=1,
        max_fields=1,
        max_part_size=KIND_FIELD_MAX_BYTES,
    )
    try:
        form = await parser.parse()
    except MultiPartException:
        raise problem(("body",), FORM_PROBLEM) from None
    try:
        items = form.multi_items()
        kinds = [v for name, v in items if name == "kind" and isinstance(v, str)]
        files = [v for name, v in items if name == "file" and isinstance(v, UploadFile)]
        if len(items) != 2 or len(kinds) != 1 or len(files) != 1:
            raise problem(("body",), FORM_PROBLEM)
        if kinds[0] not in FILE_KINDS:
            raise problem(("body", "kind"), KIND_PROBLEM)
        return kinds[0], await files[0].read()
    finally:
        await form.close()


@dataclass(frozen=True, slots=True)
class StoreLimits:
    """What bounds the store as a whole, the same for every caller: the bytes and
    the rows it may hold, and how many files it takes in a minute."""

    ceiling_bytes: int
    ceiling_rows: int
    rate_per_minute: int


# ── the transaction ─────────────────────────────────────────────────────────
def store_file(
    dsn: str,
    tenant: str,
    claim_id: str,
    kind: str,
    content: bytes,
    limits: StoreLimits,
) -> StoredFile:
    """Check, then store the file and its one audit row in one transaction.

    The refusals are raised as ``HTTPException`` in the contract's order, with
    nothing written. The claim, the size and the type are judged before any
    lock is taken. Then the claim's advisory lock, then the store's (always in
    that order, held to the commit), and only then are the sums read: so the
    claim's ceilings and the global ones are exact, and an upload that waits
    for a lock reads what the one before it committed. The same file twice on
    one claim is refused by a read, and, as the only answer that cannot be
    raced, by the table's unique constraint on the insert. Nothing here logs or
    returns a database error's text: its DETAIL quotes the failing row, whose
    first bytes are the file's. The route logs the class and SQLSTATE."""
    # What the bytes alone decide is worked out before a connection is opened, and
    # is raised only where the contract's order says (after the claim is found).
    size = len(content)
    media_type = sniff_media_type(content)
    digest = hashlib.sha256(content)
    with connect(dsn, SERVICE_NAME) as conn:
        if conn.execute(CLAIM_EXISTS_SQL, (claim_id, tenant)).fetchone() is None:
            raise HTTPException(404, NO_SUCH_CLAIM_DETAIL)
        if size == 0:
            raise problem(("body", "file"), EMPTY_PROBLEM)
        if size > MAX_FILE_BYTES:
            raise HTTPException(413, TOO_LARGE_DETAIL)
        if media_type is None:
            raise HTTPException(415, UNSUPPORTED_DETAIL)
        conn.execute(LOCK_CLAIM_SQL, (CLAIM_LOCK_CLASS, claim_id))
        # An aggregate answers exactly one row.
        ((count, held_bytes),) = conn.execute(CLAIM_FILES_SQL, (claim_id,)).fetchall()
        if count >= MAX_FILES_PER_CLAIM:
            raise UploadConflict("claim_full", CLAIM_FULL_DETAIL)
        if held_bytes + size > MAX_CLAIM_BYTES:
            raise UploadConflict("claim_bytes", CLAIM_BYTES_DETAIL)
        if conn.execute(DUPLICATE_SQL, (claim_id, digest.digest())).fetchone():
            raise UploadConflict("duplicate", DUPLICATE_DETAIL)
        conn.execute(LOCK_STORE_SQL, (STORE_LOCK_CLASS,))
        ((rows, stored_bytes, recent),) = conn.execute(
            STORE_TOTALS_SQL, (RATE_WINDOW_SECONDS,)
        ).fetchall()
        if stored_bytes + size > limits.ceiling_bytes or rows + 1 > limits.ceiling_rows:
            raise HTTPException(507, STORE_FULL_DETAIL)
        # The rate, a second ceiling on the store as a whole (never a caller's
        # own: there is no caller identity), read under the store's lock so that
        # uploads at the same moment count each other. After the ceilings: a full
        # store says so rather than "slow down".
        if recent >= limits.rate_per_minute:
            raise HTTPException(
                429, RATE_DETAIL, headers={"Retry-After": str(RATE_RETRY_SECONDS)}
            )
        file_id = uuid.uuid4()
        try:
            conn.execute(
                INSERT_FILE_SQL,
                (file_id, claim_id, kind, media_type, size, digest.digest(), content),
            )
        except psycopg.errors.UniqueViolation as exc:
            # Only the table's rule of one file per claim and hash is a duplicate;
            # any other unique violation (an identifier) is an error, and goes on
            # to be answered as one. No text of the 409 is kept: ``from None``.
            if exc.diag.constraint_name != DUPLICATE_CONSTRAINT:
                raise
            raise UploadConflict("duplicate", DUPLICATE_DETAIL) from None
        record_event(
            conn,
            AuditEvent(
                service=SERVICE_NAME,
                event=UPLOAD_EVENT,
                outcome=UPLOAD_OUTCOME,
                reason=UPLOAD_REASON,
                tenant=tenant,
                reference=claim_id,
            ),
        )
    return StoredFile(
        file_id=file_id,
        kind=kind,
        media_type=media_type,
        size_bytes=size,
        sha256=digest.hexdigest(),
    )


# ── the route ───────────────────────────────────────────────────────────────
async def refuse_cross_site(request: Request) -> None:
    """A post another site made is refused before a byte of it is read (T-70). A
    multipart post is one a browser sends cross-site with no preflight, which a
    JSON route is not; a client that sends none of the headers passes, as on
    every route of the app (T-69)."""
    headers = request.headers
    if is_cross_site(
        headers.get("origin"), headers.get("host"), headers.get("sec-fetch-site")
    ):
        raise HTTPException(403, CROSS_SITE_DETAIL)


REQUEST_BODY = {
    "requestBody": {
        "required": True,
        "content": {
            "multipart/form-data": {
                "schema": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["kind", "file"],
                    "properties": {
                        "kind": {"type": "string", "enum": list(FILE_KINDS)},
                        "file": {
                            "type": "string",
                            "format": "binary",
                            "description": (
                                "Synthetic files only; never a real person's "
                                "document. A PDF, a JPEG or a PNG of 1 byte to "
                                "1 MiB, known by its first bytes alone; its name "
                                "and declared type are ignored and never kept."
                            ),
                        },
                    },
                }
            }
        },
    }
}
CROSS_SITE_RESPONSE = {
    403: {"model": ErrorBody, "description": "The post came from another site."}
}


async def refuse_encoded_path(request: Request) -> None:
    """A request whose RAW path holds a ``%`` is the 404 of a path that is no
    route, before a byte of its body is read. uvicorn decodes the path before the
    router sees it, so ``/claims/CLM-0001/%66iles`` reaches this route; but the
    edge's second route matches the raw path, so the same request may be served
    by the first, which has no rate limit. The app's brakes must not depend on
    which route served the path, and a client has no use for an encoded path
    here: every character of a valid one is plain. The query is not the path."""
    raw = request.scope.get("raw_path")
    path = raw if raw is not None else request.scope["path"].encode()
    if b"%" in path.partition(b"?")[0]:
        raise HTTPException(404, HTTPStatus.NOT_FOUND.phrase)


RETRY_AFTER = {
    "Retry-After": {
        "description": "Seconds to wait before trying again.",
        "schema": {"type": "integer"},
    }
}
# The answers the route documents beyond its own 201 and the 422 FastAPI adds. The
# 408, 500 and 503 are ``UploadErrorBody``: the route's own answers say which
# claim, and the middleware's 500 and the audit log's 503 do not. The 503 is the
# busy answer too (too many at once, a lock that did not come in time), which asks
# the caller to wait; the 415 is about the file's type, which a wrong content type
# of the request (a 422) is not.
REFUSALS = (
    CROSS_SITE_RESPONSE
    | {
        408: {
            "model": UploadErrorBody,
            "description": "The file did not arrive within the route's deadline.",
        },
        415: {
            "model": ErrorBody,
            "description": (
                "The file is not a PDF, a JPEG or a PNG, by its first bytes."
            ),
        },
        429: {
            "model": ErrorBody,
            "description": "The store took too many files in the last minute.",
            "headers": RETRY_AFTER,
        },
        503: {
            "model": UploadErrorBody,
            "description": (
                "Busy (too many uploads at once, or a lock not free in time: try "
                "again after the wait), or the database or the audit log is "
                "unavailable (no wait is given)."
            ),
            "headers": RETRY_AFTER,
        },
    }
    | error_responses(404, 409, 413, 507)
    | error_responses(500, model=UploadErrorBody)
)


@dataclass(frozen=True, slots=True)
class UploadRefusal:
    """A refusal the handler returns rather than raises: the 503 that is not a
    database that is down (too many uploads at once, a lock that did not come in
    time), the 408 of a body that did not arrive in time, and a database failure.
    ``retry_after`` is the seconds to wait, when the caller is to wait. The other
    refusals are ``HTTPException`` s: the JSON route lets its handler answer them,
    the claimant's form turns them into pages."""

    status: int
    detail: str
    retry_after: int | None = None


@dataclass(frozen=True, slots=True)
class UploadAbandoned:
    """The client went away before its body was whole. Nobody receives the answer,
    so a route sends an empty 400 and nothing is stored."""


# What both routes run, the JSON route here and the claimant's form in
# ``claimant_uploads``: one function, so the permits are one pool and the checks
# and their order are the same by construction.
type UploadHandler = Callable[
    [str, Request], Awaitable[StoredFile | UploadRefusal | UploadAbandoned]
]


def refusal_response(refusal: UploadRefusal, claim_id: str) -> JSONResponse:
    """The JSON route's answer to an ``UploadRefusal``: the shaped error with the
    claim, and the wait when there is one."""
    response = answer(refusal.status, refusal.detail, claim_id)
    if refusal.retry_after is not None:
        response.headers["Retry-After"] = str(refusal.retry_after)
    return response


def abandoned_response() -> Response:
    """The answer to a client that is gone: empty, a 400. Nobody receives it."""
    return Response(status_code=400)


def add_upload_routes(
    app: FastAPI,
    *,
    dsn: str,
    tenant: str,
    tracer: Tracer,
    limits: StoreLimits,
) -> UploadHandler:
    """Add ``POST /claims/{claim_id}/files``. Called only when the switch is on:
    with it off the route does not exist, and its path keeps the app's limit.
    Returns the handler that the route and its HTML twin both run.

    The route's own brakes, none of which depends on the edge: a raw path with a
    ``%`` is refused, at most ``MAX_CONCURRENT_UPLOADS`` uploads are read and
    stored at once (the rest are told so at once, before a byte of their body is
    read), and the store takes at most ``limits.rate_per_minute`` files a minute."""
    # One per app, never waited for: the event loop is one thread, so ``locked``
    # and the acquire after it are not separated by anything that could run.
    permits = asyncio.Semaphore(MAX_CONCURRENT_UPLOADS)

    async def handle_upload(
        claim_id: str, request: Request
    ) -> StoredFile | UploadRefusal | UploadAbandoned:
        if permits.locked():
            return UploadRefusal(503, SATURATED_DETAIL, BUSY_RETRY_SECONDS)
        # Released on every exit: a refusal, an exception, a deadline, a client
        # that goes away (uvicorn then raises ``ClientDisconnect`` from the body
        # read; a task that is cancelled instead waits for the store's thread,
        # which has finished by then).
        async with permits:
            try:
                async with asyncio.timeout(READ_DEADLINE_SECONDS):
                    kind, content = await read_upload(request)
            except TimeoutError:
                return UploadRefusal(408, READ_TIMEOUT_DETAIL)
            except ClientDisconnect as exc:
                # Class only: the exception holds nothing of the request. Nothing
                # was stored, and there is nobody to answer.
                logger.info(
                    "an upload's client went away before its body was whole: %s",
                    type(exc).__name__,
                )
                return UploadAbandoned()
            with start_span(tracer, "claims.upload") as span:
                set_span_attributes(
                    span, {"meridian.claim_id": claim_id, "meridian.tenant": tenant}
                )
                try:
                    return await run_in_threadpool(
                        store_file, dsn, tenant, claim_id, kind, content, limits
                    )
                except psycopg.Error as exc:
                    mark_error(span, exc)
                    # A statement cancelled by the 10 s bound is a wait for a lock
                    # that did not end, not a database that is down: busy, one
                    # WARNING with the class and SQLSTATE, and not the error lines
                    # of a database fault (contention under the store's lock is
                    # normal).
                    if exc.sqlstate == QUERY_CANCELED_SQLSTATE:
                        logger.warning(
                            "upload to claim %s waited too long for a lock: %s "
                            "(sqlstate %s)",
                            claim_id,
                            type(exc).__name__,
                            exc.sqlstate,
                        )
                        return UploadRefusal(503, LOCK_BUSY_DETAIL, BUSY_RETRY_SECONDS)
                    status, detail = claim_database_failure(exc, claim_id)
                    return UploadRefusal(status, detail)

    @app.post(
        UPLOAD_PATH,
        status_code=201,
        response_model=StoredFile,
        tags=["claims"],
        summary=(
            "Store a claimant's file (a PDF, a JPEG or a PNG) with the claim. "
            "Synthetic files only; never a real person's document."
        ),
        dependencies=[Depends(refuse_cross_site), Depends(refuse_encoded_path)],
        openapi_extra=REQUEST_BODY,
        responses=REFUSALS,
    )
    async def upload_file(claim_id: ClaimId, request: Request) -> StoredFile | Response:
        result = await handle_upload(claim_id, request)
        if isinstance(result, UploadRefusal):
            return refusal_response(result, claim_id)
        if isinstance(result, UploadAbandoned):
            return abandoned_response()
        return result

    return handle_upload
