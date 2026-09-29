"""Render a product's policy wording as Markdown from the catalogue."""

import textwrap

from . import catalogue, wording_text
from .catalogue import Exclusion, Product

WIDTH = 80
EM_DASH = "—"


def render_wording(product: Product) -> str:
    """The complete wording of ``product``: prose wrapped at 80 columns."""
    blocks = [
        f"# Meridian Insurance {EM_DASH} {product.name}",
        _wrap(
            f"Product code: `{product.code}`. Wording version: "
            f"{catalogue.WORDING_VERSION}."
        ),
        _wrap(wording_text.DISCLAIMER),
        *_definitions(product),
        *_covered(product),
        *_not_covered(product),
        *_deductible_and_limits(product),
        *_making_a_claim(product),
        *_period_of_cover(),
    ]
    return "\n\n".join(blocks) + "\n"


def _wrap(text: str) -> str:
    return textwrap.fill(
        text, width=WIDTH, break_long_words=False, break_on_hyphens=False
    )


def _bullet(text: str) -> str:
    return textwrap.fill(
        text,
        width=WIDTH,
        initial_indent="- ",
        subsequent_indent="  ",
        break_long_words=False,
        break_on_hyphens=False,
    )


def _clause(number: str, title: str, *paragraphs: str) -> list[str]:
    return [f"### {number} {title}", *paragraphs]


def _definitions(product: Product) -> list[str]:
    object_title, object_text = wording_text.INSURED_OBJECT[product.line]
    if product.fixed_limit is not None:
        sum_insured = wording_text.SUM_INSURED["none"]
    elif product.line == "motor":
        sum_insured = wording_text.SUM_INSURED["vehicle"]
    else:
        sum_insured = wording_text.SUM_INSURED["home"]
    return [
        "## 1. Definitions",
        *_clause("1.1", "You and we", _wrap(wording_text.YOU_AND_WE)),
        *_clause("1.2", object_title, _wrap(object_text)),
        *_clause("1.3", "Deductible", _wrap(wording_text.DEFINITION_DEDUCTIBLE)),
        *_clause("1.4", "Sum insured", _wrap(sum_insured)),
        *_clause("1.5", "Period of cover", _wrap(wording_text.DEFINITION_PERIOD)),
    ]


def _covered(product: Product) -> list[str]:
    blocks = [
        "## 2. What is covered",
        _wrap(wording_text.COVER_INTRO.format(name=product.name)),
    ]
    for peril in product.covered:
        text = wording_text.COVER[(product.line, peril)]
        blocks += _clause(
            catalogue.cover_clause(product, peril),
            catalogue.PERIL_TITLES[peril],
            _wrap(text),
        )
    return blocks


def _not_covered(product: Product) -> list[str]:
    blocks = [
        "## 3. What is not covered",
        _wrap(wording_text.EXCLUSION_INTRO.format(name=product.name)),
    ]
    for exclusion in product.exclusions:
        blocks += _clause(
            catalogue.exclusion_clause(product, exclusion.code),
            exclusion.title,
            _wrap(wording_text.EXCLUSION[exclusion.code]),
            _wrap(_applies_sentence(exclusion)),
        )
    return blocks


def _applies_sentence(exclusion: Exclusion) -> str:
    template = (
        wording_text.APPLIES_PERIL
        if exclusion.kind == catalogue.KIND_PERIL
        else wording_text.APPLIES_CIRCUMSTANCE
    )
    return template.format(
        perils=_join(catalogue.peril_label(p) for p in exclusion.perils)
    )


def _join(items) -> str:
    words = list(items)
    if len(words) == 1:
        return words[0]
    return ", ".join(words[:-1]) + " and " + words[-1]


def _deductible_and_limits(product: Product) -> list[str]:
    if product.deductible:
        deductible = wording_text.DEDUCTIBLE_WITH_AMOUNT.format(
            amount=_euros(product.deductible)
        )
    else:
        deductible = wording_text.DEDUCTIBLE_NONE
    if product.fixed_limit is not None:
        limit = wording_text.LIMIT_FIXED.format(amount=_euros(product.fixed_limit))
    else:
        limit = wording_text.LIMIT_SUM_INSURED
    return [
        "## 4. Deductible and limits",
        *_clause(catalogue.DEDUCTIBLE_CLAUSE, "Deductible", _wrap(deductible)),
        *_clause(catalogue.LIMIT_CLAUSE, "Limit", _wrap(limit)),
    ]


def _euros(amount: int) -> str:
    return f"{catalogue.CURRENCY} {amount:,}"


def _making_a_claim(product: Product) -> list[str]:
    reporting = wording_text.REPORTING.format(days=catalogue.REPORTING_WINDOW_DAYS)
    blocks = [
        "## 5. Making a claim",
        *_clause(catalogue.REPORTING_CLAUSE, "Reporting a claim", _wrap(reporting)),
    ]
    for peril in product.covered:
        label = catalogue.peril_label(peril)
        blocks += _clause(
            catalogue.documents_clause(product, peril),
            f"Documents for {label}",
            _wrap(wording_text.DOCUMENTS_INTRO.format(peril=label)),
            "\n".join(_document_bullets(peril)),
            _wrap(wording_text.DOCUMENTS_OUTRO),
        )
    return blocks


def _document_bullets(peril: str) -> list[str]:
    return [
        _bullet(
            f"{catalogue.DOCUMENT_TITLES[code]} (`{code}`): "
            f"{wording_text.DOCUMENT_DESCRIPTION[code]}"
        )
        for code in catalogue.REQUIRED_DOCUMENTS[peril]
    ]


def _period_of_cover() -> list[str]:
    return [
        "## 6. Period of cover",
        *_clause(
            catalogue.PERIOD_CLAUSE, "Period of cover", _wrap(wording_text.PERIOD)
        ),
        *_clause(
            catalogue.LAPSE_CLAUSE, "Lapse for non-payment", _wrap(wording_text.LAPSE)
        ),
    ]
