"""Run a workload's golden set against a deployed stack (S050, T-78).

``run_cases`` posts each of the workload's submissions, one at a time, and reads
the answer of each case that ran. It sends nothing else: no decision, no
withdrawal. A response's body is never kept in a failure or printed; only its
status is. Only the body of a 200 answer is read, at most 1 MiB of it, and an
answer is kept only when the field the workload names holds the case it was
fetched for. The HTTP client is the caller's, so a test passes a transport.
"""

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

import httpx
from pydantic import JsonValue

from meridian.platform.common.jsonfile import JsonFileError, parse_json
from meridian.platform.evaluation.workload import Submission, WorkloadEvaluation

CREATED = 201  # the stack took the claim and triaged it: the case ran
ALREADY_THERE = 409  # the stack already has the case: it is skipped
ANSWERED = 200
# A proposal is a few KiB; this refuses a stack that sends more.
MAX_ANSWER_BYTES = 1024 * 1024
NO_ANSWER = "no answer from the stack"
NOT_JSON = "the answer is not JSON"
TOO_LARGE = "the answer is too large"
WRONG_CASE = "the answer is for another case"


@dataclass(frozen=True, slots=True)
class RunOutcome:
    """What a run did, by case ID. ``ran`` holds every case the stack took (201),
    including one whose answer then could not be read; ``failed`` holds the
    cases that could not be posted or read, with a fixed text each in
    ``reasons``; ``answers`` holds only the answers that were read (200)."""

    ran: tuple[str, ...]
    skipped: tuple[str, ...]
    failed: tuple[str, ...]
    reasons: Mapping[str, str]
    answers: Mapping[str, JsonValue]


@dataclass(frozen=True, slots=True)
class _Reply:
    """A response as the run keeps it: the status and, for the answer of a case
    that returned 200, its body, at most ``MAX_ANSWER_BYTES`` and one byte more
    when the body is longer."""

    status: int
    body: bytes


def _read_body(response: httpx.Response) -> bytes:
    """The body, read no further than one byte past the limit (decoded, so a
    compressed body counts as what it expands to)."""
    chunks: list[bytes] = []
    size = 0
    for chunk in response.iter_bytes():
        chunks.append(chunk)
        size += len(chunk)
        if size > MAX_ANSWER_BYTES:
            break
    return b"".join(chunks)[: MAX_ANSWER_BYTES + 1]


def _send(
    http: httpx.Client,
    method: str,
    path: str,
    body: Mapping[str, JsonValue] | None,
    *,
    reading: bool = False,
) -> _Reply | None:
    """The reply, or None when the request did not complete, whether on the
    way out or while the body was read. Only a 200 answer's body is read, and
    only when ``reading``; any other body is closed unread. The exception's text
    can quote the address, so it is not kept."""
    try:
        with http.stream(
            method, path, json=None if body is None else dict(body)
        ) as response:
            wanted = reading and response.status_code == ANSWERED
            return _Reply(response.status_code, _read_body(response) if wanted else b"")
    except httpx.HTTPError:
        return None


def _status_text(reply: _Reply | None) -> str:
    return NO_ANSWER if reply is None else f"status {reply.status}"


def _post_cases(
    http: httpx.Client,
    submissions: list[Submission],
    limit: int | None,
    pace: float,
    sleep: Callable[[float], None],
) -> tuple[list[str], list[str], dict[str, str]]:
    ran: list[str] = []
    skipped: list[str] = []
    reasons: dict[str, str] = {}
    for submission in submissions:
        if limit is not None and len(ran) >= limit:
            break
        reply = _send(http, "POST", submission.path, submission.body)
        if reply is not None and reply.status == ALREADY_THERE:
            skipped.append(submission.case)
        elif reply is not None and reply.status == CREATED:
            ran.append(submission.case)
            sleep(pace)  # the next case finds the tenant's window clear
        else:
            reasons[submission.case] = _status_text(reply)
    return ran, skipped, reasons


def _parsed(body: bytes) -> tuple[JsonValue, str | None]:
    """The JSON in ``body`` and ``None``, or ``None`` and the fixed reason it
    cannot be read: too long, not UTF-8, not JSON, nested too deeply (a
    ``RecursionError`` is an unreadable answer like any other) or holding a key
    twice. Nothing of the body is kept."""
    if len(body) > MAX_ANSWER_BYTES:
        return None, TOO_LARGE
    try:
        return parse_json(body.decode("utf-8")), None
    except (UnicodeDecodeError, JsonFileError, RecursionError):
        return None, NOT_JSON


def _names_the_case(answer: JsonValue, field: str, case: str) -> bool:
    """Whether ``answer`` is an object whose ``field`` is exactly ``case``."""
    return isinstance(answer, dict) and answer.get(field) == case


def _read_answers(
    http: httpx.Client, evaluation: WorkloadEvaluation, ran: list[str]
) -> tuple[dict[str, JsonValue], dict[str, str]]:
    answers: dict[str, JsonValue] = {}
    reasons: dict[str, str] = {}
    for case in ran:
        reply = _send(http, "GET", evaluation.answer_path(case), None, reading=True)
        if reply is None or reply.status != ANSWERED:
            reasons[case] = _status_text(reply)
            continue
        answer, problem = _parsed(reply.body)
        if problem is not None:
            reasons[case] = problem
        elif not _names_the_case(answer, evaluation.case_field, case):
            reasons[case] = WRONG_CASE
        else:
            answers[case] = answer
    return answers, reasons


def run_cases(
    http: httpx.Client,
    evaluation: WorkloadEvaluation,
    golden_set: Path,
    *,
    limit: int | None,
    pace: float,
    sleep: Callable[[float], None] = time.sleep,
) -> RunOutcome:
    """Post the submissions in order and read the answers of the cases that ran.

    A 201 means the case ran and 409 that the stack already has it (skipped);
    any other status, or a request that did not complete, fails that case and
    the run goes on. After a case that ran, ``sleep(pace)`` gives the next one a
    clear window. ``limit`` stops the run once that many cases ran; a skipped or
    failed case does not count."""
    submissions = list(evaluation.submissions(golden_set))
    ran, skipped, reasons = _post_cases(http, submissions, limit, pace, sleep)
    answers, unread = _read_answers(http, evaluation, ran)
    reasons = {**reasons, **unread}
    return RunOutcome(
        ran=tuple(ran),
        skipped=tuple(skipped),
        failed=tuple(s.case for s in submissions if s.case in reasons),
        reasons=MappingProxyType(reasons),
        answers=MappingProxyType(answers),
    )
