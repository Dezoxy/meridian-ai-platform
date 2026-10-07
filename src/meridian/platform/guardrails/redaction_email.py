import re

from meridian.platform.guardrails.redaction_common import (
    EMAIL_PLACEHOLDER,
    JSON_ESCAPE_BEFORE,
    JSON_ESCAPE_LETTERS,
)

# Every pattern below is linear: no quantifier sits inside a repeated group
# that can match the same text two ways, and every repeat is bounded or
# anchored by a character the repeated part cannot match. A pattern only finds
# a candidate; the checksum or the structure test decides.
#
# The local part of an e-mail address is letters, digits and ``._%+-`` of any
# script (and a combining mark of U+0300 to U+036F, so that a decomposed "e
# acute" is not cut). It starts where such a run starts, or right after a JSON
# escape, whose letter is not part of it. It has no upper bound: the pattern
# starts only where a local part starts, so one of any length costs one scan,
# and it is redacted whole, never in part.
EMAIL_LOCAL_CHAR = r"[\w.%+\-̀-ͯ]"
# The domain is dot-separated labels of letters and digits of any script (not
# "_"), a combining mark and hyphens, then a top-level domain of two or more
# letters of any script, or a punycode one ("xn--" and letters or digits). A
# label cannot hold ".", so each label ends at one dot and the match has one
# way to go; the punycode form comes first, so that "xn" alone is not taken as
# a two-letter domain. The ASCII-only form is a subset, so what matched still
# matches.
EMAIL_LABEL_CHAR = r"(?:[^\W_]|[̀-ͯ-])"
EMAIL_TLD = r"(?:xn--[A-Za-z0-9]+|[^\W\d_]{2,})"
EMAIL = re.compile(
    rf"(?:(?<!{EMAIL_LOCAL_CHAR})(?!(?<=\\)[{JSON_ESCAPE_LETTERS}])"
    rf"|(?<={JSON_ESCAPE_BEFORE}))"
    rf"{EMAIL_LOCAL_CHAR}+"
    rf"@(?:{EMAIL_LABEL_CHAR}+\.)+{EMAIL_TLD}"
)


def _replace_email(text: str, found: dict[str, int]) -> str:
    replaced, count = EMAIL.subn(EMAIL_PLACEHOLDER, text)
    if count:
        found["email"] = count
    return replaced
