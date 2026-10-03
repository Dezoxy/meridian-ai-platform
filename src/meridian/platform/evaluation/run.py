"""Run a workload's golden set against a deployed stack (S050, T-78).

``run_cases`` posts each of the workload's submissions, one at a time, and reads
the answer of each case that ran. It sends nothing else: no decision, no
withdrawal. A response's body is never kept in a failure or printed; only its
status is. The HTTP client is the caller's, so a test passes a transport.
"""

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

import httpx
from pydantic import JsonValue

from meridian.platform.evaluation.workload import Submission, WorkloadEvaluation

CREATED = 201  # the stack took the claim and triaged it: the case ran
ALREADY_THERE = 409  # the stack already has the case: it is skipped
ANSWERED = 200
NO_ANSWER = "no answer from the stack"
NOT_JSON = "the answer is not JSON"


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


def _send(
    http: httpx.Client, method: str, path: str, body: Mapping[str, JsonValue] | None
) -> httpx.Response | None:
    """The response, or None when the request did not complete. The exception's
    text can quote the address, so it is not kept."""
    try:
        return http.request(method, path, json=None if body is None else dict(body))
    except httpx.HTTPError:
        return None


def _status_text(response: httpx.Response | None) -> str:
    return NO_ANSWER if response is None else f"status {response.status_code}"


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
        response = _send(http, "POST", submission.path, submission.body)
        if response is not None and response.status_code == ALREADY_THERE:
            skipped.append(submission.case)
        elif response is not None and response.status_code == CREATED:
            ran.append(submission.case)
            sleep(pace)  # the next case finds the tenant's window clear
        else:
            reasons[submission.case] = _status_text(response)
    return ran, skipped, reasons


def _read_answers(
    http: httpx.Client, evaluation: WorkloadEvaluation, ran: list[str]
) -> tuple[dict[str, JsonValue], dict[str, str]]:
    answers: dict[str, JsonValue] = {}
    reasons: dict[str, str] = {}
    for case in ran:
        response = _send(http, "GET", evaluation.answer_path(case), None)
        if response is None or response.status_code != ANSWERED:
            reasons[case] = _status_text(response)
            continue
        try:
            answers[case] = response.json()
        except ValueError:
            reasons[case] = NOT_JSON
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
