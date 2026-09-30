"""The replay provider: deterministic, labelled simulated, no fixture files."""

import hashlib
import json
import re

from meridian.platform.gateway.models import Message
from meridian.platform.gateway.replay import REPLAY_PREFIX, replay_chat

MESSAGES = (
    Message(role="system", content="You draft triage summaries."),
    Message(role="user", content="Storm damage to the roof, 2890 claimed."),
)
TEXT_FORMAT = re.compile(
    r"^Replay response \(simulated; no model was called\)\. "
    r"Request fingerprint: [0-9a-f]{12}\.$"
)


def test_the_same_messages_give_the_same_reply() -> None:
    assert replay_chat(MESSAGES) == replay_chat(MESSAGES)


def test_the_text_is_labelled_simulated_and_carries_a_fingerprint() -> None:
    assert TEXT_FORMAT.match(replay_chat(MESSAGES).text)
    assert "simulated" in REPLAY_PREFIX


def test_the_fingerprint_is_the_first_12_hex_of_the_canonical_json_sha256() -> None:
    canonical = json.dumps(
        [{"content": m.content, "role": m.role} for m in MESSAGES],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    expected = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]

    assert replay_chat(MESSAGES).text.endswith(f"fingerprint: {expected}.")


def test_a_pinned_fingerprint_guards_the_canonical_form() -> None:
    # If this changes, every stored replay reply changes with it.
    one = (Message(role="user", content="hello"),)
    assert replay_chat(one).text.endswith("fingerprint: d98167dd28f2.")


def test_a_different_message_gives_a_different_fingerprint() -> None:
    other = (*MESSAGES[:1], Message(role="user", content="Another claim."))

    assert replay_chat(other).text != replay_chat(MESSAGES).text


def test_the_role_is_part_of_the_fingerprint() -> None:
    as_user = (Message(role="user", content="same"),)
    as_assistant = (Message(role="assistant", content="same"),)

    assert replay_chat(as_user).text != replay_chat(as_assistant).text


def test_tokens_are_the_characters_divided_by_four_rounded_up() -> None:
    chars = sum(len(m.content) for m in MESSAGES)  # 27 + 39 = 66
    reply = replay_chat(MESSAGES)

    assert chars == 66
    assert reply.input_tokens == 17
    assert reply.output_tokens == -(-len(reply.text) // 4)


def test_a_partial_token_rounds_up() -> None:
    reply = replay_chat((Message(role="user", content="abcde"),))

    assert reply.input_tokens == 2
