"""The replay provider: deterministic, labelled simulated, no fixture files."""

import hashlib
import json
import math
import re

import pytest
from servicesupport import REGISTRY_DIR

from meridian.platform.gateway.models import ChatRequest, EmbeddingRequest, Message
from meridian.platform.gateway.providers.base import (
    ChatProvider,
    EmbeddingProvider,
    EmbeddingReply,
    ProviderReply,
)
from meridian.platform.gateway.replay import (
    REPLAY_PREFIX,
    ReplayProvider,
    replay_chat,
    replay_embedding,
)
from meridian.platform.registry import load_registry
from meridian.platform.registry.models import Deployment

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


def test_the_replay_provider_returns_the_same_text_and_tokens_as_replay_chat() -> None:
    deployment = load_registry(REGISTRY_DIR).replay_deployment("chat")
    assert deployment is not None
    provider: ChatProvider = ReplayProvider()
    request = ChatRequest(messages=MESSAGES, max_output_tokens=7)

    reply = provider.chat(deployment, request, timeout_seconds=1.0)

    direct = replay_chat(MESSAGES)
    assert reply == ProviderReply(
        text=direct.text,
        finish_reason="stop",
        model=deployment.model,
        input_tokens=direct.input_tokens,
        output_tokens=direct.output_tokens,
    )


def test_a_response_schema_is_ignored_and_the_reply_is_not_json() -> None:
    schema = {
        "type": "object",
        "properties": {"verdict": {"type": "string"}},
        "required": ["verdict"],
        "additionalProperties": False,
    }
    deployment = load_registry(REGISTRY_DIR).replay_deployment("chat")
    assert deployment is not None
    plain = ChatRequest(messages=MESSAGES)
    asking = ChatRequest(messages=MESSAGES, response_schema=schema)

    without = ReplayProvider().chat(deployment, plain, timeout_seconds=1.0)
    with_schema = ReplayProvider().chat(deployment, asking, timeout_seconds=1.0)

    assert with_schema == without
    with pytest.raises(json.JSONDecodeError):
        json.loads(with_schema.text)


# ── the replay embedding (S045): SIMULATED, a hashed bag of words ───────────
EMBEDDING_TEXT = "Storm damage to the roof, claim 2890."


def cosine(a: tuple[float, ...], b: tuple[float, ...]) -> float:
    """The dot product; the vectors are unit length, so it is the cosine."""
    return sum(x * y for x, y in zip(a, b, strict=True))


def embedding_deployment() -> Deployment:
    found = load_registry(REGISTRY_DIR).replay_deployment("embedding")
    assert found is not None
    return found


def slot(token: str, dimensions: int) -> tuple[int, float]:
    """What the contract says a token's SHA-256 picks: an index modulo the
    dimensions from its first eight bytes, a sign from the lowest bit of the
    ninth."""
    digest = hashlib.sha256(token.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % dimensions, (
        -1.0 if digest[8] & 1 else 1.0
    )


def test_the_same_text_gives_the_same_vector() -> None:
    assert replay_embedding(EMBEDDING_TEXT, 1024) == replay_embedding(
        EMBEDDING_TEXT, 1024
    )


@pytest.mark.parametrize("dimensions", [1, 2, 8, 1024, 2000])
def test_the_length_is_the_dimensions_and_the_norm_is_one(dimensions: int) -> None:
    vector = replay_embedding(EMBEDDING_TEXT, dimensions)

    assert len(vector) == dimensions
    assert math.isclose(math.hypot(*vector), 1.0, abs_tol=1e-12)


def test_a_single_word_is_a_signed_one_at_the_index_its_hash_picks() -> None:
    index, sign = slot("hello", 1024)

    vector = replay_embedding("hello", 1024)

    assert (index, sign) == (782, 1.0)  # pinned: a change here changes every vector
    assert vector[index] == sign
    assert sum(1 for x in vector if x != 0.0) == 1


def test_the_words_add_up_and_the_sum_is_normalised() -> None:
    dimensions = 16
    counts = [0.0] * dimensions
    for word in ("storm", "damage", "storm"):
        index, sign = slot(word, dimensions)
        counts[index] += sign
    length = math.hypot(*counts)

    vector = replay_embedding("storm damage storm", dimensions)

    assert vector == pytest.approx(tuple(c / length for c in counts))
    assert vector[14] == pytest.approx(-2 / math.sqrt(5))  # a repeat counts twice


def test_the_tokens_are_the_casefolded_word_runs() -> None:
    same = [
        "storm damage",
        "STORM, damage!",
        "damage -- Storm",
        "  Storm\tDAMAGE\n",
        "storm damage",
    ]

    vectors = {replay_embedding(text, 1024) for text in same}

    assert len(vectors) == 1


def test_a_run_of_letters_digits_and_underscores_is_one_token() -> None:
    assert replay_embedding("claim_2890", 1024) == replay_embedding("CLAIM_2890", 1024)
    assert replay_embedding("claim_2890", 1024) != replay_embedding("claim 2890", 1024)


def test_texts_that_share_words_are_closer_than_texts_that_share_none() -> None:
    claim = replay_embedding("storm damage to the roof of the house", 1024)
    shares = replay_embedding("the roof was damaged in the storm", 1024)
    nothing = replay_embedding("invoice payment overdue reminder sent", 1024)

    assert cosine(claim, shares) > 0.3
    assert abs(cosine(claim, nothing)) < 0.2
    assert cosine(claim, shares) > cosine(claim, nothing)
    assert cosine(claim, claim) == pytest.approx(1.0)


def test_a_different_text_with_other_words_gives_another_vector() -> None:
    assert replay_embedding("storm", 1024) != replay_embedding("flood", 1024)


@pytest.mark.parametrize("text", ["!!!", "...", "  \t\n", "—", "?!?!"])
def test_a_text_with_no_token_gives_a_finite_unit_vector(text: str) -> None:
    vector = replay_embedding(text, 1024)

    assert len(vector) == 1024
    assert all(math.isfinite(x) for x in vector)
    assert math.isclose(math.hypot(*vector), 1.0, abs_tol=1e-12)
    assert vector == replay_embedding(text, 1024)


def test_texts_with_no_token_get_a_vector_of_their_own() -> None:
    assert replay_embedding("!!!", 1024) != replay_embedding("???", 1024)


def test_the_vector_of_a_tokenless_text_comes_from_the_hash_of_the_whole_text() -> None:
    index, sign = slot("!!!", 1024)

    vector = replay_embedding("!!!", 1024)

    assert vector[index] == sign
    assert sum(1 for x in vector if x != 0.0) == 1


def test_words_that_cancel_give_the_one_hot_vector_of_the_whole_text() -> None:
    # Two words that land on one index with opposite signs add up to nothing:
    # the sum has no direction to normalise. The pair is one whose whole text
    # picks another index than 0 or the words' own, with a minus sign, so a
    # vector that keeps the words' index or a fixed +1 at index 0 cannot pass.
    dimensions = 8
    words = [f"w{n}" for n in range(60)]
    plus, minus = next(
        (p, m)
        for p in words
        for m in words
        if slot(p, dimensions)[0] == slot(m, dimensions)[0]
        and slot(p, dimensions)[1] == 1.0
        and slot(m, dimensions)[1] == -1.0
        and slot(f"{p} {m}", dimensions)[1] == -1.0
        and slot(f"{p} {m}", dimensions)[0] not in (0, slot(p, dimensions)[0])
    )
    text = f"{plus} {minus}"
    index, sign = slot(text, dimensions)

    vector = replay_embedding(text, dimensions)

    assert len(vector) == dimensions
    assert vector[index] == sign
    assert sum(1 for x in vector if x != 0.0) == 1


def test_the_embedding_input_tokens_are_characters_over_four_per_input() -> None:
    request = EmbeddingRequest(inputs=("abcde", "abcd", "x"))

    reply = ReplayProvider().embed(embedding_deployment(), request, timeout_seconds=1.0)

    # 2 + 1 + 1: each input rounds up on its own (pooled it would be 3)
    assert reply.input_tokens == 4
    assert reply.output_tokens == 0


def test_the_replay_provider_embeds_every_input_in_order() -> None:
    deployment = embedding_deployment()
    inputs = ("first text", "second text", "third text")
    provider: EmbeddingProvider = ReplayProvider()

    reply = provider.embed(
        deployment, EmbeddingRequest(inputs=inputs), timeout_seconds=1.0
    )

    assert reply == EmbeddingReply(
        embeddings=tuple(replay_embedding(text, 1024) for text in inputs),
        model="replay-embedding",
        input_tokens=3 + 3 + 3,
    )
    assert deployment.dimensions == 1024


def test_the_replay_provider_embeds_to_the_deployments_dimensions() -> None:
    small = embedding_deployment().model_copy(update={"dimensions": 8})

    reply = ReplayProvider().embed(
        small, EmbeddingRequest(inputs=("some words",)), timeout_seconds=1.0
    )

    assert [len(v) for v in reply.embeddings] == [8]


def test_a_replay_deployment_without_dimensions_cannot_embed() -> None:
    nameless = embedding_deployment().model_copy(update={"dimensions": None})

    with pytest.raises(ValueError, match="dimensions"):
        ReplayProvider().embed(
            nameless, EmbeddingRequest(inputs=("x",)), timeout_seconds=1.0
        )
