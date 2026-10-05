"""Guardrails on text before a model reads it: redaction of personal
identifiers, screens for special-category data and for injected instructions,
and the order of the data classes.

Pure and standard-library only: no I/O, no logging of the text it is given."""

from meridian.platform.guardrails.classes import (
    DATA_CLASS_ORDER,
    higher_class,
    parse_data_class,
)
from meridian.platform.guardrails.redaction import (
    CARD_PLACEHOLDER,
    EMAIL_PLACEHOLDER,
    IBAN_PLACEHOLDER,
    PHONE_PLACEHOLDER,
    PLACEHOLDERS,
    Redaction,
    redact,
)
from meridian.platform.guardrails.screening import (
    addresses_the_model,
    holds_special_category,
    screen_fingerprint,
)

__all__ = [
    "CARD_PLACEHOLDER",
    "DATA_CLASS_ORDER",
    "EMAIL_PLACEHOLDER",
    "IBAN_PLACEHOLDER",
    "PHONE_PLACEHOLDER",
    "PLACEHOLDERS",
    "Redaction",
    "addresses_the_model",
    "higher_class",
    "holds_special_category",
    "parse_data_class",
    "redact",
    "screen_fingerprint",
]
