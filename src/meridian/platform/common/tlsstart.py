"""Start a service that serves TLS, reading its certificate once (S069, R11).

``python -m meridian.platform.common.tlsstart`` takes the words the chart used to
give ``uvicorn``, spelled the same (``--factory <module:attr>``, ``--host``,
``--port``, ``--ssl-certfile``, ``--ssl-keyfile``, ``--ssl-ca-certs``,
``--ssl-cert-reqs``, ``--http``, ``--ws``), and calls ``uvicorn.run`` with them
and one more thing, an ``ssl_context_factory``. The server is the one uvicorn's
own command line would have started: the context is uvicorn's own default
builder's, and a test compares the ``Config`` and the context with uvicorn's.

Why it exists. uvicorn read the certificate file to build its TLS context, and
the app factory read it again, later, so that ``/healthz`` knew which
certificate to watch. A renewal that landed between the two left the health
check on a newer certificate than the one served: green until the newer one
neared its end, while the served one expired first. Here the file is read once
for both, in this order: the certificate's bytes, uvicorn's default builder
(which loads the chain from the path), the bytes again. Equal bytes mean the
context holds that certificate, and ``certlife`` is handed the certificate
parsed from those same bytes. Unequal bytes load again, at most ``ATTEMPTS``
times in all; so does an ``ssl.SSLError`` from the builder when the bytes
changed during that load (a renewal between OpenSSL's two opens, the
certificate and the key, shows as a key that does not match).

The premise is Kubernetes', not Python's, and holds as far as this: a Secret
volume moves its data link forward atomically, certificate and key together, so
two equal reads around the load mean the context holds that certificate, UNLESS
the Secret was set back to an earlier version during the load (cert-manager does
not do that). ``ca.crt`` is not inside the bracket: a change of the CA needs a
restart, as it did before. A mount that is not a kubelet Secret volume is
outside the premise.

Nothing is copied: no temporary file, and the key file is never read by this
module (uvicorn's builder reads it, from its mount). Only the certificate's
bytes are held, and they reach no log line.

It fails closed. A file that cannot be read or built into a context, bytes that
never settle, a restart share that is not a fraction and a command line that
is not the one above end the process before it listens, with one line on
standard error that names the flag or the variable and the error's class (for
an ``ssl.SSLError`` also OpenSSL's fixed reason, such as ``KEY_VALUES_MISMATCH``),
never a path's content or a certificate's bytes, and exit status 3 (uvicorn's
own status for a server that did not start). It serves mutual TLS, so a start
without ``--ssl-certfile``, ``--ssl-keyfile`` or ``--ssl-ca-certs``, or with an
``--ssl-cert-reqs`` that is not 1 (optional) or 2 (required), is refused: this
module serves TLS that asks for a client certificate, or nothing. What an app
factory raises, and any error in the builder that is not a bad file (an
``OSError``, ``ssl.SSLError`` or ``ValueError``), is not caught here: it leaves
as it did under ``uvicorn``, a traceback and exit status 1.
"""

import argparse
import os
import re
import ssl
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import NoReturn

import uvicorn

from meridian.platform.common.certlife import hand_over_certificate
from meridian.platform.common.env import SettingsError

PROGRAM = "tlsstart"
# uvicorn's own status for a server that did not start.
STARTUP_FAILURE = 3
# Loads of the context in all, the first included, before the start gives up.
ATTEMPTS = 5
# OpenSSL's reasons are fixed tokens such as KEY_VALUES_MISMATCH.
_TOKEN = re.compile(r"[A-Z0-9_]+")


class StartFailure(Exception):
    """A start that must not go on. The message is written to standard error as
    it is: it names a flag or a variable and an error's class, never a value."""


class _Parser(argparse.ArgumentParser):
    """The parser of the words above, which fails as ``StartFailure`` does."""

    def error(self, message: str) -> NoReturn:
        # argparse's text can quote a word of the command line; none is carried.
        raise StartFailure("the command line is not one this start takes")


def parse_arguments(argv: Sequence[str] | None) -> argparse.Namespace:
    """The words after ``python -m`` and the module's name. Raise
    ``StartFailure`` for a word the start does not take (an abbreviated flag
    included), a missing ``--ssl-certfile``, ``--ssl-keyfile`` or
    ``--ssl-ca-certs``, and an ``--ssl-cert-reqs`` that is missing or is not 1
    (a client certificate optional) or 2 (required): the five services serve
    mutual TLS, so a start that would not ask for one is refused."""
    parser = _Parser(prog=PROGRAM, add_help=False, allow_abbrev=False)
    parser.add_argument("app")
    parser.add_argument("--factory", action="store_true")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--ssl-certfile", required=True)
    parser.add_argument("--ssl-keyfile", required=True)
    parser.add_argument("--ssl-ca-certs", required=True)
    parser.add_argument(
        "--ssl-cert-reqs",
        type=int,
        required=True,
        choices=(int(ssl.CERT_OPTIONAL), int(ssl.CERT_REQUIRED)),
    )
    parser.add_argument("--http", default="auto")
    parser.add_argument("--ws", default="auto")
    return parser.parse_args(argv)


def serve_one_certificate(
    config: uvicorn.Config, default: Callable[[], ssl.SSLContext]
) -> ssl.SSLContext:
    """uvicorn's ``ssl_context_factory``: its own default context, loaded from
    certificate bytes that read the same before and after, and the certificate
    parsed from those bytes handed to ``certlife``. Raise ``StartFailure``."""
    path = Path(str(config.ssl_certfile))
    for _ in range(ATTEMPTS):
        before = _read(path)
        try:
            context = default()
        except (OSError, ssl.SSLError, ValueError) as error:
            # A renewal between OpenSSL's two opens (the certificate, then the
            # key) shows as a key that does not match: when the certificate's
            # bytes changed meanwhile, that is a load to repeat, not a bad file.
            if isinstance(error, ssl.SSLError) and _read(path) != before:
                continue
            raise _cannot_build(error) from None
        if before == _read(path):
            return _hand_over(before, path, context)
    raise StartFailure(
        f"--ssl-certfile changed during each of {ATTEMPTS} loads; not started"
    )


def _cannot_build(error: Exception) -> StartFailure:
    """The refusal for a builder that raised: the error's class, and for an
    ``ssl.SSLError`` OpenSSL's own reason when it is a fixed token (capitals,
    digits and underscores: ``KEY_VALUES_MISMATCH``), never its text."""
    kind = type(error).__name__
    reason = getattr(error, "reason", None) if isinstance(error, ssl.SSLError) else None
    if isinstance(reason, str) and _TOKEN.fullmatch(reason):
        kind = f"{kind}: {reason}"
    return StartFailure(
        "the TLS context cannot be built from --ssl-certfile, "
        f"--ssl-keyfile and --ssl-ca-certs ({kind})"
    )


def _read(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as error:
        raise StartFailure(
            f"--ssl-certfile cannot be read ({type(error).__name__})"
        ) from None


def _hand_over(data: bytes, path: Path, context: ssl.SSLContext) -> ssl.SSLContext:
    try:
        hand_over_certificate(data, path, os.environ)
    except SettingsError as error:
        # The text names a variable and carries no value or path.
        raise StartFailure(f"{error} ({type(error).__name__})") from None
    return context


def main(argv: Sequence[str] | None = None) -> None:
    """Start the service ``argv`` describes; exit 3 with one line when the start
    cannot be made safely."""
    try:
        arguments = parse_arguments(argv)
        uvicorn.run(
            arguments.app,
            factory=arguments.factory,
            host=arguments.host,
            port=arguments.port,
            ssl_certfile=arguments.ssl_certfile,
            ssl_keyfile=arguments.ssl_keyfile,
            ssl_ca_certs=arguments.ssl_ca_certs,
            ssl_cert_reqs=arguments.ssl_cert_reqs,
            http=arguments.http,
            ws=arguments.ws,
            ssl_context_factory=serve_one_certificate,
        )
    except StartFailure as failure:
        print(f"{PROGRAM}: {failure}", file=sys.stderr)
        sys.exit(STARTUP_FAILURE)


if __name__ == "__main__":
    main()
