"""Redaction of Hungarian identifiers (S067): national phone numbers.

Every number here is made up (the examples of the sourced note); none is a
lookup of a person."""

import pytest
from hungariansupport import PHONES

from meridian.platform.guardrails import PHONE_PLACEHOLDER, Redaction, redact

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
