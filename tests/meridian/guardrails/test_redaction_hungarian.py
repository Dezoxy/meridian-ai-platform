"""Redaction of Hungarian identifiers (S067): national phone numbers, the tax
number, the domestic account number, the personal identification number, and
the social security and tax identification numbers after their word.

Every number here is made up and passes its public check by construction (the
examples of the sourced note, or built by the helpers below from the published
rule); none is a lookup of a person."""

import json
import time
from collections.abc import Callable

import pytest

from meridian.platform.guardrails import (
    ACCOUNT_PLACEHOLDER,
    IBAN_PLACEHOLDER,
    NATIONAL_ID_PLACEHOLDER,
    PHONE_PLACEHOLDER,
    PLACEHOLDERS,
    TAX_NUMBER_PLACEHOLDER,
    Redaction,
    redact,
)
from meridian.platform.guardrails.redaction import WORD_GAP_MAX_CHARS

NBSP = chr(0x00A0)
THIN_SPACE = chr(0x2009)

# The numbering plan's table (NMHH, official notice of 10 March 2026): the
# digits of a domestic number, area or service code included, by code. Code 1
# (Budapest) stands alone; 55 is a test number and 71 is machine-to-machine.
EIGHT_DIGIT_CODES = [
    "22",
    "23",
    "24",
    "25",
    "26",
    "27",
    "28",
    "29",
    "32",
    "33",
    "34",
    "35",
    "36",
    "37",
    "42",
    "44",
    "45",
    "46",
    "47",
    "48",
    "49",
    "52",
    "53",
    "54",
    "56",
    "57",
    "59",
    "62",
    "63",
    "66",
    "68",
    "69",
    "72",
    "73",
    "74",
    "75",
    "76",
    "77",
    "78",
    "79",
    "80",
    "82",
    "83",
    "84",
    "85",
    "87",
    "88",
    "89",
    "90",
    "91",
    "92",
    "93",
    "94",
    "95",
    "96",
    "99",
]
NINE_DIGIT_CODES = ["20", "21", "30", "31", "38", "50", "70"]
UNUSED_CODES = [
    "39",
    "40",
    "41",
    "43",
    "51",
    "55",
    "58",
    "60",
    "61",
    "64",
    "65",
    "67",
    "81",
    "86",
    "97",
    "98",
] + [f"{number:02d}" for number in range(10)]

PHONES = [
    "06 30 123 4567",
    "06301234567",
    "06-30/123-4567",
    "06.30.123.4567",
    "(06 30) 123 4567",
    "06 (30) 123 4567",
    "(06)30 1234567",
    "0630 1234567",
    "06 30 123-4567",
    "06/30/123-45-67",
    "06 20 123 4567",
    "06 70 123 4567",
    "06 21 123 4567",
    "06 38 123 4567",
    "06 1 234 5678",
    "(06 1) 234 5678",
    "06-1-234-5678",
    "06 22 123 456",
    "06 80 123 456",
    "06 90 123 456",
    "06 71 123 456 7890",
    "0036 30 123 4567",
    "0036301234567",
    "00 36 30 123 4567",
    "(0036) 30 123 4567",
    "0036 1 234 5678",
    f"06{NBSP}30{NBSP}123{NBSP}4567",
    f"06{THIN_SPACE}30{THIN_SPACE}123{THIN_SPACE}4567",
]
TAX_NUMBERS = [
    "12345676-2-42",
    "20000019-1-02",
    "45678909-4-13",
    "30000010-2-44",
]
ACCOUNTS = [
    "99900016-00012348",
    "99900016-55501020",
    "99900016-70000131",
    "99900016 00012348",
    "99900016-00012348".replace("-", NBSP),
    "99900016-12345678-00000125",
    "99900016-00000000-00003450",
    "99900016-99900011-12223337",
    "99900016 12345678 00000125",
    "99900016-12345678 00000125",
]
NATIONAL_IDS = [
    "1-800101-1238",
    "2-851231-4560",
    "1-780807-3214",
    "3-050315-7895",
    "4-100228-0128",
    "2-990412-5559",
    "18001011238",
    "30503157895",
]
OWN_VALUES = [
    ("phone", PHONES[0]),
    ("phone", PHONES[1]),
    ("phone", PHONES[21]),
    ("tax_number", TAX_NUMBERS[0]),
    ("account", ACCOUNTS[0]),
    ("account", ACCOUNTS[5]),
    ("national_id", NATIONAL_IDS[0]),
    ("national_id", NATIONAL_IDS[6]),
]


def _expected(kind: str) -> str:
    return PLACEHOLDERS[kind]


def test_the_new_placeholders_are_fixed_and_safe_inside_a_json_string() -> None:
    assert TAX_NUMBER_PLACEHOLDER == "[tax-number]"
    assert ACCOUNT_PLACEHOLDER == "[account]"
    assert NATIONAL_ID_PLACEHOLDER == "[national-id]"
    assert PLACEHOLDERS["tax_number"] == TAX_NUMBER_PLACEHOLDER
    assert PLACEHOLDERS["account"] == ACCOUNT_PLACEHOLDER
    assert PLACEHOLDERS["national_id"] == NATIONAL_ID_PLACEHOLDER
    for placeholder in (
        TAX_NUMBER_PLACEHOLDER,
        ACCOUNT_PLACEHOLDER,
        NATIONAL_ID_PLACEHOLDER,
    ):
        assert json.dumps(placeholder) == f'"{placeholder}"'
        assert not any(ch in placeholder for ch in "\"\\'")
        assert not any(ch.isdigit() for ch in placeholder)


@pytest.mark.parametrize("value", PHONES)
def test_a_national_phone_number_is_replaced_whole_inside_a_sentence(
    value: str,
) -> None:
    result = redact(f"Please call back on {value} before Friday.")

    assert result.text == f"Please call back on {PHONE_PLACEHOLDER} before Friday."
    assert dict(result.found) == {"phone": 1}


@pytest.mark.parametrize("value", PHONES)
def test_a_national_phone_number_is_replaced_at_the_start_and_at_the_end(
    value: str,
) -> None:
    assert redact(f"{value} was mine").text == f"{PHONE_PLACEHOLDER} was mine"
    assert redact(f"mine was {value}").text == f"mine was {PHONE_PLACEHOLDER}"
    assert redact(value).text == PHONE_PLACEHOLDER
    assert redact(f"({value}).").text == f"({PHONE_PLACEHOLDER})."
    assert (
        redact(f"{value}, {value}").text == f"{PHONE_PLACEHOLDER}, {PHONE_PLACEHOLDER}"
    )


def test_the_two_forms_the_old_rule_left_alone_are_phone_numbers_now() -> None:
    for text in ("06 30 123 4567", "06301234567"):
        assert redact(text) == Redaction(text="[phone]", found={"phone": 1})


@pytest.mark.parametrize(
    "text",
    [
        "+36 30 123 4567",
        "+36301234567",
        "+36 (30) 123-4567",
        "+36-30-123-4567",
        "+36 1 234 5678",
    ],
)
def test_an_international_number_is_still_found_once_and_whole(text: str) -> None:
    result = redact(f"call {text} now")

    assert result.text == "call [phone] now"
    assert dict(result.found) == {"phone": 1}


def test_an_international_and_a_national_number_are_both_counted_as_phones() -> None:
    result = redact(
        "Mobile +36 30 123 4567, office 06 1 234 5678, old 0036 20 123 4567"
    )

    assert result.text == "Mobile [phone], office [phone], old [phone]"
    assert dict(result.found) == {"phone": 3}


@pytest.mark.parametrize("code", EIGHT_DIGIT_CODES)
def test_an_area_or_service_code_of_eight_digits_takes_exactly_eight(
    code: str,
) -> None:
    number = f"{code}123456"

    assert redact(f"06{number}") == Redaction(text="[phone]", found={"phone": 1})
    assert redact(f"06{number}7").text == f"06{number}7"
    assert redact(f"06{number[:-1]}").text == f"06{number[:-1]}"


@pytest.mark.parametrize("code", NINE_DIGIT_CODES)
def test_a_mobile_or_service_code_of_nine_digits_takes_exactly_nine(
    code: str,
) -> None:
    number = f"{code}1234567"

    assert redact(f"06{number}") == Redaction(text="[phone]", found={"phone": 1})
    assert redact(f"06{number}8").text == f"06{number}8"
    assert redact(f"06{number[:-1]}").text == f"06{number[:-1]}"


def test_the_machine_to_machine_code_takes_twelve_digits_and_budapest_eight() -> None:
    assert redact("06711234567890") == Redaction(text="[phone]", found={"phone": 1})
    assert redact("0671123456789").text == "0671123456789"
    assert redact("067112345678901").text == "067112345678901"
    assert redact("0612345678") == Redaction(text="[phone]", found={"phone": 1})
    assert redact("061234567").text == "061234567"
    assert redact("06123456789").text == "06123456789"


@pytest.mark.parametrize("code", UNUSED_CODES)
def test_a_code_the_plan_does_not_hold_is_not_a_phone_number(code: str) -> None:
    for length in (8, 9, 12):
        digits = (code + "1234567890123")[:length]
        text = f"06{digits}"

        assert redact(text) == Redaction(text=text, found={}), (code, length)


@pytest.mark.parametrize(
    "text",
    [
        "2026.06.30.",
        "2026. 06. 30.",
        "2026.06.30. 12",
        "06.30.2026",
        "06/30/2026 12 34",
        "2026-06-30 12:00",
        "2026-06-30 06 30 12",
        "13 June 2026 06 30",
        "EUR 06 300",
        "006301234567",
        "1006301234567",
        "06 30 123 456",
        "06 30 123 45678",
        "0 6 30 123 4567",
        "06+30 123 4567",
    ],
)
def test_a_date_or_a_number_that_is_not_a_phone_number_is_left_alone(
    text: str,
) -> None:
    assert redact(text) == Redaction(text=text, found={})


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("tel:06301234567", "tel:[phone]"),
        ("tel 06 30 123 4567.", "tel [phone]."),
        ("(06301234567)", "([phone])"),
        ("06 30 123 4567, then 06 20 123 4567", "[phone], then [phone]"),
        ("06 30 123 4567 12 345", "[phone] 12 345"),
        ("06 30 123 4567 8901", "[phone] 8901"),
        ("(06 30 123 4567)", "([phone])"),
        ("06301234567 12", "[phone] 12"),
        ("2026-07-13 06301234567", "2026-07-13 [phone]"),
        ("2026-06-30 06 30 123 4567", "2026-06-30 [phone]"),
    ],
)
def test_a_national_number_set_off_by_a_mark_or_a_date_is_still_replaced(
    text: str, expected: str
) -> None:
    assert redact(text).text == expected


@pytest.mark.parametrize("value", TAX_NUMBERS)
def test_a_tax_number_in_its_hyphenated_form_is_replaced(value: str) -> None:
    result = redact(f"Tax number {value} on the invoice.")

    assert result.text == f"Tax number {TAX_NUMBER_PLACEHOLDER} on the invoice."
    assert dict(result.found) == {"tax_number": 1}


@pytest.mark.parametrize(
    "text",
    [
        "12345677-2-42",
        "20000010-1-02",
        "12345676 2 42",
        "12345676-2-423",
        "12345676-22-42",
        "1234567-2-42",
        "HU12345676",
        "20260105-2-42",
        "2026-07-13",
    ],
)
def test_a_tax_number_with_a_wrong_check_digit_or_form_is_left_alone(
    text: str,
) -> None:
    assert redact(text) == Redaction(text=text, found={})


def test_the_check_digit_of_a_tax_number_is_zero_when_the_sum_ends_in_zero() -> None:
    assert redact("30000010-2-42").text == "[tax-number]"
    assert redact("30000011-2-42").text == "30000011-2-42"


@pytest.mark.parametrize("value", ACCOUNTS)
def test_a_domestic_account_number_is_replaced_whole(value: str) -> None:
    result = redact(f"Pay to {value} please.")

    assert result.text == f"Pay to {ACCOUNT_PLACEHOLDER} please."
    assert dict(result.found) == {"account": 1}


@pytest.mark.parametrize(
    "text",
    [
        "99900015-00012348",
        "99900016-12345678-00000126",
        "99900016-12345679-00000125",
        "99900017-12345678-00000125",
        "99900016-12345678",
        "99900016-12345678-0000012",
        "99900016-12345678-000001255",
        "9990001600012348",
        "999000160001234800000000",
        "99900016--00012348",
        "99900016-0001234",
        "20260105 20260106",
    ],
)
def test_an_account_number_with_a_wrong_check_digit_or_form_is_left_alone(
    text: str,
) -> None:
    assert redact(text) == Redaction(text=text, found={})


def test_a_wrong_check_digit_is_no_account_but_luhn_may_make_a_card() -> None:
    # 9990001600012349 passes Luhn by chance, so the card rule (unchanged) takes
    # it; the account rule does not.
    assert redact("99900016-00012349") == Redaction(text="[card]", found={"card": 1})
    assert redact("99900016-00012340") == Redaction(text="99900016-00012340", found={})


def test_the_sixteenth_digit_of_a_twenty_four_digit_number_is_free() -> None:
    # The 16-digit rule applied to the first 16 digits of this number fails.
    assert redact("99900016-12345678").text == "99900016-12345678"
    assert redact("99900016-12345678-00000125").text == "[account]"


def test_a_sixteen_digit_number_before_an_unrelated_block_is_replaced_alone() -> None:
    result = redact("99900016-00012348 12345678")

    assert result.text == "[account] 12345678"
    assert dict(result.found) == {"account": 1}


@pytest.mark.parametrize(
    "iban",
    [
        "HU83 9990 0016 0001 2348 0000 0000",
        "HU839990001600012348" + "00000000",
        "HU26 9990 0016 1234 5678 0000 0125",
    ],
)
def test_a_hungarian_iban_is_still_an_iban(iban: str) -> None:
    result = redact(f"IBAN {iban} ok")

    assert result.text == f"IBAN {IBAN_PLACEHOLDER} ok"
    assert dict(result.found) == {"iban": 1}


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("41111111-11111111", "[card]"),
        ("4111 1111 1111 1111", "[card]"),
        ("4111-1111-1111-1111", "[card]"),
    ],
)
def test_a_card_is_not_taken_for_an_account_number(text: str, expected: str) -> None:
    result = redact(text)

    assert result.text == expected
    assert dict(result.found) == {"card": 1}


# The personal identification number's rule, written a second time here so that
# the tests do not trust the code (Act 1996/XX, Annex 3): the weights run 1 to
# 10 for a person born before 1997, and 10 to 1 after.
OLD_WEIGHTS = tuple(range(1, 11))
NEW_WEIGHTS = tuple(range(10, 0, -1))


def _remainder(first_ten: str, weights: tuple[int, ...]) -> int:
    return sum(int(d) * w for d, w in zip(first_ten, weights, strict=True)) % 11


def _with_check(first_ten: str, weights: tuple[int, ...]) -> str | None:
    remainder = _remainder(first_ten, weights)
    return None if remainder == 10 else first_ten + str(remainder)


@pytest.mark.parametrize("value", NATIONAL_IDS)
def test_a_personal_identification_number_is_replaced_on_its_own(value: str) -> None:
    result = redact(f"Personal data {value} given.")

    assert result.text == f"Personal data {NATIONAL_ID_PLACEHOLDER} given."
    assert dict(result.found) == {"national_id": 1}


@pytest.mark.parametrize(
    "text",
    [
        "1-800101-1239",
        "2-851231-4561",
        "3-050315-7897",
        "12345678901",
        "00000000000",
        "1-800101-123",
        "1-8001011-238",
        "1-800101-12388",
        "1 800101 1238",
        "1800101 1238",
    ],
)
def test_a_personal_number_with_a_wrong_check_digit_or_form_is_left_alone(
    text: str,
) -> None:
    assert redact(text) == Redaction(text=text, found={})


def test_a_leading_three_or_four_may_mean_either_century() -> None:
    # 3-050315-7896 fails the weights of 2005, but is valid for 1805 (the old
    # weights), a date the leading digit also allows.
    assert redact("3-050315-7895").text == "[national-id]"
    assert redact("3-050315-7896").text == "[national-id]"
    assert redact("3-050315-7897").text == "3-050315-7897"
    assert redact("2-050315-7896").text == "2-050315-7896"


def test_the_old_weights_decide_for_a_person_born_before_1997() -> None:
    first_ten = "1800101123"
    old, new = _with_check(first_ten, OLD_WEIGHTS), _with_check(first_ten, NEW_WEIGHTS)
    assert old == "18001011238"
    assert new is not None
    assert new != old

    assert redact(old).text == "[national-id]"
    assert redact(new).text == new


def test_the_new_weights_decide_for_a_person_born_after_1996() -> None:
    first_ten = "2990412555"
    old, new = _with_check(first_ten, OLD_WEIGHTS), _with_check(first_ten, NEW_WEIGHTS)
    assert new == "29904125559"
    assert old is not None
    assert old != new

    assert redact(new).text == "[national-id]"
    assert redact(old).text == old


@pytest.mark.parametrize(
    ("first_ten", "weights"),
    [
        ("1801301123", OLD_WEIGHTS),
        ("1800001123", OLD_WEIGHTS),
        ("1800230123", OLD_WEIGHTS),
        ("1800132123", OLD_WEIGHTS),
        ("1800100123", OLD_WEIGHTS),
        ("1810229123", OLD_WEIGHTS),
        ("9800101123", OLD_WEIGHTS),
        ("0800101123", OLD_WEIGHTS),
        ("2991301555", NEW_WEIGHTS),
        ("2990431555", NEW_WEIGHTS),
    ],
)
def test_a_number_that_passes_the_check_but_has_no_such_date_is_left_alone(
    first_ten: str, weights: tuple[int, ...]
) -> None:
    number = _with_check(first_ten, weights)
    assert number is not None

    assert redact(number) == Redaction(text=number, found={})


def test_the_twenty_ninth_of_february_needs_a_leap_year() -> None:
    leap = _with_check("1800229123", OLD_WEIGHTS)
    other = _with_check("1810229123", OLD_WEIGHTS)
    assert leap is not None
    assert other is not None

    assert redact(leap).text == "[national-id]"
    assert redact(other).text == other


def test_a_prefix_whose_remainder_is_ten_has_no_valid_check_digit() -> None:
    first_ten = next(
        f"1800101{tail:03d}"
        for tail in range(1000)
        if _remainder(f"1800101{tail:03d}", OLD_WEIGHTS) == 10
    )

    for check in "0123456789":
        text = first_ten + check

        assert redact(text) == Redaction(text=text, found={})


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("TAJ: 123 456 788", "TAJ: [national-id]"),
        ("TAJ szám 123456788", "TAJ szám [national-id]"),
        ("TAJ-szám: 987 654 322.", "TAJ-szám: [national-id]."),
        ("tajszám 222222220", "tajszám [national-id]"),
        ("tajszám: 222 222 220, kérem", "tajszám: [national-id], kérem"),
        ("TAJ number: 123456788", "TAJ number: [national-id]"),
        ("my TAJ no. 123 456 788", "my TAJ no. [national-id]"),
        ("TAJ szám: 123 456 788", "TAJ szám: [national-id]"),
        (
            "Társadalombiztosítási azonosító jel: 123456788",
            "Társadalombiztosítási azonosító jel: [national-id]",
        ),
        ("social security number 123456788", "social security number [national-id]"),
        ("tAj 123456788", "tAj [national-id]"),
        ("TAJ123456788", "TAJ[national-id]"),
        ("TAJ-123456788", "TAJ-[national-id]"),
        (
            "TAJ" + "." * WORD_GAP_MAX_CHARS + "123456788",
            "TAJ" + "." * WORD_GAP_MAX_CHARS + "[national-id]",
        ),
        (f"TAJ: 123{NBSP}456{NBSP}788", "TAJ: [national-id]"),
        ("adóazonosító jel: 8296301237", "adóazonosító jel: [national-id]"),
        ("adoazonosito: 8320104564", "adoazonosito: [national-id]"),
        ("ADÓAZONOSÍTÓ 8462507898", "ADÓAZONOSÍTÓ [national-id]"),
        ("adóazonosító: 8296301237.", "adóazonosító: [national-id]."),
        ("tax ID: 8296301237", "tax ID: [national-id]"),
        (
            "tax identification number 8320104564",
            "tax identification number [national-id]",
        ),
        ("tax number: 8462507898", "tax number: [national-id]"),
        ("adószám: 12345676242", "adószám: [tax-number]"),
        ("adoszam 20000019102", "adoszam [tax-number]"),
        ("Adószáma: 45678909413", "Adószáma: [tax-number]"),
        ("tax number: 30000010244", "tax number: [tax-number]"),
        ("tax ID 12345676242", "tax ID [tax-number]"),
        ("személyi szám: 18001011238", "személyi szám: [national-id]"),
        ("személyi azonosító: 1-800101-1238", "személyi azonosító: [national-id]"),
        ("szemelyi szam 30503157895", "szemelyi szam [national-id]"),
        ("personal ID: 1-800101-1238", "personal ID: [national-id]"),
        ("personal ID number 18001011238", "personal ID number [national-id]"),
    ],
)
def test_an_identifier_after_its_word_is_replaced_and_the_word_stays(
    text: str, expected: str
) -> None:
    assert redact(text).text == expected


@pytest.mark.parametrize(
    "text",
    [
        "123 456 788",
        "123456788",
        "987 654 322",
        "8296301237",
        "12345676242",
        "TAJ: 123 456 789",
        "TAJ: 12345678",
        "TAJ: 1234567881",
        "TAJ: 123456788x",
        "TAJ: 123456788-1",
        "TAJ: 123-456-788",
        "TAJ: 123 456788",
        "TAJ: 123 456 78 8",
        "tajvan 123456788",
        "Taiwan 123456788",
        "adóazonosító: 8296301238",
        "adóazonosító: 9296301237",
        "adóazonosító: 8296301270",
        "adóazonosító: 82963012371",
        "adóazonosító: 829 630 1237",
        "adószám: 12345677242",
        "adószám: 123456762421",
        "tax number: 1234567624",
        "personal ID: 1-800101-1239",
        "TAJ: abc\n123456788",
        "TAJ:\r\n123456788",
        "TAJ:" + " " * (WORD_GAP_MAX_CHARS + 1) + "123456788",
        "TAJ" + "." * (WORD_GAP_MAX_CHARS + 1) + "123456788",
    ],
)
def test_a_number_without_its_word_or_with_a_wrong_check_is_left_alone(
    text: str,
) -> None:
    assert redact(text) == Redaction(text=text, found={})


def test_the_word_and_its_number_may_stand_the_bound_apart_and_no_further() -> None:
    near = "TAJ" + ":" + " " * (WORD_GAP_MAX_CHARS - 1) + "123456788"
    far = "TAJ" + ":" + " " * WORD_GAP_MAX_CHARS + "123456788"

    assert redact(near) == Redaction(
        text="TAJ" + ":" + " " * (WORD_GAP_MAX_CHARS - 1) + "[national-id]",
        found={"national_id": 1},
    )
    assert redact(far) == Redaction(text=far, found={})


def test_an_identifier_after_its_word_in_a_longer_line_is_replaced_in_place() -> None:
    text = "Name: Anna, TAJ: 123 456 788, adószám: 12345676242. Call back."

    result = redact(text)

    assert result.text == (
        "Name: Anna, TAJ: [national-id], adószám: [tax-number]. Call back."
    )
    assert dict(result.found) == {"national_id": 1, "tax_number": 1}


@pytest.mark.parametrize(("kind", "value"), OWN_VALUES)
@pytest.mark.parametrize(
    "wrapper",
    ["ref-{}", "x{}", "{}x", "{}_", "{}-5", "7{}", "trace_{}_id", "{}0"],
)
def test_the_right_digits_inside_a_longer_token_are_left_alone(
    kind: str, value: str, wrapper: str
) -> None:
    text = wrapper.format(value)

    assert redact(text) == Redaction(text=text, found={}), kind


def test_every_new_kind_in_one_text_is_counted_by_kind() -> None:
    text = (
        "Phone 06 30 123 4567 and +36 20 123 4567, tax 12345676-2-42, "
        "account 99900016-00012348, ID 1-800101-1238, TAJ 123 456 788."
    )

    result = redact(text)

    assert result.text == (
        "Phone [phone] and [phone], tax [tax-number], account [account], "
        "ID [national-id], TAJ [national-id]."
    )
    assert dict(result.found) == {
        "phone": 2,
        "tax_number": 1,
        "account": 1,
        "national_id": 2,
    }


def test_two_forms_of_one_kind_in_separate_passes_add_up() -> None:
    result = redact("+36 30 123 4567 / 06 30 123 4567 / 0036 30 123 4567")

    assert result.text == "[phone] / [phone] / [phone]"
    assert dict(result.found) == {"phone": 3}


def test_redacting_a_second_time_changes_nothing_more() -> None:
    once = redact(
        "06 30 123 4567, 12345676-2-42, 99900016-00012348, 1-800101-1238, "
        "TAJ: 123456788, adószám: 12345676242"
    )

    twice = redact(once.text)

    assert twice.text == once.text
    assert dict(twice.found) == {}


# ``json.dumps`` writes a line break as a backslash and a letter; the letter
# must not join the value after it to a word, nor the word after it to a letter.
ESCAPES = ["\n", "\t", "\r\n", "\b", "\f"]
ESCAPE_IDS = ["newline", "tab", "crlf", "backspace", "formfeed"]
JSON_VALUES = {
    "phone": "06 30 123 4567",
    "phone-compact": "06301234567",
    "phone-long": "0036 30 123 4567",
    "tax_number": "12345676-2-42",
    "account": "99900016-00012348",
    "account-long": "99900016-12345678-00000125",
    "national_id": "1-800101-1238",
    "national_id-compact": "18001011238",
}
JSON_WORD_VALUES = {
    "taj": ("TAJ: 123 456 788", "TAJ: [national-id]"),
    "tax-id": ("adóazonosító: 8296301237", "adóazonosító: [national-id]"),
    "tax-number": ("adószám: 12345676242", "adószám: [tax-number]"),
}


@pytest.mark.parametrize("escape", ESCAPES, ids=ESCAPE_IDS)
@pytest.mark.parametrize("name", list(JSON_VALUES))
def test_a_value_after_a_json_escape_is_replaced_and_the_json_stays_valid(
    name: str, escape: str
) -> None:
    kind = name.split("-")[0]
    description = f"first line{escape}{JSON_VALUES[name]}{escape}last line"

    result = redact(json.dumps({"description": description}, ensure_ascii=False))

    assert json.loads(result.text) == {
        "description": f"first line{escape}{PLACEHOLDERS[kind]}{escape}last line"
    }
    assert dict(result.found) == {kind: 1}


@pytest.mark.parametrize("escape", ESCAPES, ids=ESCAPE_IDS)
@pytest.mark.parametrize("name", list(JSON_WORD_VALUES))
def test_a_word_after_a_json_escape_still_finds_its_number(
    name: str, escape: str
) -> None:
    text, expected = JSON_WORD_VALUES[name]
    description = f"first line{escape}{text}{escape}last line"

    result = redact(json.dumps({"description": description}, ensure_ascii=False))

    assert json.loads(result.text) == {
        "description": f"first line{escape}{expected}{escape}last line"
    }


def test_a_json_escape_between_the_word_and_its_number_is_a_line_break() -> None:
    text = json.dumps({"description": "TAJ:\n123456788"}, ensure_ascii=False)

    assert redact(text) == Redaction(text=text, found={})


@pytest.mark.parametrize("space", [NBSP, THIN_SPACE, "\t"], ids=["nbsp", "thin", "tab"])
def test_another_space_joins_the_groups_of_a_national_number_and_an_account(
    space: str,
) -> None:
    phone = redact(f"call 06{space}30{space}123{space}4567 now")
    account = redact(f"pay 99900016{space}00012348 now")

    assert phone.text == "call [phone] now"
    assert account.text == "pay [account] now"


# Linear time (T-73): a text four times as long takes four times as long, not
# sixteen. The limit sits at twice the linear growth, as in test_redaction.py;
# the thread's CPU time is measured, never the wall clock, and the best of RUNS
# runs is taken at each size.
SMALL_LENGTH = 10_000
LARGE_LENGTH = 40_000
MAX_GROWTH = 8
RUNS = 5
Shape = Callable[[int], str]
ADVERSARIAL: dict[str, Shape] = {
    "phone-prefix": lambda n: "06 " * (n // 3),
    "phone-hyphen": lambda n: "06-" * (n // 3),
    "phone-paren": lambda n: "(06" * (n // 3),
    "phone-groups": lambda n: "06 30 " * (n // 6),
    "phone-digits": lambda n: "06" + "3" * n,
    "phone-slash": lambda n: "06/" + "30/" * (n // 3),
    "phone-dots": lambda n: "06." * (n // 3),
    "phone-international-prefix": lambda n: "0036 " * (n // 5),
    "phone-double-zero": lambda n: "00 " * (n // 3),
    "phone-separators": lambda n: "06" + "- " * (n // 2),
    "tax-hyphens": lambda n: "1-" * (n // 2),
    "tax-blocks": lambda n: "12345678-1-" * (n // 11),
    "tax-long": lambda n: "12345676-2-42" * (n // 13),
    "account-hyphens": lambda n: "99900016-" * (n // 9),
    "account-spaces": lambda n: "99900016 " * (n // 9),
    "account-digits": lambda n: "9" * n,
    "national-id-hyphens": lambda n: "1-800101-" * (n // 9),
    "national-id-digits": lambda n: "1800101" * (n // 7),
    "national-id-long": lambda n: "1-800101-1238" * (n // 13),
    "word-short": lambda n: "TAJ: 1 " * (n // 7),
    "word-repeated": lambda n: "taj" * (n // 3),
    "word-colons": lambda n: "TAJ" + ":" * n,
    "word-spaces": lambda n: "adószám" + " " * n,
    "word-then-digits": lambda n: "TAJ " + "1" * n,
    "word-many": lambda n: "tax ID " * (n // 7),
    "word-gap-edge": lambda n: ("TAJ" + "x" * WORD_GAP_MAX_CHARS) * (n // 15),
    "word-hyphenated": lambda n: "TAJ-szám " * (n // 9),
}


def _best_time(text: str) -> float:
    best = float("inf")
    for _ in range(RUNS):
        started = time.thread_time()
        redact(text)
        best = min(best, time.thread_time() - started)
    return best


@pytest.mark.parametrize("name", list(ADVERSARIAL))
def test_an_adversarial_text_is_redacted_in_linear_time(name: str) -> None:
    small, large = ADVERSARIAL[name](SMALL_LENGTH), ADVERSARIAL[name](LARGE_LENGTH)
    assert len(small) >= SMALL_LENGTH - 20
    assert len(large) >= LARGE_LENGTH - 20

    growth = _best_time(large) / _best_time(small)

    assert growth < MAX_GROWTH
