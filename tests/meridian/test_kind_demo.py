"""demo.sh, run whole against stand-in commands.

The adjuster's decision and the two traces (S015).
"""

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from kindsupport import (
    DEMO_SH,
    KIND_DIR,
    function_definition,
)

from meridian.workloads.claims_triage.triaging import (
    DIFFERENT_SUBMISSION_DETAIL,
    HAS_PROPOSAL_DETAIL,
)

# ── demo.sh: the claim, the adjuster's decision and the two traces (S015) ────

# No other test runs demo.sh: these run the whole script, copied with its
# helpers into a scratch tree, against stubs for the two programs that reach out
# (curl: the Claims API, the edge and Tempo through Grafana; kubectl: the
# cluster and the Grafana port-forward). The stub curl records every call in
# ``calls`` (a "POST url traceparent-trace-id body" or "GET url" line each) and
# answers from the environment.
TRIAGE_FIVE = "claims-api agent-runtime policy-mcp knowledge-mcp model-gateway"
DECISION_THREE = "claims-api agent-runtime claims-mcp"
DEMO_CLAIMS = [
    {"claim_id": "CLM-0001", "description": "first"},
    {"claim_id": "CLM-0002", "description": "second"},
]
requires_demo_tools = pytest.mark.skipif(
    not all(shutil.which(tool) for tool in ("jq", "openssl", "base64")),
    reason="jq, openssl or base64 is not installed",
)
STUB_CURL = r"""#!/usr/bin/env bash
out="" url="" reads=no
while (($#)); do
  case "$1" in
    -o) out=$2; shift ;;
    -H)
      case "$2" in
        traceparent:*) trace="${2#*: 00-}"; trace="${trace%%-*}" ;;
      esac
      shift ;;
    --data-binary) reads=body; shift ;;
    -K) reads=config; shift ;;
    -w | -m | --noproxy) shift ;;
    -*) ;;
    *) url=$1 ;;
  esac
  shift
done
body=""
case "${reads}" in body) body="$(cat)" ;; config) cat >/dev/null ;; esac
case "${url}" in
  */healthz) printf 200 ;;
  */claims/*/decision)
    echo "POST ${url} ${trace} ${body}" >>"${STUB_DIR}/calls"
    echo "${trace}" >"${STUB_DIR}/decision_trace"
    printf '%s' "${STUB_DECISION_ANSWER}" >"${out}"
    printf '%s' "${STUB_DECISION_STATUS}" ;;
  */claims)
    echo "POST ${url} ${trace} ${body}" >>"${STUB_DIR}/calls"
    if [[ -n "${STUB_CONFLICT_DETAIL}" && "${body}" == *'"CLM-0001"'* ]]; then
      jq -cn --arg detail "${STUB_CONFLICT_DETAIL}" '{detail: $detail}' >"${out}"
      printf 409
    else
      printf '%s' "${STUB_SUBMIT_ANSWER}" >"${out}"
      printf 201
    fi ;;
  */tempo/api/traces/*)
    echo "GET ${url}" >>"${STUB_DIR}/calls"
    id="${url##*/}"
    services="${STUB_TRIAGE_SERVICES}"
    decided=""
    marker="${STUB_DIR}/decision_trace"
    [[ ! -f "${marker}" ]] || decided="$(cat "${marker}")"
    if [[ "${decided}" == "${id}" ]]; then services="${STUB_DECISION_SERVICES}"; fi
    # The reading's number for this trace: STUB_GROW_UNTIL makes every service
    # report 2 + min(reading, STUB_GROW_UNTIL) spans, and STUB_LATE_AFTER holds
    # model-gateway back until that reading. STUB_ALTERNATE_UNTIL answers the
    # odd readings up to that number with every service, and the even ones and
    # every one after it without the first (claims-api for the triage). All
    # unset: two spans, always.
    echo >>"${STUB_DIR}/reads-${id}"
    reading="$(wc -l <"${STUB_DIR}/reads-${id}" | tr -d ' ')"
    spans=2
    if [[ -n "${STUB_GROW_UNTIL}" ]]; then
      spans=$((2 + (reading < STUB_GROW_UNTIL ? reading : STUB_GROW_UNTIL)))
    fi
    if [[ -n "${STUB_LATE_AFTER}" ]] && ((reading < STUB_LATE_AFTER)); then
      services="${services/model-gateway/}"
    fi
    if [[ -n "${STUB_ALTERNATE_UNTIL}" ]] &&
      { ((reading > STUB_ALTERNATE_UNTIL)) || ((reading % 2 == 0)); }; then
      services="${services#* }"
    fi
    # STUB_SILENT_SERVICES names services that are in the answer with a
    # resource and no span (the shape of a service that sent nothing yet).
    jq -cn --arg services "${services}" --argjson spans "${spans}" \
      --arg silent " ${STUB_SILENT_SERVICES} " '{batches: [
      ($services | split(" ")[] | select(. != "")) as $name
      | (if ($silent | contains(" " + $name + " ")) then 0 else $spans end) as $count
      | {resource: {attributes: [{key: "service.name", value: {stringValue: $name}}]},
         scopeSpans: [{spans: [range(0; $count) | {}]}]}]}'
    printf '\n200' ;;
  *) echo "unexpected curl: ${url}" >&2; exit 1 ;;
esac
"""
STUB_KUBECTL = r"""#!/usr/bin/env bash
case "$*" in
  *port-forward*) echo "Forwarding from 127.0.0.1:41999 -> 3000"; exec sleep 30 ;;
  *"get nodes"*) exit 0 ;;
  *"get secret"*) printf '%s' "$(printf '%s' stub-admin-value | base64)" ;;
esac
"""


def referred_answer(state: str = "awaiting_adjuster", route: str = "adjuster") -> str:
    """The Claims API's answer to a posted claim."""
    paused = state == "awaiting_adjuster"
    return json.dumps(
        {
            "claim_id": "CLM-0001",
            "state": state,
            "run_id": "3f1c2d4e-0000-4000-8000-000000000001",
            "run_status": "AwaitingApproval" if paused else "Completed",
            "proposal": {"route": route, "drafted_by": None},
        }
    )


def decision_answer(state: str = "approved") -> str:
    return json.dumps(
        {
            "claim_id": "CLM-0001",
            "state": state,
            "run_id": "3f1c2d4e-0000-4000-8000-000000000001",
            "run_status": "Completed",
        }
    )


# The poll's own constants in these runs (the script's are 120 and 3). The wait
# between two readings does not sleep: it moves the script's clock (``SECONDS``)
# by ``POLL_INTERVAL_SECONDS``, so the deadline is a count of readings, not of
# seconds the machine may not have: a trace that never settles is read
# ``MAX_READINGS`` times (ten) and the run ends at once, however busy the
# machine is, and a trace that settles after three readings has used 20 of the
# 100 seconds, so 80 more of real time may pass before it would fail (S074: a
# real deadline of 3 or 6 seconds failed twice, unnamed, at a load over 100).
POLL_TIMEOUT_SECONDS = 100
POLL_INTERVAL_SECONDS = 10
MAX_READINGS = POLL_TIMEOUT_SECONDS // POLL_INTERVAL_SECONDS


def run_demo(
    tmp_path: Path,
    *,
    decision: str | None = None,
    submit_answer: str | None = None,
    decision_status: str = "200",
    decision_body: str | None = None,
    triage_services: str = TRIAGE_FIVE + " claims-mcp",
    decision_services: str = DECISION_THREE,
    grow_until: int | None = None,
    late_after: int | None = None,
    alternate_until: int | None = None,
    first_claim_conflict: str | None = None,
    silent_services: str = "",
) -> tuple[subprocess.CompletedProcess[str], list[list[str]]]:
    """demo.sh run in a scratch copy of ``infra/kind`` against the stubs, with
    the poll of ``POLL_TIMEOUT_SECONDS`` and ``POLL_INTERVAL_SECONDS`` whose wait
    moves the script's clock instead of sleeping (see them): a trace that never
    settles ends the run after ``MAX_READINGS`` readings, and one that settles
    is not failed by the time the machine took. ``decision``
    sets the DECISION variable (unset when None).
    ``grow_until``, ``late_after`` and ``alternate_until`` shape what the stub
    Tempo answers (see the stub); ``first_claim_conflict`` is the detail of a
    409 the stub answers to the first claim (the second is accepted);
    ``silent_services`` lists services (space-separated) that the stub's
    answers carry with a resource and no span. Returns
    the process and the
    stub curl's calls, each as its words: ``POST``/``GET``, the URL, and for a
    POST the trace ID the traceparent carried and the body."""
    kind, bin_dir = tmp_path / "infra" / "kind", tmp_path / "bin"
    data = tmp_path / "data" / "synthetic"
    for folder in (kind, bin_dir, data):
        folder.mkdir(parents=True)
    # The timeouts are readonly constants of the script. The one wait of the
    # poll (the others, for the edge and for the port-forward, stay real) adds
    # the interval to ``SECONDS`` rather than sleeping, as the smoke tests'
    # stand-ins for ``sleep`` do: no real second is spent between readings, and
    # a trace that never settles ends the run after a fixed number of them.
    patched, count = re.subn(
        r"^readonly POLL_(TIMEOUT|INTERVAL)=\d+$",
        lambda match: (
            f"readonly POLL_{match[1]}="
            + str(
                POLL_TIMEOUT_SECONDS if match[1] == "TIMEOUT" else POLL_INTERVAL_SECONDS
            )
        ),
        DEMO_SH,
        flags=re.MULTILINE,
    )
    assert count == 2
    patched, count = re.subn(
        r'^(\s*)sleep "\$\{POLL_INTERVAL\}"$',
        r"\1SECONDS=$((SECONDS + POLL_INTERVAL))",
        patched,
        flags=re.MULTILINE,
    )
    assert count == 1
    (kind / "demo.sh").write_text(patched, encoding="utf-8")
    for name in ("common.sh", "pins.env"):
        (kind / name).write_text((KIND_DIR / name).read_text(encoding="utf-8"))
    (kind / "kubeconfig").touch()
    (data / "claims.json").write_text(json.dumps(DEMO_CLAIMS), encoding="utf-8")
    for name, text in (("curl", STUB_CURL), ("kubectl", STUB_KUBECTL)):
        (bin_dir / name).write_text(text, encoding="utf-8")
        (bin_dir / name).chmod(0o755)
    env = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "STUB_DIR": str(tmp_path),
        "STUB_SUBMIT_ANSWER": submit_answer or referred_answer(),
        "STUB_DECISION_STATUS": decision_status,
        "STUB_DECISION_ANSWER": decision_body or decision_answer(),
        "STUB_TRIAGE_SERVICES": triage_services,
        "STUB_DECISION_SERVICES": decision_services,
        "STUB_GROW_UNTIL": "" if grow_until is None else str(grow_until),
        "STUB_LATE_AFTER": "" if late_after is None else str(late_after),
        "STUB_ALTERNATE_UNTIL": (
            "" if alternate_until is None else str(alternate_until)
        ),
        "STUB_CONFLICT_DETAIL": first_claim_conflict or "",
        "STUB_SILENT_SERVICES": silent_services,
    }
    if decision is not None:
        env["DECISION"] = decision
    done = subprocess.run(
        ["bash", str(kind / "demo.sh")],
        capture_output=True,
        text=True,
        env=env,
        check=False,
        timeout=60,
    )
    calls = tmp_path / "calls"
    lines = calls.read_text().splitlines() if calls.exists() else []
    return done, [line.split(" ", 3) for line in lines]


def posts_of(calls: list[list[str]]) -> list[list[str]]:
    return [call for call in calls if call[0] == "POST"]


def trace_reads(calls: list[list[str]]) -> dict[str, int]:
    """How many times Tempo was asked for each trace, in order of first ask."""
    reads: dict[str, int] = {}
    for call in calls:
        if call[0] == "GET":
            trace = call[1].rsplit("/", 1)[1]
            reads[trace] = reads.get(trace, 0) + 1
    return reads


@requires_demo_tools
def test_a_referred_claim_is_decided_with_approve_unless_told_otherwise(
    tmp_path: Path,
) -> None:
    done, calls = run_demo(tmp_path)

    assert done.returncode == 0, done.stdout + done.stderr
    claim_post, decision_post = posts_of(calls)
    assert claim_post[1].endswith(":8088/claims")
    # Only the first claim is posted, and the decision goes to that claim.
    assert decision_post[1].endswith(":8088/claims/CLM-0001/decision")
    assert json.loads(decision_post[3]) == {"decision": "approve"}
    # Each post carries a trace ID of its own; the traces read back are those.
    assert len(claim_post[2]) == len(decision_post[2]) == 32
    assert claim_post[2] != decision_post[2]
    assert list(trace_reads(calls)) == [claim_post[2], decision_post[2]]
    lines = done.stdout.splitlines()
    assert "state       awaiting_adjuster" in lines
    assert "decision    approve" in lines
    assert "state       approved" in lines
    assert "run status  Completed" in lines
    assert any(line.startswith("PASS  trace ") for line in lines)
    assert any(line.startswith("PASS  decision trace ") for line in lines)
    assert "no adjuster was needed" not in done.stdout


@requires_demo_tools
def test_decision_reject_is_what_a_referred_claim_is_decided_with(
    tmp_path: Path,
) -> None:
    done, calls = run_demo(
        tmp_path, decision="reject", decision_body=decision_answer("rejected")
    )

    assert done.returncode == 0, done.stdout + done.stderr
    assert json.loads(posts_of(calls)[1][3]) == {"decision": "reject"}
    assert "decision    reject" in done.stdout.splitlines()
    assert "state       rejected" in done.stdout.splitlines()


@requires_demo_tools
def test_an_empty_decision_variable_means_approve_as_when_make_passes_none(
    tmp_path: Path,
) -> None:
    done, calls = run_demo(tmp_path, decision="")

    assert done.returncode == 0, done.stdout + done.stderr
    assert json.loads(posts_of(calls)[1][3]) == {"decision": "approve"}


@requires_demo_tools
@pytest.mark.parametrize(
    "decision", ["approved", "Approve", "reject; true", " approve"]
)
def test_a_decision_that_is_not_one_of_the_three_words_posts_nothing_and_fails(
    tmp_path: Path, decision: str
) -> None:
    done, calls = run_demo(tmp_path, decision=decision)

    assert done.returncode != 0
    assert calls == []  # not even the edge's health check
    assert "DECISION must be approve, reject or request_documents" in done.stderr
    assert "PASS" not in done.stdout


@requires_demo_tools
def test_request_documents_is_a_decision_the_script_posts(tmp_path: Path) -> None:
    done, calls = run_demo(
        tmp_path,
        decision="request_documents",
        decision_body=decision_answer("documents_requested"),
    )

    assert done.returncode == 0, done.stdout + done.stderr
    assert json.loads(posts_of(calls)[1][3]) == {"decision": "request_documents"}
    assert "state       documents_requested" in done.stdout.splitlines()


@requires_demo_tools
def test_a_claim_that_is_not_referred_posts_no_decision_and_passes_on_its_trace(
    tmp_path: Path,
) -> None:
    done, calls = run_demo(
        tmp_path,
        submit_answer=referred_answer("approved", "auto_approve"),
        triage_services=TRIAGE_FIVE,  # no approval request, so no claims-mcp
    )

    assert done.returncode == 0, done.stdout + done.stderr
    assert len(posts_of(calls)) == 1
    assert not any("/decision" in call[1] for call in calls)
    lines = done.stdout.splitlines()
    assert "state       approved" in lines
    assert any(line.startswith("PASS  trace ") for line in lines)
    assert not any("decision trace" in line for line in lines)
    assert "no adjuster was needed; the next make demo posts the next claim" in lines
    assert len(trace_reads(calls)) == 1  # the triage's trace only


@requires_demo_tools
def test_the_decision_trace_check_fails_when_claims_mcp_has_no_span(
    tmp_path: Path,
) -> None:
    done, _ = run_demo(tmp_path, decision_services="claims-api agent-runtime")

    lines = done.stdout.splitlines()
    assert done.returncode != 0
    assert any(line.startswith("PASS  trace ") for line in lines)  # the triage
    (failure,) = [line for line in lines if line.startswith("FAIL")]
    assert failure.startswith("FAIL  no decision trace ")
    assert "claims-api agent-runtime claims-mcp" in failure
    # What Tempo did return is listed, so the missing service shows.
    returned = lines[lines.index("      Tempo returned:") + 1 :]
    assert [line.split()[0] for line in returned[:2]] == ["agent-runtime", "claims-api"]
    assert not any(line.strip().startswith("claims-mcp") for line in returned)


@requires_demo_tools
def test_the_triage_trace_check_fails_without_hiding_the_decision_traces_result(
    tmp_path: Path,
) -> None:
    done, _ = run_demo(tmp_path, triage_services="claims-api agent-runtime")

    lines = done.stdout.splitlines()
    assert done.returncode != 0
    (failure,) = [line for line in lines if line.startswith("FAIL")]
    assert failure.startswith("FAIL  no trace ")
    assert "policy-mcp" in failure and "model-gateway" in failure
    assert any(line.startswith("PASS  decision trace ") for line in lines)


@requires_demo_tools
def test_the_decision_is_not_followed_by_a_trace_check_when_the_api_refuses_it(
    tmp_path: Path,
) -> None:
    refusal = json.dumps({"detail": "the claim does not wait for an adjuster"})
    done, calls = run_demo(tmp_path, decision_status="409", decision_body=refusal)

    assert done.returncode != 0
    assert (
        "CLM-0001: decision HTTP 409 the claim does not wait for an adjuster"
        in done.stderr
    )
    assert [c for c in calls if c[0] == "GET"] == []
    assert "PASS" not in done.stdout


def demo_constant(name: str) -> str:
    """The value of a quoted ``readonly NAME="..."`` constant of demo.sh."""
    (value,) = re.findall(rf'^readonly {name}="(.*)"$', DEMO_SH, re.MULTILINE)
    return value


def test_demo_skips_a_claim_on_the_two_409_details_the_claims_api_gives() -> None:
    assert demo_constant("ALREADY_TRIAGED") == HAS_PROPOSAL_DETAIL
    assert demo_constant("DIFFERENT_SUBMISSION") == DIFFERENT_SUBMISSION_DETAIL


@requires_demo_tools
@pytest.mark.parametrize(
    ("detail", "logged"),
    [
        (HAS_PROPOSAL_DETAIL, "CLM-0001: already triaged, trying the next claim"),
        (
            DIFFERENT_SUBMISSION_DETAIL,
            "CLM-0001: exists with a different submission (the claimant's form "
            "stamps its own report date), trying the next claim",
        ),
    ],
)
def test_a_409_that_means_the_claim_is_taken_moves_on_to_the_next_claim(
    tmp_path: Path, detail: str, logged: str
) -> None:
    done, calls = run_demo(tmp_path, first_claim_conflict=detail)

    assert done.returncode == 0, done.stdout + done.stderr
    claim_posts = [call for call in posts_of(calls) if "/decision" not in call[1]]
    assert [json.loads(call[3])["claim_id"] for call in claim_posts] == [
        "CLM-0001",
        "CLM-0002",
    ]
    assert f"==> {logged}" in done.stdout.splitlines()
    assert "claim       CLM-0002" in done.stdout.splitlines()
    assert posts_of(calls)[-1][1].endswith(":8088/claims/CLM-0002/decision")


@requires_demo_tools
def test_any_other_409_still_stops_the_demo(tmp_path: Path) -> None:
    done, calls = run_demo(tmp_path, first_claim_conflict="something else is wrong")

    assert done.returncode != 0
    assert "error: CLM-0001: 409 something else is wrong" in done.stderr
    assert len(posts_of(calls)) == 1  # the second claim is not tried
    assert "PASS" not in done.stdout


SETTLE_POLLS = 3  # demo.sh: readings with unchanged counts before PASS


@requires_demo_tools
def test_a_trace_that_has_every_service_at_once_passes_after_three_readings(
    tmp_path: Path,
) -> None:
    done, calls = run_demo(tmp_path)

    assert done.returncode == 0, done.stdout + done.stderr
    # Both traces are read exactly SETTLE_POLLS times: not once, not four times.
    assert list(trace_reads(calls).values()) == [SETTLE_POLLS, SETTLE_POLLS]
    assert re.search(r"^readonly SETTLE_POLLS=3$", DEMO_SH, re.MULTILINE)


@requires_demo_tools
def test_a_trace_that_never_settles_is_read_a_fixed_number_of_times(
    tmp_path: Path,
) -> None:
    done, calls = run_demo(tmp_path, grow_until=1_000_000)

    assert done.returncode != 0
    reads = list(trace_reads(calls).values())
    assert len(reads) == 2  # both traces
    # The deadline is counted by the script's clock, which the wait between two
    # readings moves: never more readings than the count says, however fast the
    # machine is, and not fewer than half of it unless the machine took half the
    # timeout of real time over them.
    assert all(MAX_READINGS // 2 <= count <= MAX_READINGS for count in reads), reads


@requires_demo_tools
def test_a_trace_that_grows_and_then_stays_the_same_passes_with_its_final_counts(
    tmp_path: Path,
) -> None:
    # Readings 1 to 3 report 3, 4, 5 spans per service; every later one 5. The
    # run of equal readings starts at the third (the first with the final
    # counts), so the script stops at the fifth.
    done, calls = run_demo(tmp_path, grow_until=3)

    lines = done.stdout.splitlines()
    assert done.returncode == 0, done.stdout + done.stderr
    assert list(trace_reads(calls).values()) == [5, 5]
    assert any(line.startswith("PASS  trace ") for line in lines)
    spans = [line.split() for line in lines if line.endswith(" span(s)")]
    assert len(spans) == 9  # the triage's six services, then the decision's three
    assert {words[1] for words in spans} == {"5"}
    assert not any(line.startswith("FAIL") for line in lines)


@requires_demo_tools
def test_a_service_that_arrives_late_restarts_the_count_of_unchanged_readings(
    tmp_path: Path,
) -> None:
    # model-gateway is absent from readings 1 and 2: the readings that have
    # every service are 3, 4 and 5, so no sooner than the fifth passes.
    done, calls = run_demo(tmp_path, late_after=3)

    assert done.returncode == 0, done.stdout + done.stderr
    assert trace_reads(calls)[posts_of(calls)[0][2]] == 5
    assert "PASS  trace " in done.stdout


@requires_demo_tools
def test_a_trace_that_keeps_growing_until_the_deadline_fails_as_still_growing(
    tmp_path: Path,
) -> None:
    done, calls = run_demo(tmp_path, grow_until=1_000_000)

    lines = done.stdout.splitlines()
    assert done.returncode != 0
    assert "PASS" not in done.stdout
    triage, decision = [line for line in lines if line.startswith("FAIL")]
    reads = trace_reads(calls)
    triage_id, decision_id = reads  # the order the script read them in
    still_growing = f"was still growing after {POLL_TIMEOUT_SECONDS}s"
    assert triage == f"FAIL  trace {triage_id} {still_growing}"
    assert decision == f"FAIL  decision trace {decision_id} {still_growing}"
    # Every service was there, so the old "no trace with spans from all of"
    # line would be wrong; what Tempo last returned is listed, with the counts
    # of the last reading (2 spans, and one more with each reading).
    assert "no trace" not in done.stdout
    first = lines.index("      Tempo returned:") + 1
    returned = lines[first : lines.index(decision)]
    triage_counts = [
        line.split()[1] for line in returned if line.startswith("        ")
    ]
    assert triage_counts == [str(2 + reads[triage_id])] * 6
    assert reads[triage_id] > SETTLE_POLLS  # it did not stop at three


@requires_demo_tools
def test_a_trace_whose_readings_alternate_fails_saying_they_alternated(
    tmp_path: Path,
) -> None:
    # Readings 1 and 3 have every service, 2 and 4 lack one, and so does every
    # later reading: two complete in a row never happens, and the last reading
    # is partial although the trace was complete twice.
    done, calls = run_demo(tmp_path, alternate_until=4)

    lines = done.stdout.splitlines()
    assert done.returncode != 0
    assert "PASS" not in done.stdout
    triage, decision = [line for line in lines if line.startswith("FAIL")]
    reads = trace_reads(calls)
    triage_id, decision_id = reads
    assert triage == (
        f"FAIL  trace {triage_id}: readings alternated between complete and "
        f"partial (2 complete, {reads[triage_id] - 2} partial or missing) "
        f"over {POLL_TIMEOUT_SECONDS}s, Tempo was still settling"
    )
    assert decision.startswith(f"FAIL  decision trace {decision_id}: readings alt")
    # It says what to do: look the trace up by its ID in a moment.
    remedy = (
        "      Look it up in a moment: make grafana, then Explore, Tempo, "
        'TraceQL { trace:id = "%s" }'
    )
    assert remedy % triage_id in lines
    assert remedy % decision_id in lines
    assert reads[triage_id] > 4  # the last readings were partial
    # Neither of the two other wordings: every service was there at times, and
    # the trace did not keep growing.
    assert "no trace" not in done.stdout
    assert "still growing" not in done.stdout


@requires_demo_tools
def test_one_complete_reading_and_then_only_partial_ones_is_worded_as_alternated(
    tmp_path: Path,
) -> None:
    # Reading 1 has every service, every later one lacks one. The script never
    # saw complete, partial, complete: "alternated" is its inference from one
    # complete reading and the partial ones after it, and demo.sh says so.
    done, calls = run_demo(tmp_path, alternate_until=1)

    lines = done.stdout.splitlines()
    triage_id, _ = trace_reads(calls)
    triage, _ = [line for line in lines if line.startswith("FAIL")]
    assert done.returncode != 0
    assert triage.startswith(f"FAIL  trace {triage_id}: readings alternated between")
    assert "(1 complete, " in triage
    assert "is an inference from those counts" in DEMO_SH


@requires_demo_tools
def test_a_trace_missing_a_service_still_fails_with_the_line_it_had_before(
    tmp_path: Path,
) -> None:
    done, _ = run_demo(tmp_path, triage_services="claims-api agent-runtime")

    (failure,) = [line for line in done.stdout.splitlines() if "FAIL" in line]
    assert done.returncode != 0
    assert failure.startswith("FAIL  no trace ")
    assert failure.endswith(
        " with spans from all of: "
        "claims-api agent-runtime policy-mcp knowledge-mcp model-gateway "
        f"after {POLL_TIMEOUT_SECONDS}s"
    )
    assert "still growing" not in done.stdout


@requires_demo_tools
def test_a_service_in_the_trace_with_no_span_does_not_count_as_present(
    tmp_path: Path,
) -> None:
    # Tempo's answer names model-gateway (a resource) but carries no span of it:
    # the per-service list says "model-gateway 0", which is not "spans from".
    done, _ = run_demo(tmp_path, silent_services="model-gateway")

    (failure,) = [line for line in done.stdout.splitlines() if "FAIL" in line]
    assert done.returncode != 0
    assert failure.startswith("FAIL  no trace ")
    assert failure.endswith(f" after {POLL_TIMEOUT_SECONDS}s")
    assert "PASS  trace " not in done.stdout
    # Every reading was partial, so none counts as complete or as alternating.
    assert "alternated" not in done.stdout
    assert "still growing" not in done.stdout
    # What Tempo returned is listed, the silent service with its zero.
    assert re.search(r"^ +model-gateway +0 span\(s\)$", done.stdout, re.MULTILINE)
    # The decision trace, which has all of its services, still passes.
    assert "PASS  decision trace " in done.stdout


@requires_demo_tools
def test_the_decisions_trace_fails_too_when_one_of_its_services_has_no_span(
    tmp_path: Path,
) -> None:
    done, _ = run_demo(tmp_path, silent_services="claims-mcp")

    # claims-mcp is in the decision trace's three and in the triage's optional
    # sixth, which is not required: the triage passes, the decision fails.
    assert done.returncode != 0
    assert "PASS  trace " in done.stdout
    (failure,) = [line for line in done.stdout.splitlines() if "FAIL" in line]
    assert failure.startswith("FAIL  no decision trace ")


@pytest.mark.parametrize(
    ("counts", "present"),
    [
        ("claims-api 1\nagent-runtime 12", True),
        ("claims-api 0\nagent-runtime 12", False),
        ("claims-api 10\nagent-runtime 0", False),
        ("claims-api 1", False),
        ("claims-api-extra 5\nagent-runtime 1", False),
    ],
)
def test_a_service_counts_as_present_from_its_first_span_and_not_before(
    counts: str, present: bool
) -> None:
    script = (
        function_definition(DEMO_SH, "has_every_service")
        + f"counts='{counts}'\n"
        + "has_every_service claims-api agent-runtime\n"
    )

    done = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, check=False, timeout=30
    )

    assert (done.returncode == 0) is present, done.stderr
