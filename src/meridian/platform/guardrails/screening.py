"""Screens for text that a model should not be sent as it stands."""

import hashlib
import inspect
import json
import re
import unicodedata

_SPACES = re.compile(r"\s+")
_FORMAT_CATEGORY = "Cf"


def _normalise(text: str) -> str:
    """NFKC, without zero-width and other format characters, casefolded, with
    each run of white space inside a line collapsed to one space. Line breaks
    stay, one per break, because a role marker is recognised at a line start.

    A fullwidth letter, a ligature or a zero-width joiner inside a word does
    not hide it. A look-alike letter from another script is not mapped: that
    is a residual, not a case this handles."""
    composed = unicodedata.normalize("NFKC", text)
    visible = "".join(
        char for char in composed if unicodedata.category(char) != _FORMAT_CATEGORY
    )
    lines = visible.casefold().splitlines()
    return "\n".join(_SPACES.sub(" ", line) for line in lines)


def _phrase(*words: str) -> str:
    """The words as a pattern that tolerates any white space, line breaks
    included, between them."""
    return r"\s+".join(words)


_BODY_PART = r"(?:arm|leg|wrist|ankle|ribs?|nose|collarbone|hip)"
_SPECIAL_CATEGORY = re.compile(
    r"\b(?:"
    + "|".join(
        [
            # Health.
            r"hospital(?:s|i[sz]ed|i[sz]ation)?",
            r"injur(?:y|ies|ed|e)",
            r"surger(?:y|ies)",
            r"diagnos(?:is|es|ed|e)",
            r"illness(?:es)?",
            r"diseases?",
            r"medical",
            r"medications?",
            r"doctors?",
            r"pregnan(?:t|cy|cies)",
            r"disabilit(?:y|ies)",
            r"therap(?:y|ies|ist|ists)",
            r"cancers?",
            r"mental(?:\s+|-)health",
            r"depress(?:ion|ed)",
            r"whiplash",
            r"fracture[sd]?",
            r"ambulances?",
            r"wheelchairs?",
            r"diabetes",
            r"asthma",
            r"psychiatrists?",
            r"surgeons?",
            r"chemotherapy",
            r"hiv",
            r"concussions?",
            r"paramedics?",
            r"clinics?",
            r"nurses?",
            r"dentists?",
            r"x-rays?",
            r"dementia",
            r"epilepsy",
            _phrase("heart", "attacks?"),
            _phrase("broken", _BODY_PART),
            _phrase("broke", "my", _BODY_PART),
            # The other Article 9 categories, as unambiguous words only.
            r"religio(?:n|ns|us)",
            _phrase("ethnic", "origin"),
            r"ethnicit(?:y|ies)",
            _phrase("trade", "unions?"),
            _phrase("sexual", "orientation"),
            _phrase("political", "opinions?"),
            r"biometrics?",
            r"genetics?",
            r"(?:muslim|jewish|catholic|christian|hindu|buddhist|sikh)s?",
        ]
    )
    + r")\b"
)


def holds_special_category(text: str) -> bool:
    """Whether ``text`` states special-category personal data (GDPR Article 9):
    health first, then religion, ethnic origin, trade union membership, sexual
    orientation, political opinion, biometric and genetic data.

    The text is normalised, then matched against a short, conservative list of
    whole words and phrases. The list is English only: a claim written in
    Hungarian or German is not screened by it. It is incomplete on purpose: a
    miss is T-13's residual risk, and a word that merely resembles one
    ("will", "ill", "hospitality") never matches; a word with an ordinary
    sense ("stroke", "stitches", "bruises", "anxiety") is left out. A false
    positive costs little: the claim goes to a person (T-73). It does not log
    or keep the text."""
    return _SPECIAL_CATEGORY.search(_normalise(text)) is not None


# Straight quotes, the backtick, and the curly quotes NFKC leaves alone.
_CURLY_QUOTES = "".join(chr(code) for code in (0x201C, 0x201D, 0x2018, 0x2019))
_QUOTES = "\"'`" + _CURLY_QUOTES
_ANSWER_FIELDS = r"(?:verdict|rationale)"
_ADDRESSES_THE_MODEL = tuple(
    re.compile(pattern, re.MULTILINE)
    for pattern in (
        # Orders to drop what the model was told.
        r"\b(?:ignore|disregard|forget|override)\s+"
        r"(?:(?:all|any|the)\s+(?:of\s+the\s+)?)?"
        r"(?:(?:previous|prior|above|earlier|preceding|your)\s+)?"
        r"(?:instructions?|rules|prompts?|messages|guidelines|directions)\b",
        rf"\b{_phrase('disregard', 'everything', '(?:above|before)')}\b",
        rf"\b{_phrase('forget', 'everything')}\b",
        # Phrases that give the model a new role. "act as" counts only before
        # a word that starts a role or a pretence: "I could not act as quickly"
        # is a claimant's sentence.
        rf"\b{_phrase('you', 'are', 'now')}\b",
        rf"\b{_phrase('act', 'as')}\s+(?:a|an|the|my|your|if|though)\b",
        rf"\b{_phrase('pretend', 'to', 'be')}\b",
        rf"\b{_phrase('from', 'now', 'on')},?\s+you\b",
        rf"\b{_phrase('new', 'instructions?')}\b",
        rf"\b{_phrase('system', 'prompt')}\b",
        rf"\b{_phrase('developer', 'message')}\b",
        rf"\b{_phrase('as', 'an', 'ai')}\b",
        # A role marker at the start of a line, and a role tag anywhere.
        r"^ ?(?:system|assistant|user|developer) ?:",
        r"<\s?/?\s?system\s?>",
        r"<\|im_(?:start|end)\|>",
        r"\[/?inst\]",
        # The answer format of the triage call.
        rf"[{_QUOTES}:]\s?{_ANSWER_FIELDS}\b",
        rf"\b{_ANSWER_FIELDS}\s?[{_QUOTES}:]",
        rf"\b{_phrase('approve', '(?:this|the)', 'claim')}\b",
        rf"\b{_phrase('mark', 'this', 'claim')}\b",
        rf"\b{_phrase('set', 'the', 'verdict')}\b",
        r"\b(?:respond|answer|reply)\s+with\b",
        rf"\b{_phrase('output', 'only')}\b",
    )
)


def addresses_the_model(text: str) -> bool:
    """Whether ``text`` tries to instruct a model: an order to drop its
    instructions, a new role, a role marker or tag, or a demand about the
    answer of the triage call ("approve this claim", a ``verdict`` field).

    The text is normalised as for special-category data, then matched
    case-insensitively against a list of patterns that tolerate any white
    space, line breaks included. It is a heuristic and never complete: S032
    measures it. The model call still delimits the claimant's text as data
    (T-26), so a miss here is not the only defence. It does not log or keep
    the text."""
    normalised = _normalise(text)
    return any(pattern.search(normalised) for pattern in _ADDRESSES_THE_MODEL)


def screen_fingerprint() -> str:
    """The SHA-256, in hex, of everything that decides what the two screens
    match: the normalisation, the special-category pattern and each pattern
    that addresses the model, with its flags.

    An evaluation report carries it, so that a changed screen asks for a new
    baseline instead of passing because no grade happened to regress.

    ``_normalise`` is code that uses two pieces of data, so its source text
    (``inspect.getsource``, docstring and comments included: any edit to the
    function changes the digest) is hashed with that data. The text is the
    same in a source tree and in an installed package, which ships its
    ``.py`` files; a package without them cannot be fingerprinted. The
    Unicode database of the interpreter, which NFKC and the category check
    read, is not part of it: a baseline is made under the pinned Python."""
    parts = [
        inspect.getsource(_normalise),
        _SPACES.pattern,
        _SPACES.flags,
        _FORMAT_CATEGORY,
        _SPECIAL_CATEGORY.pattern,
        _SPECIAL_CATEGORY.flags,
        # In the order they are tried: the order is part of the screen's source.
        *[(pattern.pattern, pattern.flags) for pattern in _ADDRESSES_THE_MODEL],
    ]
    # JSON, so that two parts cannot run together into the same bytes.
    encoded = json.dumps(parts, ensure_ascii=True).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()
