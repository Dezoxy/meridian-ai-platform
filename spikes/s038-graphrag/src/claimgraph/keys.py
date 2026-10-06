"""What identifies a customer, an address and an asset, given that the data
holds no customer ID, no address ID and no asset ID.

- A customer is the holder's e-mail, lower-cased and trimmed.
- An address is the street, city and country of a holder's address, case and
  spacing normalised.
- A vehicle is its registration, upper-cased with no spaces. A home is the
  street, city and country of its insured address, normalised as above.

A key is stored and shown only as a digest: a prefix and the first 12
hexadecimal digits of a SHA-256 over a namespace and the normalised value.
A digest is a pure function of the field, so two policies that share the
field share the ID, and it does not depend on the order the files are read in.
It is not a secret: a short value can be guessed and hashed again. It only
keeps the field's text out of every output of this spike.
"""

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

DIGEST_HEX_DIGITS = 12


@dataclass(frozen=True, slots=True)
class Identity:
    """The three IDs one policy is linked to."""

    customer: str
    address: str
    asset: str


def _digest(prefix: str, namespace: str, value: str) -> str:
    material = f"claimgraph/{namespace}/{value}".encode()
    return f"{prefix}-{hashlib.sha256(material).hexdigest()[:DIGEST_HEX_DIGITS]}"


def _words(value: str) -> str:
    return " ".join(value.casefold().split())


def _address_value(address: Mapping[str, Any]) -> str:
    return "|".join(
        (
            _words(address["street"]),
            _words(address["city"]),
            address["country"].strip().upper(),
        )
    )


def customer_id(policy: Mapping[str, Any]) -> str:
    return _digest("CUS", "customer", policy["holder"]["email"].strip().lower())


def address_id(policy: Mapping[str, Any]) -> str:
    return _digest("ADR", "address", _address_value(policy["holder"]["address"]))


def asset_family(policy: Mapping[str, Any]) -> str:
    return "vehicle" if "registration" in policy["insured_object"] else "home"


def asset_id(policy: Mapping[str, Any]) -> str:
    insured = policy["insured_object"]
    if asset_family(policy) == "vehicle":
        registration = "".join(insured["registration"].split()).upper()
        return _digest("AST", "vehicle", registration)
    return _digest("AST", "home", _address_value(insured["address"]))


def asset_field(policy: Mapping[str, Any]) -> str:
    if asset_family(policy) == "vehicle":
        return "insured_object.registration"
    return "insured_object.address"


def derived_identity(policy: Mapping[str, Any]) -> Identity:
    return Identity(customer_id(policy), address_id(policy), asset_id(policy))
