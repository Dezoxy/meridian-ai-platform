"""The one check of a service address, and the types built on it (S059)."""

from typing import Annotated

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from meridian.platform.common.env import (
    BaseHttpUrl,
    HttpUrl,
    service_base_url_problem,
    service_url_problem,
)

SECRET = "s3cret-value"  # noqa: S105

USABLE_ADDRESSES = [
    "http://svc.invalid",
    "https://svc.invalid",
    "http://svc.invalid:8080",
    "https://svc.test:8443/",
    "http://svc.invalid/v1",
    "https://svc.invalid:8443/base/path",
    "http://127.0.0.1:8000",
    "http://[::1]:8000",
]
# Each shape breaks one rule; the user and password carry the secret.
REFUSED_ADDRESSES = {
    "leading whitespace": " http://svc.invalid",
    "trailing whitespace": "http://svc.invalid ",
    "trailing newline": "http://svc.invalid\n",
    "a port that is not a number": "http://svc.invalid:abc",
    "a port out of range": "http://svc.invalid:99999",
    # ``urlsplit`` drops a tab, a carriage return and a line feed anywhere in the
    # text and reads the rest as a host, so these must be refused before it.
    "a line feed inside": "http://ho\nst",
    "a carriage return inside": "https://ho\rst",
    "a tab inside": "http://ho\tst",
    "a null character": "http://host\x00",
    "a space inside": "http://ho st",
    "a space in the path": "http://host/a b",
    "an ideographic space": "http://　host",
    "port 0": "http://host:0",
    "port 0 with leading zeros": "http://host:00",
    # ``urlsplit`` reads an empty port as no port at all.
    "an empty port": "http://host:",
    "an empty port and a path": "http://host:/v1",
    "an empty port after an IPv6 literal": "http://[::1]:",
    "an unclosed bracket": "http://[svc.invalid",
    "no scheme": "svc.invalid:8080",
    "a scheme that is not http": "ftp://svc.invalid",
    "a file address": "file:///etc/passwd",
    "no host": "http://",
    "no host and a port": "http://:80",
    "a user name": f"http://{SECRET}@svc.invalid",
    "a user name and a password": f"http://operator:{SECRET}@svc.invalid:8080",
    "a password without a user name": f"http://:{SECRET}@svc.invalid",
    "a query": f"http://svc.invalid/v1?token={SECRET}",
    "an empty query": "http://svc.invalid/v1?",
    "a fragment": f"http://svc.invalid/v1#{SECRET}",
    "an empty fragment": "http://svc.invalid/v1#",
}


@pytest.mark.parametrize("address", USABLE_ADDRESSES)
def test_a_usable_service_address_has_no_problem(address: str) -> None:
    assert service_url_problem(address) is None


@pytest.mark.parametrize("address", REFUSED_ADDRESSES.values(), ids=REFUSED_ADDRESSES)
def test_a_refused_service_address_names_the_rule_and_not_the_address(
    address: str,
) -> None:
    problem = service_url_problem(address)

    assert problem
    for fragment in (SECRET, "operator", "svc.invalid"):
        assert fragment not in problem


@pytest.mark.parametrize(
    "address", ["http://svc.invalid", "https://svc.invalid:8443/", "http://svc.test/"]
)
def test_a_base_address_has_no_problem(address: str) -> None:
    assert service_base_url_problem(address) is None


@pytest.mark.parametrize(
    "address",
    [
        "http://svc.invalid/mcp",
        "http://svc.invalid/a/",
        "http://svc.invalid//",
        f"http://svc.invalid/{SECRET}",
    ],
)
def test_an_address_with_a_path_beyond_a_slash_is_not_a_base_address(
    address: str,
) -> None:
    problem = service_base_url_problem(address)

    assert problem
    assert SECRET not in problem
    assert "svc.invalid" not in problem


@pytest.mark.parametrize("address", REFUSED_ADDRESSES.values(), ids=REFUSED_ADDRESSES)
def test_an_address_the_shared_check_refuses_is_not_a_base_address(
    address: str,
) -> None:
    problem = service_base_url_problem(address)

    assert problem
    assert service_url_problem(address) is not None
    for fragment in (SECRET, "operator", "svc.invalid"):
        assert fragment not in problem


class _Holder(BaseModel):
    # As the real settings classes: an error shows the rule, not the input.
    model_config = ConfigDict(hide_input_in_errors=True)

    address: Annotated[str, HttpUrl]
    base: BaseHttpUrl | None = None


@pytest.mark.parametrize("address", USABLE_ADDRESSES)
def test_the_http_url_type_accepts_a_usable_address_with_a_path_and_keeps_it(
    address: str,
) -> None:
    assert _Holder(address=address).address == address


@pytest.mark.parametrize("address", REFUSED_ADDRESSES.values(), ids=REFUSED_ADDRESSES)
def test_the_http_url_type_refuses_what_the_check_refuses_without_the_address(
    address: str,
) -> None:
    with pytest.raises(ValidationError) as raised:
        _Holder(address=address)

    assert "address" in str(raised.value)
    for fragment in (SECRET, "operator", "svc.invalid"):
        assert fragment not in str(raised.value)


@pytest.mark.parametrize("address", ["http://svc.invalid", "https://svc.invalid:8443/"])
def test_the_base_http_url_type_accepts_a_base_address(address: str) -> None:
    assert _Holder(address="http://x.invalid", base=address).base == address


@pytest.mark.parametrize(
    "address", ["http://svc.invalid/mcp", f"http://operator:{SECRET}@svc.invalid"]
)
def test_the_base_http_url_type_refuses_a_path_and_a_password_without_the_address(
    address: str,
) -> None:
    with pytest.raises(ValidationError) as raised:
        _Holder(address="http://x.invalid", base=address)

    assert "base" in str(raised.value)
    for fragment in (SECRET, "operator", "svc.invalid"):
        assert fragment not in str(raised.value)
