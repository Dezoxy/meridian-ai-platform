"""The brief's text is redacted before the Claims API stores it and before any
answer shows it (S037, W1b; threat h): the model can make up an e-mail address
or an IBAN, as it can in the triage's rationale, which is redacted by the same
function (``redact``) before it is returned.

Against a stand-in runtime that outputs the model's text with a canary e-mail
address and a test IBAN in it; the stack test (``test_claim_brief_stack.py``)
shows the same through the real runtime and a stub gateway."""

import uuid

from briefsupport import (
    BRIEF_TEXT,
    CLAIM_ID,
    Runtime,
    brief_rows,
    completed,
    make_client,
    paused,
)
from briefsupport import world as world  # a fixture: pytest finds it here
from dbsupport import DatabaseHandle

from meridian.platform.guardrails import redact
from meridian.workloads.claims_triage.briefs import MAX_BRIEF_CHARS

URL = f"/claims/{CLAIM_ID}/brief"
CANARY_EMAIL = "someone.made.up@example.com"
# A valid IBAN of the standard's examples: the screen checks its mod-97 digits.
CANARY_IBAN = "GB82 WEST 1234 5698 7654 32"
LEAKING_TEXT = f"Write to {CANARY_EMAIL} or pay {CANARY_IBAN}. {BRIEF_TEXT}"
REDACTED_TEXT = redact(LEAKING_TEXT).text
SHORTEST_ADDRESS = "a@b.co"


def client_of(db: DatabaseHandle, runtime: Runtime):
    return make_client(db.dsn("claims_api"), runtime)


def test_the_text_the_row_and_the_201_hold_are_the_redacted_text(
    world: DatabaseHandle,
) -> None:
    run_id = uuid.uuid4()
    runtime = Runtime(paused(run_id, LEAKING_TEXT))

    response = client_of(world, runtime).post(URL, json={})

    assert response.status_code == 201
    assert REDACTED_TEXT != LEAKING_TEXT
    assert CANARY_EMAIL not in REDACTED_TEXT
    assert "GB82" not in REDACTED_TEXT
    assert response.json()["brief"] == REDACTED_TEXT
    assert brief_rows(world) == [("awaiting_decision", run_id, REDACTED_TEXT)]


def test_the_get_answers_the_redacted_text_and_not_the_runtimes(
    world: DatabaseHandle,
) -> None:
    runtime = Runtime(paused(uuid.uuid4(), LEAKING_TEXT))
    client = client_of(world, runtime)
    client.post(URL, json={})

    response = client.get(URL)

    assert response.status_code == 200
    assert response.json()["brief"] == REDACTED_TEXT
    assert CANARY_EMAIL not in response.text
    assert "GB82" not in response.text


def test_the_decision_answers_the_stored_text_and_not_the_resumed_runs(
    world: DatabaseHandle,
) -> None:
    run_id = uuid.uuid4()
    runtime = Runtime(
        paused(run_id, LEAKING_TEXT), completed(run_id, True, LEAKING_TEXT)
    )
    client = client_of(world, runtime)
    client.post(URL, json={})

    response = client.post(
        f"{URL}/decision", json={"decision": "approve", "run": str(run_id)}
    )

    assert response.status_code == 200
    assert response.json()["state"] == "filed"
    assert response.json()["brief"] == REDACTED_TEXT
    assert CANARY_EMAIL not in response.text
    assert brief_rows(world) == [("filed", run_id, REDACTED_TEXT)]


def test_a_text_that_grows_when_redacted_is_cut_to_the_bound_and_stored(
    world: DatabaseHandle,
) -> None:
    # The placeholder is longer than the shortest address: a text of exactly the
    # bound ends up one character over once it is redacted.
    filler = "x" * (MAX_BRIEF_CHARS - len(SHORTEST_ADDRESS) - 1)
    text = f"{filler} {SHORTEST_ADDRESS}"
    assert len(text) == MAX_BRIEF_CHARS
    assert len(redact(text).text) == MAX_BRIEF_CHARS + 1
    run_id = uuid.uuid4()
    runtime = Runtime(paused(run_id, text))

    response = client_of(world, runtime).post(URL, json={})

    assert response.status_code == 201
    stored = brief_rows(world)[0][2]
    assert stored == redact(text).text[:MAX_BRIEF_CHARS]
    assert len(stored) == MAX_BRIEF_CHARS
    assert response.json()["brief"] == stored


def test_a_text_without_anything_to_redact_is_stored_as_it_came(
    world: DatabaseHandle,
) -> None:
    run_id = uuid.uuid4()
    runtime = Runtime(paused(run_id, BRIEF_TEXT))

    response = client_of(world, runtime).post(URL, json={})

    assert response.json()["brief"] == BRIEF_TEXT
    assert brief_rows(world) == [("awaiting_decision", run_id, BRIEF_TEXT)]
