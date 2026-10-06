"""Check digits and numbering-plan tables of Hungarian identifiers (S067).

Pure functions on a string of ASCII digits: no I/O, no logging, and nothing
here sees text that is not already a candidate. ``redaction`` finds the
candidates and decides where each may stand; this module says whether the
digits are a number of the kind. Each rule cites its public source.

A number that passes a check can still be someone's or no one's: the
algorithms are public, so a check cuts the false positives, it does not prove
that a number was issued."""

import datetime
from collections.abc import Sequence
from types import MappingProxyType

# Phone numbers: the National Media and Infocommunications Authority's
# "Magyar nemzeti számozási terv" (official notice of 10 March 2026, page
# updated 8 April 2026, https://nmhh.hu/cikk/185491). The domestic number has
# the area or service code in it: this is the number of digits from the code
# on, by two-digit code. Budapest is the one-digit code 1, with eight. Not in
# the table, so not a phone number here: the deleted and unassigned codes (39,
# 40, 41, 43, 51, 58, 60, 61, 64, 65, 67, 81, 86, 97, 98) and 55, which the
# plan lists as test numbers (no subscriber).
BUDAPEST_CODE = "1"
BUDAPEST_LENGTH = 8
_CODES_BY_LENGTH = {
    8: (
        "22 23 24 25 26 27 28 29 32 33 34 35 36 37 42 44 45 46 47 48 49 52 53 54"
        " 56 57 59 62 63 66 68 69 72 73 74 75 76 77 78 79 80 82 83 84 85 87 88 89"
        " 90 91 92 93 94 95 96 99"
    ),
    9: "20 21 30 31 38 50 70",
    12: "71",
}
DOMESTIC_LENGTHS = MappingProxyType(
    {
        code: length
        for length, codes in _CODES_BY_LENGTH.items()
        for code in codes.split()
    }
)
# The domestic prefix and the international one with the country code: the
# prefix is not part of the number (NMHH 14/2020 §26).
DOMESTIC_PREFIXES = ("0036", "06")

# Social security number (TAJ), Act 1996/XX Annex 2: nine digits; the eighth
# are a serial and the ninth is the remainder of the sum of the first eight,
# the odd places times 3 and the even places times 7, divided by ten.
SOCIAL_SECURITY_WEIGHTS = (3, 7, 3, 7, 3, 7, 3, 7)

# Tax identification number (adóazonosító jel), Act 1996/XX Annex 1: ten
# digits, the first an 8; each of the first nine times its place (1 to 9),
# summed, divided by 11: the remainder is the tenth digit, and a prefix whose
# remainder is 10 is never issued.
TAX_ID_LEADING_DIGIT = "8"

# Tax number (adószám) and domestic account number: the check of GIRO's
# "Általános Üzletszabályzat" IG1 Annex 6 and MNB decree 35/2017 Annex 1.
# Weights 9, 7, 3, 1 repeating from the first digit of the run; the check
# digit is ten less the units digit of the sum (0 when that is ten).
GIRO_WEIGHTS = (9, 7, 3, 1)
TAX_NUMBER_STEM_DIGITS = 8
TAX_NUMBER_DIGITS = 11
ACCOUNT_SHORT_LENGTH = 16
ACCOUNT_LONG_LENGTH = 24
# The runs of an account number the check digits cover: digits 1 to 7 (the
# check is the 8th) and 9 to 15 (16 digits: the check is the 16th) or 9 to 23
# (24 digits: the check is the 24th; the 16th is free but is in the sum).
ACCOUNT_BANK_RUN = slice(0, 7)
ACCOUNT_BANK_CHECK = 7
ACCOUNT_RUNS = MappingProxyType(
    {
        ACCOUNT_SHORT_LENGTH: (slice(8, 15), 15),
        ACCOUNT_LONG_LENGTH: (slice(8, 23), 23),
    }
)

# Personal identification number (személyi azonosító), Act 1996/XX Annex 3:
# eleven digits. The first says sex and century (the table below: the
# centuries a leading digit can mean), digits 2 to 7 are the birth date
# YYMMDD, 8 to 10 a serial, the 11th the check. A person born before 1997
# has the weights 1 to 10 on digits 1 to 10, one born after 1996 has 10 to 1;
# the remainder of the sum divided by 11 is the check, and 10 is never issued.
# A Hungarian born 1800 to 1899 or 2000 to 2099 both have 3 or 4; a person
# who is not Hungarian has 5 to 8, and the law lists none born after 1996.
PERSONAL_ID_LENGTH = 11
PERSONAL_ID_CENTURIES = MappingProxyType(
    {
        "1": (1900,),
        "2": (1900,),
        "3": (1800, 2000),
        "4": (1800, 2000),
        "5": (1900,),
        "6": (1900,),
        "7": (1800,),
        "8": (1800,),
    }
)
PERSONAL_ID_NEW_WEIGHTS_FROM = datetime.date(1997, 1, 1)
PERSONAL_ID_OLD_WEIGHTS = tuple(range(1, 11))
PERSONAL_ID_NEW_WEIGHTS = tuple(range(10, 0, -1))
PERSONAL_ID_FOREIGN_LEADING_DIGITS = "5678"
PERSONAL_ID_MODULUS = 11
TAX_ID_MODULUS = 11


def _weighted_sum(digits: str, weights: Sequence[int]) -> int:
    return sum(
        int(digit) * weights[index % len(weights)] for index, digit in enumerate(digits)
    )


def _giro_check(digits: str) -> int:
    return (10 - _weighted_sum(digits, GIRO_WEIGHTS) % 10) % 10


def national_phone_holds(digits: str) -> bool:
    """Whether the digits are a domestic number after the prefix ``06`` or
    ``0036``: a code of the numbering plan and exactly its length."""
    for prefix in DOMESTIC_PREFIXES:
        if digits.startswith(prefix):
            number = digits[len(prefix) :]
            if number[:1] == BUDAPEST_CODE:
                return len(number) == BUDAPEST_LENGTH
            return DOMESTIC_LENGTHS.get(number[:2]) == len(number)
    return False


def social_security_holds(digits: str) -> bool:
    """Whether nine digits are a social security number (TAJ)."""
    return len(digits) == len(SOCIAL_SECURITY_WEIGHTS) + 1 and _weighted_sum(
        digits[:-1], SOCIAL_SECURITY_WEIGHTS
    ) % 10 == int(digits[-1])


def tax_id_holds(digits: str) -> bool:
    """Whether ten digits are a tax identification number (adóazonosító jel)."""
    if len(digits) != 10 or digits[0] != TAX_ID_LEADING_DIGIT:
        return False
    places = tuple(range(1, 10))
    remainder = _weighted_sum(digits[:-1], places) % TAX_ID_MODULUS
    return remainder == int(digits[-1])


def tax_number_holds(digits: str) -> bool:
    """Whether eleven digits are a tax number (adószám): its first eight, the
    stem, are seven digits and their check digit. The VAT code and the county
    code that follow are not checked: a list of them is not sourced."""
    stem = digits[:TAX_NUMBER_STEM_DIGITS]
    return len(digits) == TAX_NUMBER_DIGITS and _giro_check(stem[:-1]) == int(stem[-1])


def account_holds(digits: str) -> bool:
    """Whether 16 or 24 digits are a domestic account number: the bank run and
    the account run each end in their check digit, and the length decides
    which digits the second run covers."""
    runs = ACCOUNT_RUNS.get(len(digits))
    if runs is None:
        return False
    account_run, account_check = runs
    return _giro_check(digits[ACCOUNT_BANK_RUN]) == int(
        digits[ACCOUNT_BANK_CHECK]
    ) and _giro_check(digits[account_run]) == int(digits[account_check])


def personal_id_holds(digits: str) -> bool:
    """Whether eleven digits are a personal identification number: a leading
    digit, a date of birth that is a date in a century that digit allows, and
    the check digit of the variant that date selects."""
    if len(digits) != PERSONAL_ID_LENGTH:
        return False
    return any(
        _personal_id_born_in(digits, century)
        for century in PERSONAL_ID_CENTURIES.get(digits[0], ())
    )


def _personal_id_born_in(digits: str, century: int) -> bool:
    year, month, day = (int(digits[1:3]), int(digits[3:5]), int(digits[5:7]))
    try:
        born = datetime.date(century + year, month, day)
    except ValueError:
        return False
    after_1996 = born >= PERSONAL_ID_NEW_WEIGHTS_FROM
    if after_1996 and digits[0] in PERSONAL_ID_FOREIGN_LEADING_DIGITS:
        return False
    weights = PERSONAL_ID_NEW_WEIGHTS if after_1996 else PERSONAL_ID_OLD_WEIGHTS
    remainder = _weighted_sum(digits[:-1], weights) % PERSONAL_ID_MODULUS
    return remainder == int(digits[-1])
