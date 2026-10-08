"""The ID token's check at the pages' callback (S021, Y3, T-05). Wired to no
route. The third module that parses and verifies a token (with ``signin``, the
access token's, and ``signinkeys``, the key set's), and the only one the page
flow uses; ``signinflow`` calls it and does no token work itself.

It is NOT ``check_bearer``, because the two ask different questions. The key is
the key set's (``alg`` RS256 and a ``kid`` first, as there). The library
verifies the signature and nothing else, and every claim is checked here, so
that no library default decides what is accepted: PyJWT accepts an ``aud`` list
that merely contains the audience, and does not check ``typ`` or ``azp``.

- ``iss`` is the pinned issuer.
- ``aud`` is the client id: the text, or a list of that one text.
- ``exp`` is ahead of ``now``, with no leeway: a session cannot outlive its
  token, so a token inside a leeway could not make one.
- ``iat`` is present and no more than ``LEEWAY_SECONDS`` ahead; ``nbf`` is
  checked when present (Keycloak sends none).
- ``typ`` is the setting's (``ID`` for the pinned Keycloak; an access token is
  ``Bearer`` and a refresh token ``Refresh``). Empty for an issuer that sends no
  ``typ`` (Entra), and then not enforced.
- ``azp`` is the client id when present.
- ``nonce`` is the transaction's (constant-time compare); a missing one is
  refused.
- the subject is a text of 1 to ``MAX_SUBJECT_CHARS`` characters.

The roles come from the ID token's configured claim: a list of short strings, or
none. Whatever a token holds, ``principal_or_reason`` answers with a
``Principal`` or a ``FlowReason`` and never raises (a library's error is mapped
to a reason by its class and is not chained), except for ``CalledFromEventLoop``
from the key set, which is the caller's mistake.
"""

import hmac
import math
import re
from collections.abc import Mapping
from typing import Any

import jwt
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey

from meridian.platform.common.signin import (
    ALGORITHMS,
    LEEWAY_SECONDS,
    MAX_ROLE_CHARS,
    MAX_ROLES,
    MAX_SUBJECT_CHARS,
    MAX_TOKEN_CHARS,
    Population,
    Principal,
)
from meridian.platform.common.signinkeys import (
    KeySet,
    KeySetUnavailable,
    UnknownKeyId,
)
from meridian.platform.common.signinstate import FlowReason

_COMPACT = re.compile(r"[A-Za-z0-9_-]*\.[A-Za-z0-9_-]*\.[A-Za-z0-9_-]*")


def _header_of(token: str) -> dict[str, Any] | None:
    """The token's header, read without verifying anything."""
    if not _COMPACT.fullmatch(token):
        return None
    try:
        return jwt.get_unverified_header(token)
    except Exception:  # a refusal, whatever the library raises
        return None


def _number(value: object) -> float | None:
    """A finite JSON number; a bool, NaN, infinity and an integer too large for
    a float are not."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


def _roles_of(claim: object) -> frozenset[str]:
    """A list of strings within the bounds, else no roles at all."""
    if not isinstance(claim, list) or len(claim) > MAX_ROLES:
        return frozenset()
    if not all(isinstance(r, str) and 0 < len(r) <= MAX_ROLE_CHARS for r in claim):
        return frozenset()
    return frozenset(claim)


def _verified(token: str, key: RSAPublicKey) -> dict[str, Any] | FlowReason:
    """The claims of a token whose signature holds under ``key``. The library
    checks the signature and RS256 and nothing else."""
    try:
        return jwt.decode(
            token,
            key,
            algorithms=list(ALGORITHMS),
            options={
                "verify_signature": True,
                "verify_exp": False,
                "verify_nbf": False,
                "verify_iat": False,
                "verify_aud": False,
                "verify_iss": False,
                "verify_sub": False,
                "verify_jti": False,
                "require": [],
            },
        )
    except jwt.InvalidSignatureError:
        return FlowReason.ID_SIGNATURE
    except Exception:  # its other errors, and whatever hostile input raises
        return FlowReason.ID_MALFORMED


def _times_problem(claims: Mapping[str, Any], now: float) -> FlowReason | None:
    expires, issued = _number(claims.get("exp")), _number(claims.get("iat"))
    if expires is None or issued is None:
        return FlowReason.ID_CLAIMS
    ahead = [issued]
    if "nbf" in claims:
        not_before = _number(claims["nbf"])
        if not_before is None:
            return FlowReason.ID_CLAIMS
        ahead.append(not_before)
    if now >= expires:
        return FlowReason.ID_EXPIRED
    if any(now < value - LEEWAY_SECONDS for value in ahead):
        return FlowReason.ID_NOT_YET_VALID
    return None


class IdTokenCheck:
    """The check of one client's ID tokens at one issuer, against the key set."""

    def __init__(
        self,
        keys: KeySet,
        *,
        population: Population,
        issuer: str,
        client_id: str,
        required_typ: str,
        subject_claim: str,
        roles_claim: str,
    ) -> None:
        self._keys = keys
        self._population = population
        self._issuer = issuer
        self._client_id = client_id
        self._required_typ = required_typ
        self._subject_claim = subject_claim
        self._roles_claim = roles_claim

    def principal_or_reason(
        self, token: str, nonce: str, now: float
    ) -> Principal | FlowReason:
        """The principal an ID token proves for the transaction whose nonce this
        is, or why not. Its ``via`` is ``cookie``: it is the session's."""
        claims = self._claims_of(token)
        if isinstance(claims, FlowReason):
            return claims
        problem = self._problem(claims, nonce, now)
        if problem is not None:
            return problem
        return Principal(
            population=self._population,
            issuer=self._issuer,
            subject=claims[self._subject_claim],
            roles=_roles_of(claims.get(self._roles_claim)),
            expires_at=math.floor(claims["exp"]),
            via="cookie",
        )

    def _claims_of(self, token: str) -> dict[str, Any] | FlowReason:
        """The claims of a token signed by the issuer: size, header, key and
        signature, in the order ``check_bearer`` uses."""
        if len(token) > MAX_TOKEN_CHARS:
            return FlowReason.ID_TOO_LARGE
        header = _header_of(token)
        if header is None:
            return FlowReason.ID_MALFORMED
        if header.get("alg") not in ALGORITHMS:
            return FlowReason.ID_ALGORITHM
        kid = header.get("kid")
        if not isinstance(kid, str) or not kid:
            return FlowReason.ID_NO_KEY_ID
        try:
            key = self._keys.key_for(kid)
        except UnknownKeyId:
            return FlowReason.ID_UNKNOWN_KEY
        except KeySetUnavailable:
            return FlowReason.ID_KEYS_UNAVAILABLE
        return _verified(token, key)

    def _problem(
        self, claims: Mapping[str, Any], nonce: str, now: float
    ) -> FlowReason | None:
        """Why verified claims are not an ID token for this client, this issuer
        and this transaction, or None."""
        if claims.get("iss") != self._issuer:
            return FlowReason.ID_ISSUER
        audience = claims.get("aud")
        if audience != self._client_id and audience != [self._client_id]:
            return FlowReason.ID_AUDIENCE
        timing = _times_problem(claims, now)
        if timing is not None:
            return timing
        if self._required_typ and claims.get("typ") != self._required_typ:
            return FlowReason.ID_TYPE
        if "azp" in claims and claims["azp"] != self._client_id:
            return FlowReason.ID_AUTHORIZED_PARTY
        given = claims.get("nonce")
        if not isinstance(given, str) or not hmac.compare_digest(
            given.encode(), nonce.encode()
        ):
            return FlowReason.ID_NONCE
        subject = claims.get(self._subject_claim)
        if not isinstance(subject, str) or not 0 < len(subject) <= MAX_SUBJECT_CHARS:
            return FlowReason.ID_CLAIMS
        return None
