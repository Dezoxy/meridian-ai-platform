"""What the adjuster's and the claimant's pages put on every response, and the
cross-site check of their posts (S016, T-70), moved out of ``adjuster.py`` with no
change of behaviour (S070 F4d: that module was over the size ceiling).

``SecurityHeadersMiddleware`` adds the pages' headers to every response under
``/adjuster/`` and ``/claimant/``. ``is_cross_site`` and ``require_same_origin``
decide whether a request came from another site; the adjuster module registers
the handler that answers ``CrossSiteRefused`` with the pages' 403.
"""

from urllib.parse import urlsplit

from fastapi import Request
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

PAGES_PREFIX = "/adjuster/"
CLAIMANT_PREFIX = "/claimant/"
# The sign-in's own routes (``staff_signin``); a prefix of the headers only
# while the sign-in is on.
AUTH_PREFIX = "/auth/"
STYLESHEET_PATH = "/adjuster/static/adjuster.css"
CROSS_SITE_DETAIL = "the request came from another site"
HTTP_OK = 200
# The only ``Sec-Fetch-Site`` values that pass: ``same-origin`` (the page's own
# post, or a click on one of its links) and ``none`` (the user typed the address).
# Any other is another site, or a value this code does not know (T-70).
OWN_FETCHES = ("same-origin", "none")
# The stylesheet holds no data, so it may be cached; every other response under
# ``/adjuster/`` may hold a claim and is not stored.
STYLESHEET_CACHE_CONTROL = "max-age=3600"
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'self'; form-action 'self'; "
        "frame-ancestors 'none'; base-uri 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    # The Referer holds the claim's ID, so it goes only to this app. A post of
    # the page itself then carries a real Origin in a browser without Fetch
    # Metadata (``no-referrer`` makes Chrome send ``Origin: null``).
    "Referrer-Policy": "same-origin",
    # A page of another site cannot load a response of ``/adjuster/`` or
    # ``/claimant/``, not even with ``no-cors``.
    "Cross-Origin-Resource-Policy": "same-origin",
    "Cache-Control": "no-store",
}
# The one response of the pages that carries a Content-Security-Policy of its own:
# the adjuster's file download (``file_download``), which sandboxes the bytes it
# serves. The route sets this key in the request's scope after it has built that
# response, and only then does the middleware below keep the policy the response
# already holds; every other response under the pages' prefixes, an error page of
# the download's own path included, gets the pages' policy.
OWN_POLICY_SCOPE_KEY = "meridian.own_response_policy"
POLICY_HEADER = "Content-Security-Policy"


class CrossSiteRefused(Exception):
    """The post came from another site (T-70)."""


def is_cross_site(origin: str | None, host: str | None, fetch_site: str | None) -> bool:
    """Whether a post is one that another site made (T-70).

    ``Sec-Fetch-Site``, when present, decides alone: ``same-origin`` and
    ``none`` pass; ``cross-site``, ``same-site`` and any value not known are
    refused. A page cannot set it (it is a forbidden header name, the browser
    sets it), so it is trusted over ``Origin``: a browser that sends it sends
    ``Origin: null`` for the page's own post under some referrer policies.

    Only when it is absent (an older browser, curl, a test) does ``Origin``
    count: ``null``, one that is not a URL, and one whose host and port are not
    the request's ``Host`` are refused (a browser without Fetch Metadata sends
    a real ``Origin`` under the page's ``same-origin`` referrer policy).
    Netlocs are compared, not schemes (the edge may end TLS), and not case. A
    request with none of the headers passes, as it does on the JSON route
    (T-69).
    """
    if fetch_site is not None:
        return fetch_site.lower() not in OWN_FETCHES
    if origin is None:
        return False
    if host is None:
        return True
    try:
        netloc = urlsplit(origin).netloc
    except ValueError:
        return True
    # ``null`` and a bare word have no netloc and can never equal a Host.
    return netloc.lower() != host.lower()


def require_same_origin(request: Request) -> None:
    headers = request.headers
    if is_cross_site(
        headers.get("origin"), headers.get("host"), headers.get("sec-fetch-site")
    ):
        raise CrossSiteRefused


class SecurityHeadersMiddleware:
    """Add the pages' headers to every response under ``/adjuster/`` and
    ``/claimant/``: a 404, a 405, a 413 and a 422 as well as a page. They replace
    a header the response set, with one exception: the policy of a response whose
    route marked the request (``OWN_POLICY_SCOPE_KEY``, the download). The JSON
    routes are not touched.

    ``issuer_origin`` is given only when the staff sign-in is on (S021, Y4), and
    changes two things. ``/auth/`` joins the prefixes, with ``Referrer-Policy:
    no-referrer``: the callback's address holds the code and the state, and no
    page it leads to may learn it. And the policy of the adjuster's pages and
    of ``/auth/`` names the issuer's origin in ``form-action`` too, because the
    sign-out form posts to this app and is redirected to the issuer's
    end-session address, and Chromium checks a form's redirects against
    ``form-action``. Without it the headers are exactly ``SECURITY_HEADERS``."""

    def __init__(self, app: ASGIApp, issuer_origin: str | None = None) -> None:
        self.app = app
        self.prefixes: tuple[str, ...] = (PAGES_PREFIX, CLAIMANT_PREFIX)
        self.signin_headers: dict[str, str] | None = None
        if issuer_origin is not None:
            self.prefixes += (AUTH_PREFIX,)
            self.signin_headers = SECURITY_HEADERS | {
                POLICY_HEADER: SECURITY_HEADERS[POLICY_HEADER].replace(
                    "form-action 'self';", f"form-action 'self' {issuer_origin};"
                ),
            }

    def _headers_for(self, path: str) -> dict[str, str]:
        if self.signin_headers is None or path.startswith(CLAIMANT_PREFIX):
            return SECURITY_HEADERS
        if path.startswith(AUTH_PREFIX):
            return self.signin_headers | {"Referrer-Policy": "no-referrer"}
        return self.signin_headers

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope["path"].startswith(self.prefixes):
            await self.app(scope, receive, send)
            return
        wanted = self._headers_for(scope["path"])

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in wanted.items():
                    if name == POLICY_HEADER and scope.get(OWN_POLICY_SCOPE_KEY):
                        continue
                    headers[name] = value
                if scope["path"] == STYLESHEET_PATH and message["status"] == HTTP_OK:
                    headers["Cache-Control"] = STYLESHEET_CACHE_CONTROL
            await send(message)

        await self.app(scope, receive, send_with_headers)
