"""The pages' sign-in: the authorization-code flow with PKCE, run by the app
(S021, Y3, T-05). The Claims API runs it for the staff pages when
``MERIDIAN_SIGNIN`` is ``staff`` (``staff_signin``), off by default; no other
service does. The reasons, the transaction cookie, the return path and PKCE are
in ``signinstate``.

``SigninFlow.start`` builds the redirect to the issuer's FRONT authorization
address (the one the browser reaches) with ``response_type=code``, a ``state``,
a ``nonce`` and a PKCE challenge, ``S256`` only (the realm's discovery document
also lists ``plain``; nothing here sends it). The randoms come from ``secrets``
and travel in ONE cookie of their own, the transaction: signed, ``HttpOnly``,
``SameSite=Lax`` (the callback is a top-level navigation from another site,
which ``Strict`` would not carry), ``Secure`` as the session cookie is, ``Path``
the callback's, ten minutes long. It holds where to return to: a path of this
app under a prefix the settings allow, else the default page.

``SigninFlow.finish`` takes the callback's query and that cookie and answers
with a ``SigninOutcome``; nothing a person or an issuer sends makes it raise.
It refuses, in this order: a missing, altered, expired, other-population or
over-long transaction; a repeated or over-long ``state``, ``code``, ``error`` or
``iss``; a ``state`` that is not the transaction's (constant-time compare); the
issuer's ``error`` (its description is never read); an ``iss`` that is not the
pinned issuer (RFC 9207; Keycloak sends it); a missing code. It then exchanges
the code at the BACK-CHANNEL token address, checks the ID token, and builds the
``Principal`` and the session from that validated token and from nothing else.
``SigninOutcome.apply_to(response)`` is the only way to the cookies, and it
clears the transaction whether the person signed in or not.

The exchange is one asynchronous request run by ``asyncio.run`` in the calling
thread, under ONE deadline over the connection, the handshake, the headers and
the body, as ``signinkeys`` does it (see there for what a deadline cannot cut: a
name lookup); so ``finish`` refuses to run on an event loop
(``CalledFromEventLoop``, the caller's mistake and not a refusal): call it from
a plain ``def`` route. It POSTs ``grant_type``, ``code``, ``redirect_uri`` and
``code_verifier`` and nothing else, with the client's credential in a ``Basic``
header (``client_secret_basic``, which the realm lists), follows no redirect,
reads no proxy or certificate setting from the environment, sends
``Accept-Encoding: identity``, refuses any other ``Content-Encoding`` before a
body byte, and reads the body raw and bounded. Only ``id_token`` is read from
the answer; the access and refresh tokens are dropped and kept nowhere.

The ID token is checked by ``signinidtoken``, which is not ``check_bearer``: its
text says what it holds and why. The session is sealed as a trial before the
outcome says "signed in", so an oversized principal (a non-ASCII role is six
bytes in the cookie) is a refusal. It ends with the ID token's ``exp``: the
pinned Keycloak makes those live 300 s.

Sign-out clears the session cookie and goes to the issuer's end-session address
with ``client_id`` and ``post_logout_redirect_uri``. No token is kept, so no
``id_token_hint`` is sent. What the issuer then asks of the person, and whether
it honours the return address without the hint, is UNKNOWN: nothing readable
here says, and the flow was never run in a browser. Whether that address must be
registered on the client is a realm setting. A GET logout can be started from
another site: the route that calls ``sign_out`` is a POST that checks the origin.

What a refusal leaves: one WARNING per reason per minute, with the count the
minute held back, from a fixed text and the reason's word; never a token, a
code, a verifier, a nonce, a state, a cookie, the credential, the issuer's error
or a library's exception text (a library's error is mapped to a reason by its
class and not chained).

Residuals. The transaction is signed, not encrypted: the PKCE verifier is
readable by whoever reads the person's cookies (on kind's plain HTTP, whoever is
on the path); the client's credential, which a stolen code is useless without,
is not in it. The transaction is stateless, so one cookie can be offered again
within its ten minutes; a code is good for one exchange, so a replay buys
nothing without a fresh code. There is one transaction cookie per population: a
second tab's sign-in replaces the first's, and the first's callback is refused.
"""

import asyncio
import base64
import hmac
import json
import logging
import math
import secrets
import time
from collections.abc import Callable, Mapping
from typing import Annotated, Any, Self
from urllib.parse import quote, quote_plus, urlencode, urlsplit

import httpx
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    ValidationInfo,
    field_validator,
)
from starlette.datastructures import QueryParams
from starlette.responses import RedirectResponse, Response

from meridian.platform.common.env import (
    BaseHttpUrl,
    HttpUrl,
    SettingsError,
    require_env,
)
from meridian.platform.common.signin import (
    DEFAULT_ROLES_CLAIM,
    DEFAULT_SUBJECT_CLAIM,
    ENVIRONMENT_ENV,
    KIND_ENVIRONMENT,
    SIGNIN_ENV_PREFIX,
    OptionalWord,
    Population,
    Principal,
    Unauthenticated,
    Word,
)
from meridian.platform.common.signinidtoken import IdTokenCheck
from meridian.platform.common.signinkeys import KeySet, refuse_an_event_loop
from meridian.platform.common.signinsession import (
    SessionSettings,
    clear_session_cookie,
    seal_session,
    set_session_cookie,
)
from meridian.platform.common.signinstate import (
    PKCE_METHOD,
    TRANSACTION_SECONDS,
    FlowReason,
    Transaction,
    clean_path,
    open_transaction,
    pkce_challenge,
    safe_return_path,
    seal_transaction,
    transaction_cookie_name,
)
from meridian.platform.common.throttle import RefusalAuditThrottle
from meridian.platform.common.tls import ClientTls, verify_of

logger = logging.getLogger(__name__)

# The same shape as ``signinkeys``: the per-phase timeouts start again at every
# byte, so one deadline covers the whole request; read when the exchange starts,
# so a test can patch it. The connect timeout stays below it, so that the
# deadline never lands inside a TLS handshake (a cancelled handshake leaves its
# socket open until a collection).
EXCHANGE_DEADLINE_SECONDS = 5.0
EXCHANGE_TIMEOUT = httpx.Timeout(3.0, connect=1.0)
# Access, refresh and ID tokens with their claims: a few KB. 64 KB leaves room.
MAX_TOKEN_RESPONSE_BYTES = 64 * 1024
# The longest ``state``, ``code``, ``error`` or ``iss`` read from the callback.
MAX_CALLBACK_VALUE_CHARS = 2048
CALLBACK_NAMES = ("state", "code", "error", "iss")
MAX_RETURN_PREFIXES = 16
MAX_SCOPES = 8
# The payload ``typ`` of the pinned Keycloak's ID token.
DEFAULT_ID_TYP = "ID"
_DEFAULT_PORTS = {"http": 80, "https": 443}
# Variable names that differ from the field's, and fields that no variable sets.
_VARIABLE_OF = {"required_typ": "ID_TOKEN_TYP"}
_NOT_FROM_THE_ENVIRONMENT = ("return_prefixes", "default_return_path")

type Clock = Callable[[], float]


# ── the settings ────────────────────────────────────────────────────────────
def _origin(url: str) -> tuple[str, str, int]:
    parts = urlsplit(url)
    return (
        parts.scheme,
        parts.hostname or "",
        parts.port or _DEFAULT_PORTS[parts.scheme],
    )


Address = Annotated[str, HttpUrl]


class FlowSettings(BaseModel):
    """One population's sign-in at one issuer. Frozen; refuses to build for a
    plain-HTTP address unless the environment is exactly ``kind``, for a return
    address that is not under the app's origin, and for scopes without
    ``openid``. The client's credential is a ``SecretStr``: it is in no ``repr``,
    no ``str`` and no ``model_dump``. As for ``SigninSettings``, ``model_copy``
    and ``model_construct`` skip validation: build through the constructor or
    ``from_env``.

    Two kinds of address. FRONT-channel, reached by the browser: ``issuer``
    (compared with the token's ``iss``), ``authorization_url``,
    ``end_session_url``, and the app's own ``app_origin``, ``redirect_uri`` and
    ``post_logout_redirect_uri``. BACK-channel, reached by a pod: ``token_url``,
    which on kind is the Service's name and is NOT the discovery document's
    ``token_endpoint`` (that is the front address, which a pod cannot call).
    The keys' address is ``SigninSettings.keys_url``, behind the ``KeySet`` the
    flow is given."""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    population: Population
    # Declared before the addresses: their rule reads it.
    environment: str = ""
    issuer: Address
    client_id: Word
    client_credential: Annotated[SecretStr, Field(min_length=1, max_length=512)]
    authorization_url: Address
    token_url: Address
    end_session_url: Address
    app_origin: BaseHttpUrl
    redirect_uri: Address
    post_logout_redirect_uri: Address
    scopes: Annotated[tuple[Word, ...], Field(max_length=MAX_SCOPES)] = ("openid",)
    # The payload ``typ`` of an ID token. Empty for an issuer that sends none
    # (Entra), and then not enforced.
    required_typ: OptionalWord = DEFAULT_ID_TYP
    roles_claim: Word = DEFAULT_ROLES_CLAIM
    subject_claim: Word = DEFAULT_SUBJECT_CLAIM
    # Given by the code that wires the routes, not by the environment: the
    # prefixes of the app's own pages a person may be sent back to.
    return_prefixes: Annotated[tuple[str, ...], Field(max_length=MAX_RETURN_PREFIXES)]
    default_return_path: str = "/"

    @field_validator(
        "issuer",
        "authorization_url",
        "token_url",
        "end_session_url",
        "app_origin",
        "redirect_uri",
        "post_logout_redirect_uri",
    )
    @classmethod
    def _address_rules(cls, value: str, info: ValidationInfo) -> str:
        # Over plain HTTP, whoever is on the path reads the code and the
        # credential, or sends the person elsewhere. The value is left out of
        # the message.
        kind = info.data.get("environment") == KIND_ENVIRONMENT
        if urlsplit(value).scheme != "https" and not kind:
            raise ValueError(
                f"must be an https URL unless the environment is {KIND_ENVIRONMENT}"
            )
        origin = info.data.get("app_origin")
        returns = info.field_name in ("redirect_uri", "post_logout_redirect_uri")
        if returns and origin is not None and _origin(value) != _origin(origin):
            raise ValueError("must be under the app's own origin")
        return value

    @field_validator("scopes")
    @classmethod
    def _openid_among_the_scopes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if "openid" not in value:
            raise ValueError("must include openid")
        return value

    @field_validator("return_prefixes")
    @classmethod
    def _prefixes_are_paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not all(clean_path(prefix) for prefix in value):
            raise ValueError("must each be a path on the app")
        return value

    @field_validator("default_return_path")
    @classmethod
    def _default_is_a_path(cls, value: str) -> str:
        if not clean_path(value):
            raise ValueError("must be a path on the app")
        return value

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str],
        population: Population,
        *,
        return_prefixes: tuple[str, ...],
        default_return_path: str = "/",
    ) -> Self:
        """Read ``MERIDIAN_SIGNIN_<POPULATION>_`` ``ISSUER`` (the one the bearer
        check reads too), ``CLIENT_ID``, ``CLIENT_CREDENTIAL``,
        ``AUTHORIZATION_URL``, ``TOKEN_URL``, ``END_SESSION_URL``, ``APP_ORIGIN``,
        ``REDIRECT_URI``, ``POST_LOGOUT_REDIRECT_URI`` and, optionally, ``SCOPES``
        (comma separated, ``openid`` by default), ``ID_TOKEN_TYP`` (``ID``),
        ``ROLES_CLAIM`` and ``SUBJECT_CLAIM``, and ``MERIDIAN_ENVIRONMENT``.
        Raise ``SettingsError`` naming the variable that is missing or not valid,
        never the value."""
        prefix = f"{SIGNIN_ENV_PREFIX}{population.upper()}_"
        scopes = environ.get(prefix + "SCOPES") or ""
        outcome = cls._built(
            prefix,
            population=population,
            environment=environ.get(ENVIRONMENT_ENV) or "",
            issuer=require_env(environ, prefix + "ISSUER"),
            client_id=require_env(environ, prefix + "CLIENT_ID"),
            client_credential=require_env(environ, prefix + "CLIENT_CREDENTIAL"),
            authorization_url=require_env(environ, prefix + "AUTHORIZATION_URL"),
            token_url=require_env(environ, prefix + "TOKEN_URL"),
            end_session_url=require_env(environ, prefix + "END_SESSION_URL"),
            app_origin=require_env(environ, prefix + "APP_ORIGIN"),
            redirect_uri=require_env(environ, prefix + "REDIRECT_URI"),
            post_logout_redirect_uri=require_env(
                environ, prefix + "POST_LOGOUT_REDIRECT_URI"
            ),
            scopes=tuple(p.strip() for p in scopes.split(","))
            if scopes
            else ("openid",),
            required_typ=environ.get(prefix + "ID_TOKEN_TYP") or DEFAULT_ID_TYP,
            roles_claim=environ.get(prefix + "ROLES_CLAIM") or DEFAULT_ROLES_CLAIM,
            subject_claim=(
                environ.get(prefix + "SUBJECT_CLAIM") or DEFAULT_SUBJECT_CLAIM
            ),
            return_prefixes=return_prefixes,
            default_return_path=default_return_path,
        )
        if isinstance(outcome, str):
            raise SettingsError(outcome)
        return outcome

    @classmethod
    def _built(cls, prefix: str, **values: Any) -> Self | str:
        """The settings, or the text of the first problem, naming the variable.
        The problem is returned and not raised from the ``except``, so that
        pydantic's error, whose data holds the rejected value, is neither the
        cause nor the context of the error that reaches the caller."""
        try:
            return cls(**values)
        except ValidationError as error:
            first = error.errors()[0]
            name = str(first["loc"][0]) if first["loc"] else "settings"
            if name in _NOT_FROM_THE_ENVIRONMENT:
                return f"{name} is not valid: {first['msg']}"
            variable = prefix + _VARIABLE_OF.get(name, name.upper())
            return f"{variable} is not valid: {first['msg']}"


def discovery_problem(
    document: Mapping[str, Any], settings: FlowSettings
) -> str | None:
    """Why the issuer's discovery document does not match the settings, or None.
    The module never fetches the document and takes no address from it: this is
    for a test, and for an operator who has fetched it. A fixed text, never a
    value. It holds the issuer, the two front endpoints and ``S256`` against
    what was set."""
    if not isinstance(document, Mapping):
        return "the discovery document is not an object"
    if document.get("issuer") != settings.issuer:
        return "the discovery issuer is not the pinned issuer"
    if document.get("authorization_endpoint") != settings.authorization_url:
        return "the discovery authorization endpoint is not the setting"
    if document.get("end_session_endpoint") != settings.end_session_url:
        return "the discovery end-session endpoint is not the setting"
    methods = document.get("code_challenge_methods_supported")
    if not isinstance(methods, list) or PKCE_METHOD not in methods:
        return f"the issuer does not offer PKCE {PKCE_METHOD}"
    return None


# ── the exchange's failures, and its answer ─────────────────────────────────
class _ExchangeRefused(Exception):
    """The token endpoint answered something that is not a usable 200."""


class _ExchangeTooLarge(Exception):
    """More than ``MAX_TOKEN_RESPONSE_BYTES`` came."""


class _ExchangeDeadline(Exception):
    """The exchange was not finished ``EXCHANGE_DEADLINE_SECONDS`` after it began."""


class _ExchangeFailure:
    """Where ``_exchange`` leaves the class of the exception behind an
    ``exchange-failed`` for the one log line: a class name and nothing else."""

    __slots__ = ("class_name",)

    def __init__(self) -> None:
        self.class_name = ""


def _id_token_of(body: bytes) -> str | FlowReason:
    """The ``id_token`` of a token response, and nothing else of it."""
    try:
        document = json.loads(body)
    except (ValueError, RecursionError):
        return FlowReason.EXCHANGE_MALFORMED
    if not isinstance(document, dict):
        return FlowReason.EXCHANGE_MALFORMED
    token = document.get("id_token")
    if not isinstance(token, str) or not token:
        return FlowReason.ID_MISSING
    return token


# ── the outcome ─────────────────────────────────────────────────────────────
class SigninOutcome:
    """What a callback came to. ``signed_in`` says whether the person is; if not,
    ``reason`` is why (for a log line, never for the person) and the page shows
    one generic failure with a link to start again. ``return_to`` is a path of
    this app: where the person was going, or the default page after a refusal.

    ``apply_to(response)`` puts the cookies on the response the caller builds:
    the session cookie when signed in, and, always, the cleared transaction. It
    is the only way to the cookies, so the transaction cannot be left behind."""

    __slots__ = ("_apply", "reason", "return_to")

    def __init__(
        self,
        reason: FlowReason | None,
        return_to: str,
        apply: Callable[[Response], None],
    ) -> None:
        self.reason = reason
        self.return_to = return_to
        self._apply = apply

    @property
    def signed_in(self) -> bool:
        return self.reason is None

    def apply_to(self, response: Response) -> None:
        self._apply(response)
        response.headers["Cache-Control"] = "no-store"

    def __repr__(self) -> str:
        return f"SigninOutcome(reason={self.reason!r})"


class SigninFlow:
    """The sign-in of one population on one app. ``keys`` is the issuer's key set
    (the one the bearer check uses, if there is one); ``tls`` is the CA the token
    endpoint is trusted by when its address is ``https``; ``transport`` is for a
    test; ``clock`` times the log throttle. Refuses to build when the session
    cookie's ``Secure`` does not agree with the app's origin: a ``Secure`` cookie
    on plain HTTP never comes back, and a cookie that is not ``Secure`` on HTTPS
    is a downgrade."""

    def __init__(
        self,
        settings: FlowSettings,
        sessions: SessionSettings,
        keys: KeySet,
        *,
        tls: ClientTls | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Clock = time.monotonic,
    ) -> None:
        https = urlsplit(settings.app_origin).scheme == "https"
        if sessions.secure != https:
            raise SettingsError(
                "the session cookie must be Secure exactly when the app is served "
                "over https"
            )
        self.settings = settings
        self._sessions = sessions
        self._id_token = IdTokenCheck(
            keys,
            population=settings.population,
            issuer=settings.issuer,
            client_id=settings.client_id,
            required_typ=settings.required_typ,
            subject_claim=settings.subject_claim,
            roles_claim=settings.roles_claim,
        )
        self._verify = verify_of(tls)
        self._transport = transport
        self._throttle = RefusalAuditThrottle(clock)
        self._callback_path = urlsplit(settings.redirect_uri).path
        credential = settings.client_credential.get_secret_value()
        basic = f"{quote_plus(settings.client_id)}:{quote_plus(credential)}"
        self._authorization = "Basic " + base64.b64encode(basic.encode()).decode()

    # ── start ───────────────────────────────────────────────────────────────
    def start(self, return_to: str | None, now: float) -> RedirectResponse:
        """The redirect to the issuer, with the transaction cookie set.
        ``return_to`` is a path of this app under an allowed prefix, else the
        default page."""
        settings = self.settings
        transaction = Transaction(
            state=secrets.token_urlsafe(32),
            nonce=secrets.token_urlsafe(32),
            verifier=secrets.token_urlsafe(48),
            return_to=safe_return_path(
                return_to, settings.return_prefixes, settings.default_return_path
            ),
            population=settings.population,
            expires_at=math.floor(now) + TRANSACTION_SECONDS,
        )
        query = urlencode(
            {
                "response_type": "code",
                "client_id": settings.client_id,
                "redirect_uri": settings.redirect_uri,
                "scope": " ".join(settings.scopes),
                "state": transaction.state,
                "nonce": transaction.nonce,
                "code_challenge": pkce_challenge(transaction.verifier),
                "code_challenge_method": PKCE_METHOD,
            },
            quote_via=quote,
        )
        response = RedirectResponse(
            f"{settings.authorization_url}?{query}",
            status_code=303,
            headers={"Cache-Control": "no-store"},
        )
        response.set_cookie(
            transaction_cookie_name(settings.population),
            seal_transaction(transaction, self._sessions.keys),
            max_age=TRANSACTION_SECONDS,
            **self._transaction_attributes(),
        )
        return response

    def _transaction_attributes(self) -> dict[str, Any]:
        return {
            "httponly": True,
            "samesite": "lax",
            "path": self._callback_path,
            "secure": self._sessions.secure,
        }

    def clear_transaction(self, response: Response) -> None:
        """Ask the browser to forget the transaction cookie, with the attributes
        it was set with. For a caller that answers a callback itself after
        ``finish`` (``SigninOutcome.apply_to`` does it for the outcome)."""
        response.delete_cookie(
            transaction_cookie_name(self.settings.population),
            **self._transaction_attributes(),
        )

    # ── finish ──────────────────────────────────────────────────────────────
    def finish(
        self, query: QueryParams, cookie: str | None, now: float
    ) -> SigninOutcome:
        """The outcome of a callback: ``query`` is the callback's query string,
        ``cookie`` the transaction cookie's value (None when the browser sent
        none) and ``now`` epoch seconds. Blocks (the code exchange and a key
        fetch): call it from a plain ``def`` route. Raise ``CalledFromEventLoop``
        in a thread that runs an event loop, before anything is read; that is
        the caller's mistake and is not a refusal."""
        refuse_an_event_loop()
        failure = _ExchangeFailure()
        outcome = self._principal_or_reason(query, cookie, now, failure)
        if isinstance(outcome, FlowReason):
            self._say(outcome, failure.class_name)
            return SigninOutcome(
                outcome, self.settings.default_return_path, self.clear_transaction
            )
        principal, return_to = outcome

        def apply(response: Response) -> None:
            set_session_cookie(response, principal, self._sessions, now)
            self.clear_transaction(response)

        return SigninOutcome(None, return_to, apply)

    def _principal_or_reason(
        self,
        query: QueryParams,
        cookie: str | None,
        now: float,
        failure: _ExchangeFailure,
    ) -> tuple[Principal, str] | FlowReason:
        settings = self.settings
        if cookie is None:
            return FlowReason.NO_TRANSACTION
        transaction = open_transaction(
            cookie, self._sessions.keys, now, settings.population
        )
        if isinstance(transaction, FlowReason):
            return transaction
        problem = self._callback_problem(query, transaction)
        if problem is not None:
            return problem
        id_token = self._exchange(query["code"], transaction.verifier, failure)
        if isinstance(id_token, FlowReason):
            return id_token
        principal = self._id_token.principal_or_reason(id_token, transaction.nonce, now)
        if isinstance(principal, FlowReason):
            return principal
        problem = self._session_problem(principal, now)
        if problem is not None:
            return problem
        return_to = safe_return_path(
            transaction.return_to,
            settings.return_prefixes,
            settings.default_return_path,
        )
        return principal, return_to

    def _callback_problem(
        self, query: QueryParams, transaction: Transaction
    ) -> FlowReason | None:
        """Why the query is not the answer to this transaction, or None."""
        seen: set[str] = set()
        for name, value in query.multi_items():
            if name not in CALLBACK_NAMES:
                continue
            if name in seen or len(value) > MAX_CALLBACK_VALUE_CHARS:
                return FlowReason.BAD_CALLBACK
            seen.add(name)
        given = query.get("state")
        if given is None or not hmac.compare_digest(
            given.encode(), transaction.state.encode()
        ):
            return FlowReason.STATE_MISMATCH
        if "error" in query:  # its description is never read
            return FlowReason.ISSUER_ERROR
        if "iss" in query and query["iss"] != self.settings.issuer:
            return FlowReason.ISSUER_PARAMETER
        if not query.get("code"):
            return FlowReason.NO_CODE
        return None

    # ── the code exchange ───────────────────────────────────────────────────
    def _exchange(
        self, code: str, verifier: str, cause: _ExchangeFailure
    ) -> str | FlowReason:
        """The ID token the issuer gives for the code, or why not. The failure is
        a reason and not an exception, and the exception is not chained. For
        ``exchange-failed`` the exception's class (never its text) goes to
        ``cause``: the one word covers a certificate error, a refused connection
        and a bad answer, and the log line tells them apart by it."""
        failure: FlowReason | None = None
        body = b""
        try:
            body = asyncio.run(self._exchange_within_deadline(code, verifier))
        except _ExchangeTooLarge:
            failure = FlowReason.EXCHANGE_TOO_LARGE
        except (_ExchangeDeadline, httpx.TimeoutException):
            failure = FlowReason.EXCHANGE_DEADLINE
        except Exception as error:  # any other failure of the exchange
            failure = FlowReason.EXCHANGE_FAILED
            cause.class_name = type(error).__name__
        return failure if failure is not None else _id_token_of(body)

    async def _exchange_within_deadline(self, code: str, verifier: str) -> bytes:
        try:
            async with asyncio.timeout(EXCHANGE_DEADLINE_SECONDS) as deadline:
                return await self._post(code, verifier)
        except TimeoutError:
            if deadline.expired():  # ours, and not one a library raised
                raise _ExchangeDeadline from None
            raise

    async def _post(self, code: str, verifier: str) -> bytes:
        settings = self.settings
        form = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": settings.redirect_uri,
            "code_verifier": verifier,
        }
        headers = {
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "Authorization": self._authorization,
        }
        body = bytearray()
        client = httpx.AsyncClient(
            verify=self._verify,
            trust_env=False,
            follow_redirects=False,
            timeout=EXCHANGE_TIMEOUT,
            transport=self._transport,
        )
        async with (
            client,
            client.stream(
                "POST", settings.token_url, data=form, headers=headers
            ) as response,
        ):
            if response.status_code != httpx.codes.OK:
                raise _ExchangeRefused
            encoding = response.headers.get("content-encoding", "identity")
            if encoding.strip().lower() != "identity":
                raise _ExchangeRefused
            # RAW: nothing is decoded, so the bytes counted are the bytes received.
            async for chunk in response.aiter_raw():
                if len(body) + len(chunk) > MAX_TOKEN_RESPONSE_BYTES:
                    raise _ExchangeTooLarge
                body.extend(chunk)
        return bytes(body)

    # ── the session ─────────────────────────────────────────────────────────
    def _session_problem(self, principal: Principal, now: float) -> FlowReason | None:
        """Seal the session as it will be sealed, so that a principal that cannot
        be one is a refusal here and not an error at the response."""
        try:
            seal_session(principal, self._sessions.keys, now)
        except ValueError:
            return FlowReason.SESSION_TOO_LARGE
        except Unauthenticated:
            return FlowReason.ID_EXPIRED
        return None

    def _say(self, reason: FlowReason, class_name: str = "") -> None:
        """One line per reason per window, with the count the window held back.
        ``class_name`` is an exception's class, when there is one: never its
        text."""
        carried = self._throttle.due(None, reason.value)
        if carried is None:
            return
        extra = f" (and {carried} more since the last line)" if carried else ""
        word = f"{reason.value} ({class_name})" if class_name else reason.value
        logger.warning("sign-in callback refused: %s" + extra, word)

    # ── sign out ────────────────────────────────────────────────────────────
    def sign_out(self) -> RedirectResponse:
        """The redirect to the issuer's end-session address, with the session
        cookie (and a half-made sign-in's transaction) cleared. No token is kept,
        so no ``id_token_hint`` is sent; the module's text says what is not known
        of the issuer's answer."""
        settings = self.settings
        query = urlencode(
            {
                "client_id": settings.client_id,
                "post_logout_redirect_uri": settings.post_logout_redirect_uri,
            },
            quote_via=quote,
        )
        response = RedirectResponse(
            f"{settings.end_session_url}?{query}",
            status_code=303,
            headers={"Cache-Control": "no-store"},
        )
        clear_session_cookie(response, settings.population, self._sessions)
        self.clear_transaction(response)
        return response
