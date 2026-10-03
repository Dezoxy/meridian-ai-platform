"""``run_cases``: post a workload's submissions to a stack, read the answers.

The HTTP client is an ``httpx.Client`` over a ``MockTransport``; the workload
is a stand-in that satisfies ``WorkloadEvaluation``.
"""

from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

import httpx
import pytest
from pydantic import JsonValue

from meridian.platform.evaluation.report import Report
from meridian.platform.evaluation.run import (
    ALREADY_THERE,
    CREATED,
    RunOutcome,
    run_cases,
)
from meridian.platform.evaluation.workload import Submission
from meridian.platform.registry.models import Registry

GOLDEN = Path("unused")
CASES = ("c-1", "c-2", "c-3", "c-4")
HOSTILE_BODY = "a-body-that-must-never-be-printed"


class FakeEvaluation:
    workload = "demo"

    def __init__(self, cases: Sequence[str] = CASES) -> None:
        self.cases = tuple(cases)

    def submissions(self, golden_set: Path) -> Sequence[Submission]:
        return tuple(Submission(c, "/things", {"id": c}) for c in self.cases)

    def answer_path(self, case: str) -> str:
        return f"/things/{case}/answer"

    def report(
        self, answers: Mapping[str, JsonValue], golden_set: Path, registry: Registry
    ) -> Report:
        raise AssertionError("run_cases never builds a report")


class Stack:
    """A stand-in stack: ``post`` and ``get`` decide the status per case."""

    def __init__(
        self,
        post: Callable[[str], int] = lambda case: 201,
        get: Callable[[str], int] = lambda case: 200,
        *,
        raises_on: tuple[str, ...] = (),
    ) -> None:
        self.post = post
        self.get = get
        self.raises_on = raises_on
        self.requests: list[tuple[str, str]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append((request.method, request.url.path))
        if request.method == "POST":
            case = request.read().decode().split('"')[3]
            if case in self.raises_on:
                raise httpx.ConnectError("refused", request=request)
            return httpx.Response(self.post(case), json={"detail": HOSTILE_BODY})
        case = request.url.path.split("/")[2]
        status = self.get(case)
        if status != 200:
            return httpx.Response(status, json={"detail": HOSTILE_BODY})
        return httpx.Response(200, json={"case": case})

    def client(self) -> httpx.Client:
        return httpx.Client(
            base_url="http://stack.test", transport=httpx.MockTransport(self)
        )


class Sleeps:
    def __init__(self) -> None:
        self.paces: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.paces.append(seconds)


def run(
    stack: Stack,
    *,
    limit: int | None = None,
    pace: float = 10.0,
    sleeps: Sleeps | None = None,
    cases: Sequence[str] = CASES,
) -> RunOutcome:
    return run_cases(
        stack.client(),
        FakeEvaluation(cases),
        GOLDEN,
        limit=limit,
        pace=pace,
        sleep=sleeps or Sleeps(),
    )


def test_every_case_that_answers_201_ran_and_its_answer_is_read() -> None:
    outcome = run(Stack())

    assert outcome.ran == CASES
    assert outcome.skipped == ()
    assert outcome.failed == ()
    assert outcome.answers == {case: {"case": case} for case in CASES}


def test_a_409_is_skipped_and_its_answer_is_not_read() -> None:
    stack = Stack(post=lambda case: 409 if case == "c-2" else 201)

    outcome = run(stack)

    assert outcome.ran == ("c-1", "c-3", "c-4")
    assert outcome.skipped == ("c-2",)
    assert outcome.failed == ()
    assert "c-2" not in outcome.answers
    assert ("GET", "/things/c-2/answer") not in stack.requests


def test_a_500_is_a_failure_of_that_case_with_its_status_and_the_run_goes_on() -> None:
    stack = Stack(post=lambda case: 500 if case == "c-2" else 201)

    outcome = run(stack)

    assert outcome.ran == ("c-1", "c-3", "c-4")
    assert outcome.failed == ("c-2",)
    assert outcome.reasons == {"c-2": "status 500"}
    assert "c-2" not in outcome.answers


def test_a_transport_error_is_a_failure_of_that_case_with_a_fixed_text() -> None:
    stack = Stack(raises_on=("c-3",))

    outcome = run(stack)

    assert outcome.ran == ("c-1", "c-2", "c-4")
    assert outcome.failed == ("c-3",)
    assert outcome.reasons == {"c-3": "no answer from the stack"}


@pytest.mark.parametrize("status", [200, 202, 204, 302, 400, 404, 422, 502, 504])
def test_only_201_and_409_are_not_failures_of_a_post(status: int) -> None:
    outcome = run(Stack(post=lambda case: status), cases=("c-1",))

    assert outcome.ran == ()
    assert outcome.skipped == ()
    assert outcome.failed == ("c-1",)
    assert outcome.reasons == {"c-1": f"status {status}"}


def test_limit_counts_the_cases_that_ran_and_not_the_skipped_ones() -> None:
    stack = Stack(post=lambda case: 409 if case in ("c-1", "c-2") else 201)

    outcome = run(stack, limit=1)

    assert outcome.skipped == ("c-1", "c-2")
    assert outcome.ran == ("c-3",)
    posts = [path for method, path in stack.requests if method == "POST"]
    assert len(posts) == 3  # c-4 was never posted


def test_a_failed_case_does_not_count_against_the_limit() -> None:
    stack = Stack(post=lambda case: 500 if case == "c-1" else 201)

    outcome = run(stack, limit=2)

    assert outcome.failed == ("c-1",)
    assert outcome.ran == ("c-2", "c-3")


def test_a_limit_of_none_runs_every_case() -> None:
    assert run(Stack(), limit=None).ran == CASES


def test_sleep_is_called_once_per_case_that_ran_with_the_pace() -> None:
    sleeps = Sleeps()
    stack = Stack(post=lambda case: {"c-1": 409, "c-2": 500}.get(case, 201))

    run(stack, pace=7.5, sleeps=sleeps)

    assert sleeps.paces == [7.5, 7.5]  # c-3 and c-4; not the skipped, not the failed


def test_the_sleep_follows_its_case_and_precedes_the_next_post() -> None:
    order: list[str] = []
    stack = Stack()
    original = stack.__call__

    def watch(request: httpx.Request) -> httpx.Response:
        order.append(f"{request.method} {request.url.path}")
        return original(request)

    client = httpx.Client(
        base_url="http://stack.test", transport=httpx.MockTransport(watch)
    )

    run_cases(
        client,
        FakeEvaluation(("c-1", "c-2")),
        GOLDEN,
        limit=None,
        pace=1.0,
        sleep=lambda seconds: order.append("sleep"),
    )

    assert order[:4] == ["POST /things", "sleep", "POST /things", "sleep"]


def test_only_the_submissions_are_posted_and_only_the_answers_are_read() -> None:
    stack = Stack(
        post=lambda case: 409 if case == "c-2" else 201,
        get=lambda case: 500 if case == "c-4" else 200,
    )

    run(stack)

    posts = {path for method, path in stack.requests if method == "POST"}
    gets = {path for method, path in stack.requests if method == "GET"}
    assert posts == {"/things"}
    assert gets == {"/things/c-1/answer", "/things/c-3/answer", "/things/c-4/answer"}
    assert {method for method, _ in stack.requests} == {"POST", "GET"}


def test_a_get_that_is_not_200_is_a_failure_of_the_case_that_ran() -> None:
    stack = Stack(get=lambda case: 404 if case == "c-2" else 200)

    outcome = run(stack)

    assert outcome.ran == CASES
    assert outcome.failed == ("c-2",)
    assert outcome.reasons == {"c-2": "status 404"}
    assert set(outcome.answers) == {"c-1", "c-3", "c-4"}


def test_an_answer_that_is_not_json_is_a_failure_with_a_fixed_text() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(201, json={})
        return httpx.Response(200, text=f"<html>{HOSTILE_BODY}")

    client = httpx.Client(
        base_url="http://stack.test", transport=httpx.MockTransport(respond)
    )

    outcome = run_cases(
        client,
        FakeEvaluation(("c-1",)),
        GOLDEN,
        limit=None,
        pace=0,
        sleep=Sleeps(),
    )

    assert outcome.failed == ("c-1",)
    assert outcome.reasons == {"c-1": "the answer is not JSON"}
    assert outcome.answers == {}


def test_a_get_transport_error_is_a_failure_of_the_case() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(201, json={})
        raise httpx.ReadTimeout("slow", request=request)

    client = httpx.Client(
        base_url="http://stack.test", transport=httpx.MockTransport(respond)
    )

    outcome = run_cases(
        client,
        FakeEvaluation(("c-1",)),
        GOLDEN,
        limit=None,
        pace=0,
        sleep=Sleeps(),
    )

    assert outcome.failed == ("c-1",)
    assert outcome.reasons == {"c-1": "no answer from the stack"}


def test_no_text_of_the_outcome_holds_a_response_body() -> None:
    stack = Stack(
        post=lambda case: 500 if case == "c-1" else 201,
        get=lambda case: 500 if case == "c-2" else 200,
    )

    outcome = run(stack)

    assert HOSTILE_BODY not in repr(outcome)


def test_the_post_body_is_the_submission_and_the_codes_are_named() -> None:
    seen: list[bytes] = []

    def respond(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            seen.append(request.read())
            return httpx.Response(201)
        return httpx.Response(200, json={})

    client = httpx.Client(
        base_url="http://stack.test", transport=httpx.MockTransport(respond)
    )

    run_cases(
        client, FakeEvaluation(("c-1",)), GOLDEN, limit=None, pace=0, sleep=Sleeps()
    )

    assert seen == [b'{"id":"c-1"}']
    assert (CREATED, ALREADY_THERE) == (201, 409)


def test_an_outcome_is_immutable() -> None:
    outcome = run(Stack(), cases=("c-1",))

    with pytest.raises(AttributeError):
        outcome.ran = ()  # type: ignore[misc]
