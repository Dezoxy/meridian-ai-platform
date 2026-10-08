"""Smoke's adjuster-pages check with the staff sign-in on (S021, Y4b, L5).

With ``MERIDIAN_SIGNIN=staff`` the queue's line of ``check_adjuster_pages``
(``infra/kind/smoke.d/06-adjuster-pages.sh``) expects a 303 to the app's
``/auth/start``, follows it once and expects a 303 to the issuer's authorization
address, and prints neither query. It also sends two unsigned posts of a decision,
which the guard itself must refuse: the page's own form, with the page's own
``Origin`` (a cross-site post is refused by the origin check before the guard runs,
so it would prove nothing about the guard), must answer 401, and the JSON route's
post must answer 401 with ``WWW-Authenticate: Bearer``. It is still ONE line for the
queue, three PASS lines in all (``test_smoke_line_count.py``), and a failure names
which post. The function runs in bash against a stand-in ``curl``; no cluster.

What none of this proves: that the app answers so (the guard is another
contract's), that Envoy forwards the posts, that the issuer shows its login page.
"""

import os
import re
import stat
from pathlib import Path

import pytest
from kindsupport import KIND_DIR, SMOKE_SH, function_definition, one_line_function
from test_kind_signin_switch import (
    AUTHORIZATION,
    ORIGIN,
    QUERY_MARK,
    bash,
    shell_constant,
)

CURL = r"""#!/usr/bin/env bash
headers="" body="" format="" url="" origin="" type="" data=""
while (($#)); do
  case "$1" in
    -D) headers=$2; shift 2 ;;
    -o) body=$2; shift 2 ;;
    -w) format=$2; shift 2 ;;
    -H)
      case "$2" in
        Origin:*) origin=${2#Origin: } ;;
        Content-Type:*) type=${2#Content-Type: } ;;
      esac
      shift 2 ;;
    --data) data=$2; shift 2 ;;
    -m | --noproxy) shift 2 ;;
    -*) shift ;;
    *) url=$1; shift ;;
  esac
done
echo "${url}" >>"${CURL_LOG}"
printf '%s|%s|%s|%s\n' "${url}" "${origin}" "${type}" "${data}" >>"${CURL_FULL}"
status=200 location="" text="" challenge=""
case "${url}" in
  */adjuster/claims)
    status=${QUEUE_STATUS}; location=${QUEUE_LOCATION}; text="Synthetic data only." ;;
  */auth/start*)
    status=${START_STATUS}; location=${START_LOCATION} ;;
  */adjuster/claims/CLM-9999/decision)
    if [[ "${origin}" == "${PAGE_ORIGIN}" ]]; then
      status=${FORM_STATUS}
    else
      status=${DECISION_STATUS}
    fi ;;
  */claims/CLM-9999/decision) status=${JSON_STATUS}; challenge=${JSON_CHALLENGE} ;;
  */claimant/claims) text="${CLAIMANT_TEXT}" ;;
esac
{
  printf 'HTTP/1.1 %s\r\n' "${status}"
  [[ -z "${location}" ]] || printf 'Location: %s\r\n' "${location}"
  [[ -z "${challenge}" ]] || printf 'WWW-Authenticate: %s\r\n' "${challenge}"
  printf "Content-Security-Policy: frame-ancestors 'none'; default-src 'none'\r\n"
} >"${headers:-/dev/null}"
[[ "${body}" == /dev/null || -z "${body}" ]] || printf '%s' "${text}" >"${body}"
printf '%s' "${status}"
"""
GOOD = {
    "QUEUE_STATUS": "303",
    "QUEUE_LOCATION": f"/auth/start?return_to=%2Fadjuster%2Fclaims&{QUERY_MARK}=1",
    "START_STATUS": "303",
    "START_LOCATION": f"{AUTHORIZATION}?client_id=web&state={QUERY_MARK}",
    "DECISION_STATUS": "403",
    # With the sign-in on, the guard refuses an unsigned post before any lookup.
    "FORM_STATUS": "401",
    "JSON_STATUS": "401",
    "JSON_CHALLENGE": "Bearer",
}
FUNCTIONS = (
    "adjuster_location",
    "adjuster_absolute",
    "adjuster_without_query",
    "adjuster_guard_refuses_posts",
    "adjuster_queue_signed_out",
    "check_adjuster_pages",
)


def run_pages(
    tmp_path: Path, *, signin: str = "staff", **answers: str
) -> tuple[list[str], list[str], str]:
    """``check_adjuster_pages`` of smoke.sh (with the functions it calls) in bash
    against a stand-in ``curl``. Returns the PASS/FAIL lines, the addresses curl was
    asked for, and everything printed."""
    log = tmp_path / "curl-calls"
    stubs = tmp_path / "bin"
    stubs.mkdir()
    curl = stubs / "curl"
    curl.write_text(CURL, encoding="utf-8")
    curl.chmod(curl.stat().st_mode | stat.S_IXUSR)
    constants = re.findall(r"^readonly (?:ADJUSTER|CLAIMANT)_\w+=.*$", SMOKE_SH, re.M)
    script = "\n".join(
        [
            'pass() { echo "PASS  $*"; }',
            'fail() { echo "FAIL  $*"; }',
            'skip() { echo "SKIP  $*"; }',
            f'. "{KIND_DIR}/common.sh"',
            *constants,
            one_line_function(SMOKE_SH, "clean_lines"),
            "deployed_services() { echo deployment.apps/claims-api; }",
            *(function_definition(SMOKE_SH, name) for name in FUNCTIONS),
            "check_adjuster_pages",
        ]
    )
    done = bash(
        script,
        PATH=f"{stubs}:{os.environ['PATH']}",
        CURL_LOG=str(log),
        CURL_FULL=str(tmp_path / "curl-full"),
        PAGE_ORIGIN=ORIGIN,
        CLAIMANT_TEXT=shell_constant(SMOKE_SH, "CLAIMANT_BANNER"),
        MERIDIAN_SIGNIN=signin,
        **{**GOOD, **answers},
    )
    assert done.returncode == 0, done.stderr
    calls = log.read_text().splitlines() if log.exists() else []
    return (
        [x for x in done.stdout.splitlines() if x[:4] in ("PASS", "FAIL")],
        calls,
        (done.stdout + done.stderr),
    )


def full_log(tmp_path: Path) -> list[list[str]]:
    """What the stand-in curl was sent: address, Origin, Content-Type, body."""
    lines = (tmp_path / "curl-full").read_text().splitlines()
    return [line.split("|", 3) for line in lines]


def test_with_the_sign_in_on_the_queue_is_a_303_to_the_start_and_to_the_issuer(
    tmp_path: Path,
) -> None:
    lines, calls, output = run_pages(tmp_path)

    assert [x.split()[0] for x in lines] == ["PASS"] * 3, output
    assert calls[0] == "http://claims.meridian.localhost:8088/adjuster/claims"
    # It follows the redirect once, to the start, and goes no further.
    assert calls[1].startswith(f"{ORIGIN}/auth/start?")
    assert len([c for c in calls if "/auth/start" in c]) == 1
    assert not any(c.startswith(AUTHORIZATION) for c in calls)
    assert "303" in lines[0] and "/auth/start" in lines[0]
    assert AUTHORIZATION in lines[0]


def test_neither_query_is_printed(tmp_path: Path) -> None:
    _, _, output = run_pages(tmp_path)

    assert QUERY_MARK not in output
    for secretless in ("return_to", "state=", "client_id", "%2F"):
        assert secretless not in output


def test_the_other_two_lines_stay_what_they_were(tmp_path: Path) -> None:
    lines, calls, _ = run_pages(tmp_path)

    assert "403" in lines[1] and "attacker.example" in lines[1]
    assert "fictional-data line" in lines[2]
    assert calls[-1] == "http://claims.meridian.localhost:8088/claimant/claims"


def test_an_absolute_location_under_the_origin_is_accepted_too(tmp_path: Path) -> None:
    lines, calls, _ = run_pages(
        tmp_path, QUEUE_LOCATION=f"{ORIGIN}/auth/start?return_to=x"
    )

    assert [x.split()[0] for x in lines] == ["PASS"] * 3
    assert calls[1] == f"{ORIGIN}/auth/start?return_to=x"


@pytest.mark.parametrize(
    ("answers", "says"),
    [
        ({"QUEUE_STATUS": "200", "QUEUE_LOCATION": ""}, "expected 303"),
        ({"QUEUE_STATUS": "302"}, "expected 303"),
        ({"QUEUE_STATUS": "401", "QUEUE_LOCATION": ""}, "expected 303"),
        ({"QUEUE_LOCATION": "/elsewhere?x=1"}, "not to the app's /auth/start"),
        ({"QUEUE_LOCATION": "http://attacker.example/auth/start"}, "not to the app's"),
        ({"QUEUE_LOCATION": ""}, "no Location"),
        ({"QUEUE_LOCATION": "/auth/startx"}, "not to the app's"),
        ({"START_STATUS": "200", "START_LOCATION": ""}, "expected 303"),
        ({"START_STATUS": "500", "START_LOCATION": ""}, "expected 303"),
        ({"START_LOCATION": "http://attacker.example/auth?x=1"}, "not to the issuer"),
        ({"START_LOCATION": f"{AUTHORIZATION}x?a=1"}, "not to the issuer"),
        ({"START_LOCATION": ""}, "no Location"),
    ],
    ids=lambda x: (
        x if isinstance(x, str) else ",".join(f"{k}={v}" for k, v in x.items())
    ),
)
def test_a_wrong_answer_is_a_fail_line_that_says_what_was_expected(
    tmp_path: Path, answers: dict[str, str], says: str
) -> None:
    lines, _, output = run_pages(tmp_path, **answers)

    assert [x.split()[0] for x in lines].count("FAIL") == 1, output
    (failed,) = [x for x in lines if x.startswith("FAIL")]
    assert says in failed
    assert len(lines) == 3
    for text in (QUERY_MARK, "return_to", "state=", "client_id"):
        assert text not in output


def test_the_queue_line_also_posts_a_form_and_a_json_decision_and_they_are_401(
    tmp_path: Path,
) -> None:
    lines, calls, _ = run_pages(tmp_path)

    # Still one PASS line for the queue, and it says all three.
    assert [x.split()[0] for x in lines] == ["PASS"] * 3
    assert "form post" in lines[0] and "JSON post" in lines[0]
    assert "401" in lines[0] and "WWW-Authenticate: Bearer" in lines[0]
    sent = full_log(tmp_path)
    form = [s for s in sent if s[0].endswith("/adjuster/claims/CLM-9999/decision")]
    # The first post to that address carries the page's own origin (so the origin
    # check passes and the guard is what answers); the second is the old check's.
    assert form[0] == [
        f"{ORIGIN}/adjuster/claims/CLM-9999/decision",
        ORIGIN,
        "",
        "decision=approve&run=",
    ]
    assert form[1][1] == "http://attacker.example"
    (json_post,) = [s for s in sent if s[0] == f"{ORIGIN}/claims/CLM-9999/decision"]
    assert json_post[2] == "application/json"
    assert json_post[3] == '{"decision": "approve"}'
    assert calls.index(f"{ORIGIN}/claims/CLM-9999/decision") < len(calls) - 2


@pytest.mark.parametrize(
    ("answers", "says"),
    [
        ({"FORM_STATUS": "200"}, "form post"),
        ({"FORM_STATUS": "403"}, "form post"),
        ({"FORM_STATUS": "303"}, "form post"),
        ({"FORM_STATUS": "500"}, "form post"),
        ({"JSON_STATUS": "200"}, "JSON post"),
        ({"JSON_STATUS": "403"}, "JSON post"),
        ({"JSON_STATUS": "302"}, "JSON post"),
        ({"JSON_CHALLENGE": ""}, "WWW-Authenticate"),
        ({"JSON_CHALLENGE": "Basic realm=x"}, "WWW-Authenticate"),
        ({"JSON_CHALLENGE": "Bearerish"}, "WWW-Authenticate"),
    ],
    ids=lambda x: (
        x if isinstance(x, str) else ",".join(f"{k}={v}" for k, v in x.items())
    ),
)
def test_a_post_the_guard_does_not_refuse_is_a_fail_line_that_names_which(
    tmp_path: Path, answers: dict[str, str], says: str
) -> None:
    lines, _, output = run_pages(tmp_path, **answers)

    assert [x.split()[0] for x in lines].count("FAIL") == 1, output
    (failed,) = [x for x in lines if x.startswith("FAIL")]
    assert says in failed
    assert lines[0].startswith("FAIL")  # it is the queue's line that carries it
    assert len(lines) == 3


def test_a_form_that_fails_is_named_and_the_json_post_is_not_sent(
    tmp_path: Path,
) -> None:
    run_pages(tmp_path, FORM_STATUS="200")

    assert not [
        s
        for s in full_log(tmp_path)
        if s[0].endswith("/claims/CLM-9999/decision") and "/adjuster/" not in s[0]
    ]


def test_with_the_sign_in_off_neither_post_is_made(tmp_path: Path) -> None:
    _, calls, _ = run_pages(tmp_path, signin="", QUEUE_STATUS="200", QUEUE_LOCATION="")

    assert f"{ORIGIN}/claims/CLM-9999/decision" not in calls
    assert len(full_log(tmp_path)) == 3  # the queue, the decision, the claimant


def test_with_the_sign_in_on_no_query_or_body_is_printed(tmp_path: Path) -> None:
    _, _, output = run_pages(tmp_path)

    assert "decision=approve" not in output
    assert '{"decision"' not in output


def test_with_the_sign_in_off_the_queue_is_the_200_it_always_was(
    tmp_path: Path,
) -> None:
    lines, calls, _ = run_pages(
        tmp_path, signin="", QUEUE_STATUS="200", QUEUE_LOCATION=""
    )

    assert [x.split()[0] for x in lines] == ["PASS"] * 3
    assert "200" in lines[0] and "Content-Security-Policy" in lines[0]
    assert not [c for c in calls if "/auth/start" in c]


def test_with_the_sign_in_off_a_303_is_still_a_failure(tmp_path: Path) -> None:
    lines, _, _ = run_pages(tmp_path, signin="off")

    assert lines[0].startswith("FAIL")
