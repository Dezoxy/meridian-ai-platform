"""`make deploy` compares the ACL the Secret holds, not only its annotation (S066).

The third infra review found that ``require_rate_store_secret`` compared the
annotation ``up.sh`` writes on the Secret (the hash of the ACL's rules) and nothing
of the file itself: a ``users.acl`` edited or replaced after ``make up`` with the
annotation left alone passed. Now ``deploy.sh`` also decodes the Secret's own
``users.acl`` in the same ``jq`` call, hashes it masked, and compares that with what
``make up`` would write today. Three cases: an old annotation is refused (as before),
a matching annotation over edited data is refused (new), both matching pass. The
text of the Secret goes through a pipe into the hash and nowhere else: not printed,
not traced and not kept in a variable. Nothing touches a cluster: the function runs
in bash against a stub ``kctl``, as in ``test_kind_rate_store_secret.py``.
"""

import base64
import json
import re
import subprocess
from pathlib import Path

import pytest
from test_kind_rate_store_secret import (
    ANNOTATION,
    COMMON_SH,
    DEPLOY_SH,
    PASSWORD_HASH,
    SECRET_NAME,
    bash,
    constants_of,
    function_text,
    run_require,
    secret_as_made,
)


def acl_of(secret: dict) -> str:
    return base64.b64decode(secret["data"]["users.acl"]).decode()


def with_acl(secret: dict, text: str) -> dict:
    """The same Secret, annotation untouched, holding ``text`` as its ACL file."""
    return {
        "metadata": secret["metadata"],
        "data": {
            **secret["data"],
            "users.acl": base64.b64encode(text.encode()).decode(),
        },
    }


# ── the three cases ──────────────────────────────────────────────────────────


def test_deploy_goes_on_when_the_annotation_and_the_acl_data_are_both_current(
    tmp_path: Path,
) -> None:
    done = run_require(tmp_path, answer=secret_as_made(tmp_path))

    assert done.returncode == 0, done.stderr
    assert done.stdout + done.stderr == ""


@pytest.mark.parametrize(
    "edit",
    [
        lambda acl: acl.replace(" +hello", " +hello +keys"),
        lambda acl: acl.replace("~meridian:rate:*", "~*"),
        lambda acl: acl + "user extra on nopass ~* +@all\n",
        lambda acl: acl.replace("user probe on nopass", "user probe on #" + "0" * 64),
        lambda acl: "user default on nopass ~* +@all\n",
    ],
    ids=[
        "a-command",
        "the-key-pattern",
        "another-user",
        "a-password-for-probe",
        "replaced",
    ],
)
def test_deploy_refuses_acl_data_that_was_edited_under_an_annotation_that_still_matches(
    tmp_path: Path, edit
) -> None:
    secret = secret_as_made(tmp_path)
    edited = with_acl(secret, edit(acl_of(secret)))

    done = run_require(tmp_path, answer=edited)

    assert edited["metadata"] == secret["metadata"]
    assert acl_of(edited) != acl_of(secret)
    assert done.returncode == 1
    assert f"Secret {SECRET_NAME}" in done.stderr
    assert "users.acl" in done.stderr
    assert ANNOTATION in done.stderr
    assert "edited or replaced" in done.stderr
    # What to do, in the runbook's order, as for the annotation's refusal.
    flat = done.stderr
    assert flat.index("delete secret") < flat.index("run 'make up'")
    assert flat.index("run 'make up'") < flat.index("restart the rate store")
    assert flat.index("restart the rate store") < flat.index("then the Model Gateway")


def test_deploy_refuses_an_old_annotation_and_says_so_whatever_the_data_holds(
    tmp_path: Path,
) -> None:
    secret = secret_as_made(tmp_path)
    secret["metadata"]["annotations"][ANNOTATION] = "0" * 64

    done = run_require(tmp_path, answer=secret)

    assert done.returncode == 1
    assert "its annotation" in done.stderr
    assert "differs" in done.stderr
    assert "edited or replaced" not in done.stderr


def test_the_old_annotation_over_edited_data_is_the_annotations_refusal(
    tmp_path: Path,
) -> None:
    secret = secret_as_made(tmp_path)
    secret["metadata"]["annotations"][ANNOTATION] = "0" * 64
    edited = with_acl(secret, acl_of(secret) + "user extra on nopass ~* +@all\n")

    done = run_require(tmp_path, answer=edited)

    assert done.returncode == 1
    assert "differs" in done.stderr


def test_a_password_hash_that_differs_is_not_an_edit(tmp_path: Path) -> None:
    secret = secret_as_made(tmp_path)
    other = PASSWORD_HASH.sub("#" + "a" * 64, acl_of(secret))

    done = run_require(tmp_path, answer=with_acl(secret, other))

    assert acl_of(secret) != other
    assert done.returncode == 0, done.stderr


MALFORMED_MARK = "ZZmarkZZ"


def test_acl_data_that_is_not_base64_is_refused_with_a_sentence_of_its_own(
    tmp_path: Path,
) -> None:
    secret = secret_as_made(tmp_path)
    broken = {
        "metadata": secret["metadata"],
        "data": {**secret["data"], "users.acl": f"{MALFORMED_MARK} %%% not base64"},
    }

    done = run_require(tmp_path, answer=broken)

    assert done.returncode == 1
    assert "could not read" in done.stderr
    assert "users.acl" in done.stderr


def test_acl_data_that_is_not_base64_is_not_quoted_in_any_output(
    tmp_path: Path,
) -> None:
    secret = secret_as_made(tmp_path)
    broken = {
        "metadata": secret["metadata"],
        "data": {**secret["data"], "users.acl": f"{MALFORMED_MARK} %%% not base64"},
    }

    done = run_require(tmp_path, answer=broken)

    # jq's own error text quotes the first characters of a value it cannot
    # decode: the script's sentence says what failed, and jq's goes nowhere.
    assert done.returncode == 1
    assert MALFORMED_MARK not in done.stdout + done.stderr
    assert "%%%" not in done.stdout + done.stderr


# ── nothing of the Secret is shown ───────────────────────────────────────────


def test_a_refusal_prints_nothing_of_the_acl_the_secret_holds(tmp_path: Path) -> None:
    secret = secret_as_made(tmp_path)
    acl = acl_of(secret)
    edited = with_acl(secret, acl.replace(" +hello", " +hello +keys"))

    done = run_require(tmp_path, answer=edited)

    shown = done.stdout + done.stderr
    assert done.returncode == 1
    for held in (
        "+keys",
        "user gateway",
        "user default",
        edited["data"]["users.acl"],
        *PASSWORD_HASH.findall(acl),
        base64.b64decode(edited["data"]["uri"]).decode(),
    ):
        assert held not in shown, held


def traced_run(tmp_path: Path, answer: dict) -> subprocess.CompletedProcess[str]:
    """``require_rate_store_secret`` under ``set -x``, as ``bash -x deploy.sh`` would
    run it; it prints whether tracing is on again when the function returns."""
    script = "\n".join(
        [
            "set -euo pipefail",
            f'. "{COMMON_SH}"',
            "die() { printf 'error: %s\\n' \"$*\" >&2; exit 1; }",
            "NAMESPACE=meridian",
            *constants_of(DEPLOY_SH),
            "kctl() {",
            '  case "$*" in',
            f'    *"get secret {SECRET_NAME} -o json"*)'
            f" printf '%s' '{json.dumps(answer)}' ;;",
            '    *) echo "unexpected kctl $*" >&2; return 1 ;;',
            "  esac",
            "}",
            function_text(DEPLOY_SH, "rate_store_expected_acl_hash"),
            function_text(DEPLOY_SH, "require_rate_store_secret"),
            "set -x",
            "require_rate_store_secret",
            'case "$-" in *x*) echo "tracing: on" ;; *) echo "tracing: off" ;; esac',
        ]
    )
    return bash(script, tmp_path)


def test_a_traced_run_shows_none_of_the_secret_and_tracing_is_on_again_after(
    tmp_path: Path,
) -> None:
    secret = secret_as_made(tmp_path)

    done = traced_run(tmp_path, secret)

    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "tracing: on"
    for held in (
        secret["data"]["users.acl"],
        secret["data"]["uri"],
        acl_of(secret),
        "user gateway",
        *PASSWORD_HASH.findall(acl_of(secret)),
    ):
        assert held not in done.stderr, held


def test_a_traced_refusal_shows_none_of_the_secret_either(tmp_path: Path) -> None:
    secret = secret_as_made(tmp_path)
    edited = with_acl(secret, acl_of(secret) + "user extra on nopass ~* +@all\n")

    done = traced_run(tmp_path, edited)

    assert done.returncode == 1
    for held in (
        edited["data"]["users.acl"],
        edited["data"]["uri"],
        "user extra",
        *PASSWORD_HASH.findall(acl_of(secret)),
    ):
        assert held not in done.stderr, held


def test_the_decoded_acl_goes_into_the_hash_through_a_pipe_and_is_kept_nowhere() -> (
    None
):
    function = function_text(DEPLOY_SH, "require_rate_store_secret")

    # One jq reads the key and decodes it, and its output is the hash's input:
    # no assignment holds the text, and nothing echoes it. `-j` prints no newline
    # of jq's own, which the hash would see (the file's own ends in one).
    assert re.search(
        r'jq -j [^\n]*users\.acl[^\n]*@base64d[^\n]*<<<"\$\{secret\}" 2>/dev/null \| '
        r"rate_store_acl_rules_hash",
        function,
    ), function
    assert not re.search(r"\bacl=", function)
    assert not re.search(r"\b(echo|printf)\b[^\n]*(acl|base64)", function)
    # The Secret's own JSON is not kept past the reads that need it.
    assert re.search(r'^\s*secret=""', function, re.MULTILINE)
