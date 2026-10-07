"""The loader of the injection suite's cases that the claims tests share (S074).

No database. A case that is not in the file fails the test that asked for it,
with a message that names the case and the file: a skip is silent under CI.
"""

import pytest
from servicesupport import INJECTION_CASES_JSON, injection_case_claim


@pytest.mark.parametrize("case_id", ["CLM-1053", "CLM-1054"])
def test_a_known_case_loads_as_the_claim_the_suite_posts(case_id: str) -> None:
    claim = injection_case_claim(case_id)

    assert claim["claim_id"] == case_id
    assert {"claimant", "description", "documents"} <= set(claim)


def test_each_call_returns_a_claim_of_its_own() -> None:
    first = injection_case_claim("CLM-1053")
    first["description"] = "changed by the first caller"

    second = injection_case_claim("CLM-1053")

    assert second["description"] != first["description"]


def test_an_unknown_case_fails_with_a_message_that_names_it_and_the_file() -> None:
    with pytest.raises(AssertionError) as raised:
        injection_case_claim("CLM-0000")

    message = str(raised.value)
    assert "CLM-0000" in message
    assert INJECTION_CASES_JSON.name in message
    assert "injection" in message


def test_a_name_that_is_not_a_case_fails_and_is_not_skipped() -> None:
    with pytest.raises(AssertionError):
        injection_case_claim("")
