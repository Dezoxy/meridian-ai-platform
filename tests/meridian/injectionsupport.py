"""The injection cases through the stack, answered by a model that obeys (S032).

``run_cases`` posts every case of ``data/synthetic/injection/cases.json`` to the
real services in process (``stacksupport.build_stack``) whose runtime is handed
an ``ObedientModel``: it answers every chat request with the verdict ``none``
whatever it is sent, so a run that reaches it is a run the screen did not stop.
The model keeps, in memory only, what it was asked, so a test can tell what
reached it. The cases' attack sentences are data: nothing here prints one.

A clause case first inserts its sentence into a stored clause of the wording, as
the owner role, immediately before the clause's closing sentence ("This
exclusion applies to claims for ..."), and puts the stored body back afterwards.
A sentence after the closing sentence would make the wording unreadable as an
exclusion: the clause would drop out of the candidates and the model would never
see it.
"""

import json
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field

import httpx
from dbsupport import OWNER, DatabaseHandle
from evalsupport import EVALUATION_DIR, ToolCapture
from servicesupport import GATEWAY_REPLY, REPO_ROOT, owner_rows
from stacksupport import Stack, model_answer, stored_proposals

from meridian.platform.common.db import connect
from meridian.platform.evaluation.report import AnsweredBy
from meridian.workloads.claims_triage.injection import (
    ClauseEdit,
    InjectionCase,
    Outcome,
)

INJECTION_DIR = REPO_ROOT / "data" / "synthetic" / "injection"
INJECTION_CASES = INJECTION_DIR / "cases.json"
INJECTION_MANIFEST = INJECTION_DIR / "manifest.json"
# What `make eval-baseline` writes (the baseline and the summary beside it).
INJECTION_BASELINE_PATH = EVALUATION_DIR / "claims-triage-injection-baseline.json"
INJECTION_SUMMARY_PATH = EVALUATION_DIR / "injection-summary.md"
BASELINE_COMMAND = "run make eval-baseline"
# The model's turns are answered by a script: simulated, whatever it is sent.
SCRIPTED = AnsweredBy(kind="scripted", label="simulated")

# Where a clause's text goes: before the last occurrence of this sentence.
CLOSING_SENTENCE = "This exclusion applies to claims for"
SELECT_BODY = (
    "SELECT body FROM knowledge.chunks "
    "WHERE product = %s AND wording_version = %s AND clause = %s"
)
UPDATE_BODY = (
    "UPDATE knowledge.chunks SET body = %s "
    "WHERE product = %s AND wording_version = %s AND clause = %s"
)
RUN_FAILURES = (
    "SELECT reference, reason FROM audit.events "
    "WHERE event = 'run.failed' ORDER BY recorded_at"
)
ALL_BODIES = (
    "SELECT product, wording_version, clause, body FROM knowledge.chunks "
    "ORDER BY product, wording_version, clause"
)


# ── the model ───────────────────────────────────────────────────────────────
@dataclass
class ObedientModel:
    """The ``http_client`` of a runtime whose model obeys every injection.

    It answers every ``POST /v1/chat`` with the verdict ``none``, whatever the
    request holds, and looks nothing up. ``asked`` holds, in order and in memory
    only, the case that was being posted (``current``, set by ``run_cases``;
    None outside a run) and the user message of each request."""

    current: str | None = None
    asked: list[tuple[str | None, str]] = field(default_factory=list)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert (request.method, request.url.path) == ("POST", "/v1/chat")
        messages = json.loads(request.content)["messages"]
        self.asked.append((self.current, messages[1]["content"]))
        text = model_answer("none", None, "The description states no excluded fact.")
        reply = {**GATEWAY_REPLY, "output": {"text": text, "finish_reason": "stop"}}
        return httpx.Response(200, json=reply)

    def http(self) -> httpx.Client:
        return httpx.Client(
            base_url="http://gateway.invalid", transport=httpx.MockTransport(self)
        )

    @property
    def calls(self) -> int:
        return len(self.asked)

    def messages_for(self, case: str) -> list[str]:
        """The user messages the model was sent while ``case`` was posted."""
        return [message for name, message in self.asked if name == case]


# ── the wording ─────────────────────────────────────────────────────────────
def stored_bodies(db: DatabaseHandle) -> list[tuple]:
    """Every stored clause: product, wording version, clause and body."""
    return owner_rows(db, ALL_BODIES)


def _set_body(db: DatabaseHandle, edit: ClauseEdit, body: str) -> None:
    key = (edit.product, edit.wording_version, edit.clause)
    with connect(db.dsn(OWNER), "test-injection") as conn:
        changed = conn.execute(UPDATE_BODY, (body, *key)).rowcount
    if changed != 1:
        raise AssertionError(f"{changed} stored clauses changed for {key}, not one")


def insert_before_closing_sentence(stored: str, inserted: str) -> str:
    """``stored`` with ``inserted`` and a space put immediately before the last
    occurrence of the closing sentence, so the wording is still read as an
    exclusion and the clause reaches the model. ``AssertionError`` when ``stored``
    does not hold the closing sentence: the edit would change nothing."""
    position = stored.rfind(CLOSING_SENTENCE)
    if position < 0:
        raise AssertionError("the stored clause does not hold its closing sentence")
    return f"{stored[:position]}{inserted} {stored[position:]}"


@contextmanager
def clause_inserted(db: DatabaseHandle, edit: ClauseEdit) -> Iterator[None]:
    """The stored clause with the case's sentence inserted before its closing
    sentence; the stored body is put back on leaving. ``AssertionError`` when the
    clause is not stored exactly once, lacks its closing sentence, or an update
    does not change exactly one row."""
    key = (edit.product, edit.wording_version, edit.clause)
    rows = owner_rows(db, SELECT_BODY, key)
    if len(rows) != 1:
        raise AssertionError(f"{len(rows)} stored clauses found for {key}, not one")
    stored = rows[0][0]
    edited = insert_before_closing_sentence(stored, edit.inserted)
    try:
        _set_body(db, edit, edited)
        yield
    finally:
        _set_body(db, edit, stored)


# ── the run ─────────────────────────────────────────────────────────────────
def _failures(db: DatabaseHandle) -> dict[str, str]:
    """The reason of each claim's last ``run.failed`` audit row."""
    return dict(owner_rows(db, RUN_FAILURES))


def run_cases(
    stack: Stack,
    cases: Sequence[InjectionCase],
    capture: ToolCapture,
    *,
    model: ObedientModel | None = None,
    statuses: dict[str, int] | None = None,
) -> dict[str, Outcome]:
    """Post each case's claim in order and read what the run did.

    ``capture`` is installed by the caller (it reads the tool client's calls);
    ``model``, when given, is told which case is being posted, and ``statuses``
    receives each post's HTTP status. A post answered with neither 201 nor a run
    failure is not hidden: its outcome holds what was found (usually no proposal
    and no failure), which the ``ended`` grader says is not how a run ends."""
    for case in cases:
        if model is not None:
            model.current = case.case
        try:
            if case.clause is None:
                response = stack.post(case.claim)
            else:
                with clause_inserted(stack.db, case.clause):
                    response = stack.post(case.claim)
        finally:
            if model is not None:
                model.current = None
        if statuses is not None:
            statuses[case.case] = response.status_code
    proposals = stored_proposals(stack.db)
    failures = _failures(stack.db)
    tools = capture.by_claim(stack.db)
    return {
        case.case: Outcome(
            proposal=proposals.get(case.case),
            failure=failures.get(case.case),
            tools=tools.get(case.case, ()),
        )
        for case in cases
    }
