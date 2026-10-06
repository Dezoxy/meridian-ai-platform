"""How the Claims API triages a claim (moved from ``app.py``, S048).

The request that moves a claim to ``triaging`` owns its triage: it posts the
run's facts to the Agent Runtime, then closes the triage in one transaction,
storing the proposal and moving the claim on. A failure moves the claim to
``triage_failed`` and answers 502, 503 or 504 with the claim's ID (and the
run's, when there is one). The facts the run is sent carry neither the
claimant's name nor e-mail address (S047).
"""

import logging
import re
import unicodedata
from collections.abc import Collection, Mapping, Sequence
from datetime import datetime
from typing import Any, NamedTuple
from uuid import UUID, uuid4

import httpx
import psycopg
from fastapi import HTTPException
from fastapi.responses import JSONResponse
from opentelemetry.trace import Span
from psycopg.types.json import Jsonb
from pydantic import ValidationError
from pydantic_core import ErrorDetails

from meridian.platform.common.db import connect
from meridian.platform.common.http import database_failure, error_answer
from meridian.platform.common.telemetry import mark_error, set_span_attributes
from meridian.platform.guardrails import EMAIL_PLACEHOLDER, PLACEHOLDERS, redact
from meridian.runtime.models import RunResponse, RunState
from meridian.workloads.claims_triage.lifecycle import (
    AGENT as AGENT,
)
from meridian.workloads.claims_triage.lifecycle import (
    MAX_TRIAGES_PER_CLAIM,
    RULES_APPROVED,
    RULES_REFERRED,
    RULES_REQUESTED_DOCUMENTS,
    SERVICE_NAME,
    TRIAGE_FAILED,
    TRIAGE_LEASE_SECONDS,
    TRIAGE_RECLAIMED,
    TRIAGE_RETRIED,
    TRIAGE_STARTED,
    LifecycleState,
    Transition,
    move_claim,
)
from meridian.workloads.claims_triage.lifecycle import (
    RUNTIME_TIMEOUT_SECONDS as RUNTIME_TIMEOUT_SECONDS,
)
from meridian.workloads.claims_triage.meters import ClaimsMeters
from meridian.workloads.claims_triage.models import (
    Claimant,
    ClaimFacts,
    ClaimResponse,
    ClaimSubmission,
    DecisionFailure,
    ProposalSummary,
    Route,
)
from meridian.workloads.claims_triage.proposal import TriageProposal

# The calls to the runtime moved to ``runtime_calls`` (S037); this module still
# offers each name it offered, for the modules and tests that import them here.
from meridian.workloads.claims_triage.runtime_calls import (
    END_RUN_TIMEOUT_SECONDS as END_RUN_TIMEOUT_SECONDS,
)
from meridian.workloads.claims_triage.runtime_calls import (
    HTTP_GATEWAY_TIMEOUT as HTTP_GATEWAY_TIMEOUT,
)
from meridian.workloads.claims_triage.runtime_calls import (
    RuntimeCallError as RuntimeCallError,
)
from meridian.workloads.claims_triage.runtime_calls import (
    end_run as end_run,
)
from meridian.workloads.claims_triage.runtime_calls import (
    resume_run as resume_run,
)
from meridian.workloads.claims_triage.runtime_calls import (
    runtime_timeout as runtime_timeout,
)
from meridian.workloads.claims_triage.runtime_calls import (
    start_run as start_run,
)

RUN_FAILED_DETAIL = "the triage run did not complete; the claim is stored"
RUN_TIMEOUT_DETAIL = "the triage run timed out; the claim is stored"
PROPOSAL_LOST_DETAIL = "the proposal could not be stored; the claim is stored"
HAS_PROPOSAL_DETAIL = "the claim already has a triage proposal"
BEING_TRIAGED_DETAIL = "the claim is being triaged"
TAKEN_OVER_DETAIL = "the triage was taken over by another request"
DIFFERENT_SUBMISSION_DETAIL = "the claim exists with a different submission"
# The cap's number in words, so the text follows the constant (T-38).
NUMBER_WORDS = ("zero", "one", "two", "three", "four", "five", "six", "seven", "eight")
TRIAGE_CAP_DETAIL = (
    f"the claim has been triaged {NUMBER_WORDS[MAX_TRIAGES_PER_CLAIM]} times; "
    "an adjuster decides it"
)
# The names of the documents that arrived for a claim after its submission, in
# the order they arrived (those of one arrival, by name).
ARRIVED_DOCUMENTS_SQL = (
    "SELECT name FROM claims.claim_documents WHERE claim_id = %s "
    "ORDER BY received_at, name"
)

# What a run's answer means for the claim: its status and the proposal's route.
RUN_OUTCOMES: Mapping[tuple[RunState, Route], Transition] = {
    ("Completed", "auto_approve"): RULES_APPROVED,
    ("Completed", "request_documents"): RULES_REQUESTED_DOCUMENTS,
    ("AwaitingApproval", "adjuster"): RULES_REFERRED,
}

logger = logging.getLogger(__name__)

# What stands for a key of the data in a location: ``extra="forbid"`` puts the
# key an unknown field was sent under in the error's location, and the key is
# the caller's (or the stored row's), not the model's.
DATA_KEY = "*"


def claim_database_failure(exc: psycopg.Error, claim_id: str) -> tuple[int, str]:
    """``database_failure`` for a failure that has a claim: the same answer, and
    one line more that names the claim with the error's class and SQLSTATE (the
    shared line cannot). Never the message, the SQL or its parameters."""
    logger.error(
        "database error on claim %s: %s (sqlstate %s)",
        claim_id,
        type(exc).__name__,
        exc.sqlstate or "none",
    )
    return database_failure(exc)


def invalid_fields(exc: ValidationError) -> tuple[tuple[str, str], ...]:
    """What failed to validate, as ``(dotted location, error type)`` pairs and
    nothing else: never the message, the input or the context, which quote the
    claimant's values. A list index is kept (``documents.3``); a key of the data
    is replaced (``DATA_KEY``). No model of the workload has a free-keyed
    mapping, so the keys of the data come only from an undeclared field."""
    errors = exc.errors(include_url=False, include_input=False, include_context=False)
    return tuple((_dotted(error), error["type"]) for error in errors)


def _dotted(error: ErrorDetails) -> str:
    parts = [str(part) for part in error["loc"]]
    if error["type"] == "extra_forbidden" and parts:
        parts[-1] = DATA_KEY
    return ".".join(parts)


NAME_PLACEHOLDER = "[name]"
# The shortest part of a name that is replaced on its own: a shorter one ("Li",
# "Jr.") is also an ordinary word or an initial.
MIN_NAME_PART_LETTERS = 3


CURLY_APOSTROPHE = chr(0x2019)
# The characters a name is split on into the parts that are replaced on their
# own: white space, hyphens, apostrophes (straight and curly) and dots.
NAME_PART_SEPARATORS = re.compile(r"[\s\-'" + CURLY_APOSTROPHE + r".]+")
# A name is matched as a whole word: bounded by letters and digits only, so an
# underscore or a square bracket next to it is not a boundary that protects it.
NAME_BOUNDARY_BEFORE = r"(?<![^\W_])"
NAME_BOUNDARY_AFTER = r"(?![^\W_])"
# The exact placeholders, tried first at every position: a part that is a
# placeholder's word ("Name", "Email") must not turn "[name]" into "[[name]]".
PLACEHOLDER_PATTERN = "|".join(
    re.escape(placeholder) for placeholder in (*PLACEHOLDERS.values(), NAME_PLACEHOLDER)
)


def _name_alternatives(name: str) -> list[str]:
    """The patterns for a claimant's name, the longest first: the full name (any
    white space between its words), then each part, each with at least three
    letters. No alternative is empty: an empty one matches at every boundary.
    The minimum also bounds the copy's growth: a one-letter name would turn
    every "A" into ``[name]``."""
    alternatives = (
        [r"\s+".join(re.escape(word) for word in name.split())]
        if sum(char.isalpha() for char in name) >= MIN_NAME_PART_LETTERS
        else []
    )
    parts = {part for part in NAME_PART_SEPARATORS.split(name) if part}
    alternatives += [
        re.escape(part)
        for part in sorted(parts, key=len, reverse=True)
        if sum(char.isalpha() for char in part) >= MIN_NAME_PART_LETTERS
    ]
    return [alternative for alternative in alternatives if alternative]


def description_for_run(description: str, claimant: Claimant) -> str:
    """The description the run is sent (S047), in three steps: the claimant's
    e-mail address, ignoring case, becomes ``[email]``; ``redact`` replaces what
    it finds (so a third party's address that shares the claimant's surname is
    one address, not cut by a name); then the full name and each part of it of
    at least three letters become ``[name]``, each as a whole word and ignoring
    case, bounded by letters and digits only (a square bracket or an underscore
    next to it does not protect it). A pattern cannot find a name, and this API
    is the one place that knows it. The claimant's values are escaped: they are
    matched, never read as a pattern. One pass finds the exact placeholders
    first and keeps each as it is, then the name, so no placeholder is cut or
    nested. The description and the name are compared in Unicode form NFC, and
    the copy is NFC. The copy can be longer than the submission
    (``MAX_RUN_DESCRIPTION_CHARS``)."""
    emailless = re.sub(
        re.escape(claimant.email),
        EMAIL_PLACEHOLDER,
        unicodedata.normalize("NFC", description),
        flags=re.IGNORECASE,
    )
    redacted = unicodedata.normalize("NFC", redact(emailless).text)
    alternatives = _name_alternatives(unicodedata.normalize("NFC", claimant.name))
    if not alternatives:
        return redacted
    whole = "(?:" + "|".join(alternatives) + ")"
    pattern = re.compile(
        f"({PLACEHOLDER_PATTERN})|{NAME_BOUNDARY_BEFORE}{whole}{NAME_BOUNDARY_AFTER}",
        flags=re.IGNORECASE,
    )
    return pattern.sub(
        lambda match: match.group(1) or NAME_PLACEHOLDER,
        redacted,
    )


def facts_for_run(
    submission: ClaimSubmission, arrived: Sequence[str] = ()
) -> dict[str, Any]:
    """The facts a triage run is sent: for every triage, whatever starts it. The
    documents are the submission's, then each name that arrived, each name once
    and in order of first appearance (S048): the submission's bound counts
    names and does not require them to differ, and the documents route checks
    the bound over the distinct names, so the list must be that set."""
    # The runtime gets what the graph needs, not the claimant's name or email,
    # and a description with neither of them in it (S047).
    facts = submission.model_dump(mode="json", exclude={"claimant"})
    facts["description"] = description_for_run(
        submission.description, submission.claimant
    )
    facts["documents"] = list(dict.fromkeys([*submission.documents, *arrived]))
    # The graph validates these as ``ClaimFacts`` at every node. Facts that
    # would not validate fail the run as they always did; the log says why.
    try:
        ClaimFacts.model_validate(facts)
    except ValidationError as exc:
        logger.warning(
            "the facts of claim %s are not valid for the run: %s %s",
            submission.claim_id,
            type(exc).__name__,
            invalid_fields(exc),
        )
    return facts


def arrived_documents(conn: psycopg.Connection, claim_id: str) -> tuple[str, ...]:
    """The names of the documents that arrived for the claim, on the caller's
    connection (so inside its transaction)."""
    rows = conn.execute(ARRIVED_DOCUMENTS_SQL, (claim_id,)).fetchall()
    return tuple(name for (name,) in rows)


# When the claim moved to its state and whether that is longer ago than the
# triage lease (the same test ``take_triage`` makes in its one ``SELECT``); read
# by the triage-again route, which has locked the claim without its age.
TRIAGE_AGE_SQL = (
    "SELECT state_changed_at, "
    "state_changed_at < clock_timestamp() - make_interval(secs => %s) "
    "FROM claims.claims WHERE claim_id = %s AND tenant = %s"
)


def triage_outcome(run: RunResponse) -> tuple[TriageProposal, Transition]:
    """The run's proposal and the claim's move it leads to; anything else the
    runtime answered is outside the contract."""
    try:
        proposal = TriageProposal.model_validate(run.output)
    except ValidationError:
        raise RuntimeCallError(
            "the runtime's output is not a triage proposal",
            run_id=run.run_id,
            failure="bad-output",
        ) from None
    transition = RUN_OUTCOMES.get((run.status, proposal.route))
    if transition is None:
        raise RuntimeCallError(
            "the runtime's answer does not fit its proposal",
            run_id=run.run_id,
            failure="bad-output",
        )
    return proposal, transition


def store_claim(
    dsn: str,
    tenant: str,
    claim: dict[str, Any],
    *,
    stamped: Collection[str] = (),
) -> dict[str, Any]:
    """Store the claim and return the submission as it is stored. An existing
    one under this ID is left as it is; it is a 409 unless it is the same
    submission in every key not in ``stamped``, and then the stored one is
    returned: the keys the API stamped (the report date of the claimant's
    pages) keep their first value, so a form sent again later is the same
    submission."""
    with connect(dsn, SERVICE_NAME) as conn:
        cursor = conn.execute(
            "INSERT INTO claims.claims (claim_id, tenant, submission) "
            "VALUES (%s, %s, %s) ON CONFLICT (claim_id) DO NOTHING",
            (claim["claim_id"], tenant, Jsonb(claim)),
        )
        if cursor.rowcount == 1:
            return claim
        row = conn.execute(
            "SELECT submission, tenant FROM claims.claims WHERE claim_id = %s",
            (claim["claim_id"],),
        ).fetchone()
    # A claim of another tenant answers as a different submission does, so no
    # answer tells a caller that another tenant has the ID.
    if row is None or row[1] != tenant or not _same_but(row[0], claim, stamped):
        raise HTTPException(409, DIFFERENT_SUBMISSION_DETAIL)
    stored: dict[str, Any] = row[0]
    return stored


def _same_but(
    stored: Mapping[str, Any], claim: Mapping[str, Any], stamped: Collection[str]
) -> bool:
    """Whether the two submissions are equal in every key not in ``stamped``."""
    return {k: v for k, v in stored.items() if k not in stamped} == {
        k: v for k, v in claim.items() if k not in stamped
    }


class LapsedTriage(NamedTuple):
    """A triage taken over: the moment the claim moved, which the request that
    took it keeps (its closing update matches on it), and the run it must end
    after the commit, if the claim held one."""

    taken_at: datetime
    old_run: UUID | None


def take_over_lapsed_triage(
    conn: psycopg.Connection,
    *,
    claim_id: str,
    tenant: str,
    changed_at: datetime,
    triages: int,
    run_id: UUID | None,
) -> LapsedTriage | None:
    """Take over a triage whose lease lapsed, in the caller's transaction (the
    claim locked, found ``triaging`` since ``changed_at`` and holding ``run_id``).

    A ``triaging`` claim holds a run only when it was sent back (every other
    move into ``triaging`` passes none), and the send-back recorded that run's
    ``send_back`` word with the move. Its own request died before it ended the
    run or could not, so the takeover returns it, and the caller ends it, best
    effort, after the commit (``end_run``). The move drops the claim's run.

    A triage that died at the cap cannot be taken over (``move_claim`` refuses a
    move into triaging there), so the claim is moved to ``triage_failed`` instead
    and ``None`` answered: the caller commits that and then refuses with 409, so
    that an adjuster decides a claim whose triage failed. ``None`` is also the
    answer for a claim that did not move (not expected under the lock).
    """
    if triages >= MAX_TRIAGES_PER_CLAIM:
        move_claim(
            conn,
            TRIAGE_FAILED,
            claim_id=claim_id,
            tenant=tenant,
            changed_at=changed_at,
        )
        return None
    taken_at = move_claim(
        conn,
        TRIAGE_RECLAIMED,
        claim_id=claim_id,
        tenant=tenant,
        changed_at=changed_at,
    )
    return None if taken_at is None else LapsedTriage(taken_at, run_id)


def take_triage(
    dsn: str, tenant: str, claim_id: str
) -> tuple[datetime | None, LifecycleState, tuple[str, ...], UUID | None]:
    """Move the claim to ``triaging`` if it may be triaged now.

    Returns the moment it moved (the request keeps it: its closing update
    matches on it) or ``None``, the state the claim was found in, the names
    of the documents that arrived for it, read in the same transaction after the
    move (none for a claim that was never waiting for any), and the run to end
    after the commit: the one a send-back left on a triage that lapsed (see
    ``take_over_lapsed_triage``), otherwise ``None``. A claim that has
    been triaged ``MAX_TRIAGES_PER_CLAIM`` times is refused with 409.
    """
    with connect(dsn, SERVICE_NAME) as conn:
        row = conn.execute(
            "SELECT state, state_changed_at, "
            "state_changed_at < clock_timestamp() - make_interval(secs => %s), "
            "triages, run_id "
            "FROM claims.claims WHERE claim_id = %s AND tenant = %s "
            "FOR NO KEY UPDATE",
            (TRIAGE_LEASE_SECONDS, claim_id, tenant),
        ).fetchone()
        if row is None:
            # Not this tenant's (``store_claim`` refuses that first) or gone.
            raise HTTPException(409, DIFFERENT_SUBMISSION_DETAIL)
        state, changed_at, lapsed, triages, run_id = row
        at_cap = triages >= MAX_TRIAGES_PER_CLAIM
        old_run: UUID | None = None
        if state == "triaging" and lapsed:
            # Committed with the 409 when the claim is at the cap (see the helper).
            taken = take_over_lapsed_triage(
                conn,
                claim_id=claim_id,
                tenant=tenant,
                changed_at=changed_at,
                triages=triages,
                run_id=run_id,
            )
            moved_at, old_run = taken or (None, None)
        elif state in ("submitted", "triage_failed"):
            moved_at = (
                None
                if at_cap
                else move_claim(
                    conn,
                    TRIAGE_STARTED if state == "submitted" else TRIAGE_RETRIED,
                    claim_id=claim_id,
                    tenant=tenant,
                    changed_at=changed_at,
                )
            )
        else:
            return None, state, (), None
        arrived = () if moved_at is None else arrived_documents(conn, claim_id)
    if at_cap:
        raise HTTPException(409, TRIAGE_CAP_DETAIL)
    return moved_at, state, arrived, old_run


def _insert_proposal(
    conn: psycopg.Connection, claim_id: str, run_id: UUID, proposal: TriageProposal
) -> None:
    conn.execute(
        "INSERT INTO claims.triage_proposals "
        "(proposal_id, claim_id, run_id, route, reason, proposal) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        (
            uuid4(),
            claim_id,
            run_id,
            proposal.route,
            proposal.reason,
            Jsonb(proposal.model_dump(mode="json")),
        ),
    )


def close_triage(
    dsn: str,
    tenant: str,
    claim_id: str,
    run: RunResponse,
    proposal: TriageProposal,
    transition: Transition,
    taken_at: datetime,
) -> bool:
    """Store the proposal and move the claim on, in one transaction.

    ``False`` when the claim is no longer the one this request took (its lease
    ran out and another request took the triage over); nothing is stored then.
    """
    with connect(dsn, SERVICE_NAME) as conn:
        moved = move_claim(
            conn,
            transition,
            claim_id=claim_id,
            tenant=tenant,
            run_id=run.run_id,
            changed_at=taken_at,
        )
        if moved is None:
            return False
        _insert_proposal(conn, claim_id, run.run_id, proposal)
    return True


def fail_triage(
    dsn: str, tenant: str, claim_id: str, run_id: UUID | None, taken_at: datetime
) -> None:
    """Move the claim to ``triage_failed`` so a post triages it again. If that
    fails too the claim stays ``triaging`` until its lease runs out; the log
    says which claim and run it was."""
    try:
        with connect(dsn, SERVICE_NAME) as conn:
            move_claim(
                conn,
                TRIAGE_FAILED,
                claim_id=claim_id,
                tenant=tenant,
                run_id=run_id,
                changed_at=taken_at,
            )
    except psycopg.Error as exc:
        logger.error(
            "claim %s (run %s) could not be marked triage_failed: %s (sqlstate %s)",
            claim_id,
            run_id,
            type(exc).__name__,
            exc.sqlstate or "none",
        )


def answer(
    status: int, detail: str, claim_id: str, run_id: UUID | None = None
) -> JSONResponse:
    extra = {} if run_id is None else {"run_id": str(run_id)}
    return error_answer(status, detail, claim_id=claim_id, **extra)


def triage_claim(
    dsn: str,
    tenant: str,
    http: httpx.Client,
    span: Span,
    claim_id: str,
    submission: ClaimSubmission,
    meters: ClaimsMeters | None = None,
) -> ClaimResponse | JSONResponse:
    """Take the claim's triage, run it and close it. ``span`` is the caller's
    open span; the run's ID is set on it. A refusal (409) is raised as
    ``HTTPException``; a failure is answered with the claim's ID. The run is
    sent the submission's facts and the documents that arrived for the claim.
    ``meters`` counts the proposal when it is stored (see ``run_taken_triage``)."""
    try:
        taken_at, found_in, arrived, old_run = take_triage(dsn, tenant, claim_id)
    except psycopg.Error as exc:
        mark_error(span, exc)
        return answer(*claim_database_failure(exc, claim_id), claim_id)
    if taken_at is None:
        raise HTTPException(
            409,
            BEING_TRIAGED_DETAIL if found_in == "triaging" else HAS_PROPOSAL_DETAIL,
        )
    if old_run is not None:
        end_run(http, tenant, claim_id, old_run)
    result = run_taken_triage(
        dsn,
        tenant,
        http,
        span,
        claim_id,
        facts_for_run(submission, arrived),
        taken_at,
        meters=meters,
    )
    if isinstance(result, DecisionFailure):
        return answer(result.status, result.detail, claim_id, result.run_id)
    return result


def run_taken_triage(
    dsn: str,
    tenant: str,
    http: httpx.Client,
    span: Span,
    claim_id: str,
    facts: dict[str, Any],
    taken_at: datetime,
    *,
    meters: ClaimsMeters | None = None,
) -> ClaimResponse | DecisionFailure:
    """Run the triage this request took and close it: start the run, read its
    outcome, store the proposal and move the claim on, or move it to
    ``triage_failed``. A failure is a ``DecisionFailure`` (a status, a fixed
    text and the run's ID when there is one); a triage taken over by another
    request is a 409 raised as ``HTTPException``. ``span`` is the caller's open
    span; the run's ID is set on it. Every triage passes here, whichever route
    took it, so this is where ``meters`` counts it, once, by how it ended (the
    words are in ``meters.py``); a failure no branch expected is counted
    ``unexpected`` and raised, the 409 excepted: it is counted where it is raised."""
    try:
        return _run_triage(dsn, tenant, http, span, claim_id, facts, taken_at, meters)
    except Exception as exc:
        if meters is not None and not isinstance(exc, HTTPException):
            meters.triage_failed("unexpected")
        raise


def _run_triage(
    dsn: str,
    tenant: str,
    http: httpx.Client,
    span: Span,
    claim_id: str,
    facts: dict[str, Any],
    taken_at: datetime,
    meters: ClaimsMeters | None,
) -> ClaimResponse | DecisionFailure:
    try:
        run = start_run(http, tenant, claim_id, facts)
        proposal, transition = triage_outcome(run)
    except RuntimeCallError as exc:
        logger.error(
            "triage of %s failed: %s (runtime status %s, run %s)",
            claim_id,
            type(exc).__name__,
            exc.status_code,
            exc.run_id,
        )
        mark_error(span, exc)
        fail_triage(dsn, tenant, claim_id, exc.run_id, taken_at)
        if meters is not None:
            meters.triage_failed(exc.failure)
        return DecisionFailure(
            HTTP_GATEWAY_TIMEOUT if exc.timed_out else 502,
            RUN_TIMEOUT_DETAIL if exc.timed_out else RUN_FAILED_DETAIL,
            exc.run_id,
        )
    set_span_attributes(span, {"meridian.run_id": str(run.run_id)})
    try:
        closed = close_triage(
            dsn, tenant, claim_id, run, proposal, transition, taken_at
        )
    except psycopg.Error as exc:
        # The run finished and its proposal is lost unless the log says
        # which run it was: a retry triages again.
        logger.error(
            "proposal of claim %s (run %s) not stored: %s (sqlstate %s)",
            claim_id,
            run.run_id,
            type(exc).__name__,
            exc.sqlstate or "none",
        )
        mark_error(span, exc)
        fail_triage(dsn, tenant, claim_id, run.run_id, taken_at)
        if meters is not None:
            meters.triage_failed("proposal-lost")
        return DecisionFailure(503, PROPOSAL_LOST_DETAIL, run.run_id)
    if not closed:
        logger.warning(
            "triage of %s (run %s) was taken over; its proposal is dropped",
            claim_id,
            run.run_id,
        )
        if meters is not None:
            meters.triage_taken_over()
        raise HTTPException(409, TAKEN_OVER_DETAIL)
    response = ClaimResponse(
        claim_id=claim_id,
        state=transition.target,
        run_id=run.run_id,
        run_status=run.status,
        proposal=ProposalSummary(route=proposal.route, drafted_by=proposal.drafted_by),
    )
    if meters is not None:
        meters.proposal_stored(proposal)
    return response
