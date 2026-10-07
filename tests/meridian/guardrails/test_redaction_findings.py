"""The reviews' findings in the redaction of Hungarian identifiers (S067, F1r).

A date followed by a number is not a phone number; a number on the line after
its word is found; the international phone forms the research note cites and a
Hungarian ending after a number; the personal identification number's change of
weights on 1 January 1997 and the word rule's left boundary; the check
functions refuse what is not ASCII digits; and the placeholders of the output
agree with ``found``.

Every number here is made up and passes its public check by construction."""

import json
import random

import pytest
from cputime import MAX_GROWTH, growth

from meridian.platform.guardrails import PLACEHOLDERS, Redaction, hungarian, redact
from meridian.platform.guardrails.redaction import WORD_GAP_MAX_CHARS

# --- Item 1: a date followed by a number is not a phone number ---------------


@pytest.mark.parametrize(
    "text",
    [
        "Kár: 2026/06/30 1250000 Ft",
        "invoice 2026.06.20 8800000",
        "Kár: 2026/06/30 1 250 000 Ft",
        "2026.06.30 1250000",
    ],
)
def test_a_date_followed_by_an_amount_is_not_a_phone_number(text: str) -> None:
    assert redact(text) == Redaction(text=text, found={})


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("tel.06301234567", "tel.[phone]"),
        ("a/06301234567", "a/[phone]"),
        ("/06301234567", "/[phone]"),
        ("/ 06301234567", "/ [phone]"),
        ("2026.07.13. 06301234567", "2026.07.13. [phone]"),
        ("2026-07-13 06301234567", "2026-07-13 [phone]"),
    ],
)
def test_a_number_after_a_slash_or_a_dot_that_follows_no_digit_is_still_found(
    text: str, expected: str
) -> None:
    assert redact(text).text == expected


def test_a_number_with_a_space_before_it_is_a_phone_number_even_after_a_date() -> None:
    # A pin, not a fix: "06 30 1250000" is a mobile number written in groups,
    # and a rule that left it alone would lose real numbers. It passed before
    # the guard and passes after it.
    assert redact("Total 06 30 1250000 HUF").text == "Total [phone] HUF"


# --- Item 2: a number on the line after its word ------------------------------

BREAKS = {
    "lf": "\n",
    "crlf": "\r\n",
    "cr": "\r",
    "json-lf": "\\n",
    "json-crlf": "\\r\\n",
    "json-cr": "\\r",
}
NUMBERS_AFTER_A_WORD = [
    ("TAJ szám:", "123456788", "[national-id]"),
    ("adóazonosító jel:", "8296301237", "[national-id]"),
    ("adószám:", "12345676242", "[tax-number]"),
]


@pytest.mark.parametrize("name", BREAKS)
@pytest.mark.parametrize(("word", "number", "placeholder"), NUMBERS_AFTER_A_WORD)
def test_a_number_on_the_line_after_its_word_is_replaced(
    name: str, word: str, number: str, placeholder: str
) -> None:
    brk = BREAKS[name]

    result = redact(f"{word}{brk}{number} next")

    assert result.text == f"{word}{brk}{placeholder} next"
    assert sum(result.found.values()) == 1


@pytest.mark.parametrize("name", BREAKS)
def test_a_number_after_a_break_and_some_indentation_is_replaced(name: str) -> None:
    brk = BREAKS[name]

    assert redact(f"TAJ:{brk}  123 456 788").text == f"TAJ:{brk}  [national-id]"
    assert redact(f"TAJ:{brk}no. 123456788").text == f"TAJ:{brk}no. [national-id]"


def test_a_word_then_some_text_then_a_break_then_the_number_is_replaced() -> None:
    # Two cases of test_redaction_hungarian.py's left-alone list that pinned the
    # opposite, moved here on purpose (F1r, security M2).
    assert redact("TAJ: abc\n123456788").text == "TAJ: abc\n[national-id]"
    assert redact("TAJ:\r\n123456788").text == "TAJ:\r\n[national-id]"


def test_a_number_on_the_next_line_of_json_text_keeps_the_json_valid() -> None:
    text = json.dumps(
        {"description": "Név: Anna\nTAJ szám:\n123456788\nKár: 2026/06/30"},
        ensure_ascii=False,
    )

    result = redact(text)

    assert json.loads(result.text) == {
        "description": "Név: Anna\nTAJ szám:\n[national-id]\nKár: 2026/06/30"
    }
    assert dict(result.found) == {"national_id": 1}


@pytest.mark.parametrize("brk", ["\n", "\r\n", "\\n"])
def test_the_gap_before_and_after_the_break_each_stand_the_bound_and_no_further(
    brk: str,
) -> None:
    edge = WORD_GAP_MAX_CHARS
    before_near = "TAJ" + ":" + " " * (edge - 1) + brk + "123456788"
    before_far = "TAJ" + ":" + " " * edge + brk + "123456788"
    after_near = "TAJ:" + brk + " " * edge + "123456788"
    after_far = "TAJ:" + brk + " " * (edge + 1) + "123456788"

    assert redact(before_near).text == before_near[:-9] + "[national-id]"
    assert redact(before_far) == Redaction(text=before_far, found={})
    assert redact(after_near).text == after_near[:-9] + "[national-id]"
    assert redact(after_far) == Redaction(text=after_far, found={})


@pytest.mark.parametrize(
    "text",
    [
        "TAJ:\n\n123456788",
        "TAJ:\r\n\r\n123456788",
        "TAJ:\\n\\n123456788",
        "TAJ:\n \n123456788",
        "TAJ:\n\\n123456788",
        "TAJ: x\ny\n123456788",
        "TAJ: 5\n123456788",
        "TAJ:\n5 123456788",
        "TAJ:\\t123456788",
    ],
)
def test_two_breaks_a_digit_or_another_escape_between_the_word_and_its_number_leave_it(
    text: str,
) -> None:
    assert redact(text) == Redaction(text=text, found={})


def test_a_line_break_after_a_word_with_no_number_next_leaves_the_text_alone() -> None:
    text = "TAJ szám:\nNévjegy: Kiss Anna\n123456788"

    assert redact(text) == Redaction(text=text, found={})


# --- Item 3: international forms and a Hungarian ending -----------------------


@pytest.mark.parametrize(
    "value",
    [
        "+36/70 701-8674",
        "+36/70/701/8674",
        "(+3670) 701-8674",
        "(+36) 70 701 8674",
        "(+36)/70 701-8674",
        "+36.70.701.8674",
        "+36 70.701.8674",
    ],
)
def test_the_international_forms_of_the_research_note_are_replaced_whole(
    value: str,
) -> None:
    result = redact(f"tel {value} end")

    assert result.text == "tel [phone] end"
    assert dict(result.found) == {"phone": 1}


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("call +36 70 701 8674.", "call [phone]."),
        ("call +36.70.701.8674.", "call [phone]."),
        ("call +36 70 701 8674/", "call [phone]/"),
        ("call (+36 70 701 8674).", "call ([phone])."),
        ("call (+36 70 701 8674) now", "call ([phone]) now"),
        ("+36 70 701 8674, +36/30 123 4567", "[phone], [phone]"),
    ],
)
def test_a_mark_after_an_international_number_stays_outside_it(
    text: str, expected: str
) -> None:
    assert redact(text).text == expected


@pytest.mark.parametrize(
    "text",
    [
        "a(+3670) 701-8674",
        "1(+3670) 701-8674",
        "x+36/70 701-8674",
        "+36/70 701",
        "(+36)70",
        "+36/",
    ],
)
def test_an_international_form_that_is_joined_or_too_short_is_left_alone(
    text: str,
) -> None:
    assert redact(text) == Redaction(text=text, found={})


@pytest.mark.parametrize(
    ("number", "ending"),
    [
        ("06301234567", "es"),
        ("06 30 123 4567", "es"),
        ("+36 30 123 4567", "es"),
        ("+36301234567", "val"),
        ("06 1 234 5678", "as"),
        ("0036 30 123 4567", "nak"),
        ("(06 30) 123 4567", "re"),
        ("06301234567", "től"),
        # The ending follows the spoken name of the number's last digit.
        ("06301234567", "tel"),
        ("06301234566", "tal"),
        ("06301234563", "mal"),
        ("06301234561", "gyel"),
        ("06301234564", "gyel"),
        ("06301234568", "cal"),
        ("06301234569", "cel"),
        ("06301234565", "ös"),
        ("06301234566", "os"),
        ("+36 30 123 4565", "tel"),
    ],
)
def test_a_number_before_a_hungarian_ending_is_replaced_and_the_ending_stays(
    number: str, ending: str
) -> None:
    result = redact(f"hívja a {number}-{ending} számot")

    assert result.text == f"hívja a [phone]-{ending} számot"
    assert dict(result.found) == {"phone": 1}


@pytest.mark.parametrize("mark", [".", ",", ")", ""])
def test_the_ending_may_end_the_text_or_be_followed_by_a_mark(mark: str) -> None:
    assert redact(f"06301234567-es{mark}").text == f"[phone]-es{mark}"


@pytest.mark.parametrize(
    "text",
    [
        "06301234567-ab",
        "06301234567-abcde",
        "06301234567-ES",
        "06301234567-Es",
        "06301234567-es1",
        "06301234567-esx",
        "06301234567-es_",
        "06301234567-es-2026",
        "06301234567-es-ab",
        "06301234567-e",
        "06301234567-9",
        "+36 30 123 4567-ab",
        "+36 30 123 4567-es-2026",
        "+36 30 123 4567-ES",
        "+36 30 123 4567-esx",
    ],
)
def test_a_number_that_starts_a_longer_hyphenated_identifier_stays_whole(
    text: str,
) -> None:
    assert redact(text) == Redaction(text=text, found={})


def test_a_trailing_hyphen_and_a_hyphenated_digit_follow_the_old_rule() -> None:
    assert redact("06301234567-").text == "[phone]-"
    assert redact("+44 20 7946 0958-5").text == "[phone]"


def test_the_ending_does_not_change_what_other_identifiers_take_after_them() -> None:
    assert redact("GB82WEST12345698765432-es").text == "GB82WEST12345698765432-es"
    assert redact("4111111111111111-es").text == "4111111111111111-es"
    assert redact("99900016-00012348-es").text == "99900016-00012348-es"


# --- Item 4: two rules nobody tested ------------------------------------------

OLD_WEIGHTS = tuple(range(1, 11))
NEW_WEIGHTS = tuple(range(10, 0, -1))


def _weighted_remainder(first_ten: str, weights: tuple[int, ...]) -> int:
    return sum(int(d) * w for d, w in zip(first_ten, weights, strict=True)) % 11


def test_a_person_born_on_the_last_day_of_1996_has_the_old_weights() -> None:
    # 1 961231 001: old 1*1 + 9*2 + 6*3 + 1*4 + 2*5 + 3*6 + 1*7 + 0*8 + 0*9 +
    # 1*10 = 86, and 86 mod 11 is 9. New 10 + 81 + 48 + 7 + 12 + 15 + 4 + 0 + 0
    # + 1 = 178, and 178 mod 11 is 2.
    first_ten = "1961231001"
    assert _weighted_remainder(first_ten, OLD_WEIGHTS) == 9
    assert _weighted_remainder(first_ten, NEW_WEIGHTS) == 2

    assert redact("19612310019").text == "[national-id]"
    assert redact("19612310012").text == "19612310012"


def test_a_person_born_on_the_first_day_of_1997_has_the_new_weights() -> None:
    # 1 970101 000: old 1 + 18 + 21 + 0 + 5 + 0 + 7 + 0 + 0 + 0 = 52, and 52 mod
    # 11 is 8. New 10 + 81 + 56 + 0 + 6 + 0 + 4 + 0 + 0 + 0 = 157, and 157 mod
    # 11 is 3.
    first_ten = "1970101000"
    assert _weighted_remainder(first_ten, OLD_WEIGHTS) == 8
    assert _weighted_remainder(first_ten, NEW_WEIGHTS) == 3

    assert redact("19701010003").text == "[national-id]"
    assert redact("19701010008").text == "19701010008"


@pytest.mark.parametrize(
    "text",
    [
        "xtaj 123456788",
        "Ataj: 123 456 788",
        "1taj: 123456788",
        "étaj 123456788",
        "xtajszám 123456788",
        "xadószám: 12345676242",
        "1adóazonosító jel: 8296301237",
        "xtax number: 12345676242",
        "xsocial security 123456788",
    ],
)
def test_a_word_inside_a_longer_word_does_not_make_the_number_after_it_findable(
    text: str,
) -> None:
    assert redact(text) == Redaction(text=text, found={})


# --- Item 6: the check functions take ASCII digits and nothing else ----------


def _fullwidth(length: int) -> str:
    """Digits that ``str.isdigit`` and ``int`` accept, and ASCII is not."""
    return "".join(chr(0xFF10 + index % 10) for index in range(length))


NOT_ASCII_DIGITS = {
    "national_phone_holds": ["0630" + "²" * 7, "06301_34567", "0630" + _fullwidth(7)],
    "social_security_holds": ["12345678²", "1234_6788", _fullwidth(9)],
    "tax_id_holds": ["8" + "²" * 9, "8_96301237", "888888888é"],
    "tax_number_holds": ["²" * 11, "1234567_242", _fullwidth(11)],
    "account_holds": ["9" * 15 + "²", "9990001_00012348", "9" * 23 + "²"],
    "personal_id_holds": ["²" * 11, "1_000000000", _fullwidth(11)],
}


@pytest.mark.parametrize(
    ("name", "digits"),
    [(name, digits) for name, items in NOT_ASCII_DIGITS.items() for digits in items],
)
def test_a_check_function_returns_false_for_what_is_not_ascii_digits(
    name: str, digits: str
) -> None:
    assert getattr(hungarian, name)(digits) is False


@pytest.mark.parametrize("name", list(NOT_ASCII_DIGITS))
def test_a_check_function_returns_false_for_an_empty_string(name: str) -> None:
    assert getattr(hungarian, name)("") is False


# --- Item 7: the placeholders agree with the count ----------------------------

PIECES = [
    "call me on 06 30 123 4567",
    "or +36/70 701-8674",
    "(+3670) 701-8674",
    "06301234567-es",
    "tax 12345676-2-42",
    "account 99900016-00012348",
    "account 99900016-12345678-00000125",
    "ID 1-800101-1238",
    "personal 30503157895",
    "TAJ: 123 456 788",
    "TAJ szám:\n123456788",
    "adóazonosító jel: 8296301237",
    "adószám: 12345676242",
    "card 4111 1111 1111 1111",
    "mail anna@example.com",
    "iban GB82 WEST 1234 5698 7654 32",
    "Kár: 2026/06/30 1250000 Ft",
    "Total 06 30 1250000 HUF",
    "ref 2026-07-13 and 1250000",
    "the pipe burst on Monday",
    "tajvan 123456788",
    "06 22 123 45",
    "Anna and Béla",
]
SEPARATORS = [" ", ", ", "\n", " / ", ". ", " - "]


def test_the_placeholders_in_the_output_equal_the_count_found_of_each_kind() -> None:
    generator = random.Random(20261006)  # noqa: S311 - a fixed seed, not a secret
    seen: set[str] = set()

    for _ in range(400):
        pieces = generator.choices(PIECES, k=generator.randint(1, 6))
        text = ""
        for piece in pieces:
            text += generator.choice(SEPARATORS) + piece
        result = redact(text)

        for kind, placeholder in PLACEHOLDERS.items():
            assert result.text.count(placeholder) == result.found.get(kind, 0), text
        seen.update(result.found)

    assert seen == set(PLACEHOLDERS)


# --- Linear time (T-73) for the new patterns ----------------------------------

SMALL_LENGTH = 10_000
LARGE_LENGTH = 40_000
SHAPES = {
    "word-break": lambda n: "TAJ:\n" * (n // 5),
    "word-json-break": lambda n: "TAJ:\\n" * (n // 6),
    "word-json-crlf": lambda n: "TAJ:\\r\\n" * (n // 8),
    "word-break-gap": lambda n: ("TAJ" + " " * 12 + "\n" + " " * 12) * (n // 28),
    "word-breaks-only": lambda n: "TAJ" + "\n" * n,
    "word-json-breaks-only": lambda n: "TAJ" + "\\n" * (n // 2),
    "word-break-digits": lambda n: "TAJ:\n" + "1" * n,
    "phone-international-slash": lambda n: "+36/" * (n // 4),
    "phone-international-dots": lambda n: "+36." * (n // 4),
    "phone-international-paren": lambda n: "(+36" * (n // 4),
    "phone-international-paren-pairs": lambda n: "(+36) " * (n // 6),
    "phone-ending": lambda n: "06301234567-es " * (n // 15),
    "phone-ending-hyphens": lambda n: "06301234567-es" * (n // 14),
    "phone-ending-letters": lambda n: "06301234567-" + "e" * n,
    "phone-international-ending": lambda n: "+36 30 123 4567-es " * (n // 19),
    "phone-date-slash": lambda n: "2026/06 " * (n // 8),
    "phone-date-dots": lambda n: "2026.06." * (n // 8),
}


@pytest.mark.parametrize("name", list(SHAPES))
def test_an_adversarial_text_for_the_new_patterns_is_redacted_in_linear_time(
    name: str,
) -> None:
    small, large = SHAPES[name](SMALL_LENGTH), SHAPES[name](LARGE_LENGTH)
    assert len(small) >= SMALL_LENGTH - 20
    assert len(large) >= LARGE_LENGTH - 20

    assert growth(redact, small, large) < MAX_GROWTH
