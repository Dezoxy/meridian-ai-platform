"""`make demo-seed`: the claims it posts, what it prints and how a run ends (S098).

The script is run whole against stand-ins for the programs that reach out; the
rig is in ``demoseedsupport.py``. The refusals before the first post, the data
the script reads and the Makefile's target are in
``test_kind_demo_seed_refusals.py``.
"""

import json
import re
from pathlib import Path

import pytest
from demoseedsupport import (
    CLAIMS,
    IDS,
    MARKER,
    MAX_READINGS,
    SETTLE_TIMEOUT_SECONDS,
    claim_lines,
    post_ids,
    posts_of,
    reads_of,
    requires_tools,
    run_seed,
    summary,
)

from meridian.workloads.claims_triage.triaging import (
    BEING_TRIAGED_DETAIL,
    DIFFERENT_SUBMISSION_DETAIL,
    TRIAGE_CAP_DETAIL,
)

# ── the claims, and their order ──────────────────────────────────────────────


@requires_tools
def test_the_first_forty_claims_are_posted_in_the_files_order_by_default(
    tmp_path: Path,
) -> None:
    done, calls = run_seed(tmp_path, count=None)

    assert done.returncode == 0, done.stdout + done.stderr
    assert post_ids(calls) == IDS[:40]
    first = posts_of(calls)[0]
    assert first[3] == "http://claims.meridian.localhost:8088/claims"
    # The body is the claim as the file holds it, as demo.sh posts it.
    assert json.loads((tmp_path / "body-CLM-0001").read_text()) == CLAIMS[0]
    traces = [call[2] for call in posts_of(calls)]
    assert all(re.fullmatch(r"[0-9a-f]{32}", trace) for trace in traces)
    assert len(set(traces)) == 40  # a trace of its own for each post


@requires_tools
@pytest.mark.parametrize(
    ("count", "posted"), [("1", 1), ("3", 3), ("47", 47), ("07", 7)]
)
def test_count_sets_how_many_claims_are_posted_up_to_all_forty_seven(
    tmp_path: Path, count: str, posted: int
) -> None:
    done, calls = run_seed(tmp_path, count=count)

    assert done.returncode == 0, done.stdout + done.stderr
    assert post_ids(calls) == IDS[:posted]


@requires_tools
@pytest.mark.parametrize(
    "count", ["0", "48", "100", "abc", "-3", "4.5", "1e1", " 5", "5 ", "040", "4;ls"]
)
def test_a_count_that_is_not_a_whole_number_from_one_to_47_stops_with_a_usage_line(
    tmp_path: Path, count: str
) -> None:
    done, calls = run_seed(tmp_path, count=count)

    assert done.returncode != 0
    assert "usage: make demo-seed" in done.stderr
    assert "COUNT" in done.stderr
    assert calls == []  # not even the edge's health check
    assert done.stdout == ""


@requires_tools
@pytest.mark.parametrize("pace", ["-1", "61", "x", "2.5", "100"])
def test_a_pace_that_is_not_a_whole_number_from_zero_to_60_stops_with_a_usage_line(
    tmp_path: Path, pace: str
) -> None:
    done, calls = run_seed(tmp_path, pace=pace)

    assert done.returncode != 0
    assert "usage: make demo-seed" in done.stderr
    assert "PACE_SECONDS" in done.stderr
    assert calls == []


@requires_tools
def test_an_empty_count_and_pace_mean_the_defaults_as_when_make_passes_them(
    tmp_path: Path,
) -> None:
    done, calls = run_seed(tmp_path, count="", pace="")

    assert done.returncode == 0, done.stdout + done.stderr
    assert len(post_ids(calls)) == 40
    assert [call for call in calls if call[0] == "SLEEP"] == [["SLEEP", "10"]] * 39


@requires_tools
def test_a_run_with_no_pace_sleeps_10_seconds_between_two_triaged_claims(
    tmp_path: Path,
) -> None:
    done, calls = run_seed(tmp_path, count="3", pace=None)

    assert done.returncode == 0, done.stdout + done.stderr
    assert [call for call in calls if call[0] == "SLEEP"] == [["SLEEP", "10"]] * 2
    assert "10s apart" in done.stdout


@requires_tools
def test_a_file_with_fewer_claims_than_count_stops_before_any_post(
    tmp_path: Path,
) -> None:
    done, calls = run_seed(tmp_path, count="5", claims=CLAIMS[:3])

    assert done.returncode != 0
    assert "3" in done.stderr
    assert post_ids(calls) == []


@requires_tools
def test_the_second_claim_is_posted_only_after_the_first_has_settled(
    tmp_path: Path,
) -> None:
    # The post answers `triaging` for CLM-0001 and the route says so twice more.
    done, calls = run_seed(
        tmp_path,
        count="3",
        states={"CLM-0001": "triaging"},
        readings={"CLM-0001": ["triaging", "triaging", "awaiting_adjuster"]},
    )

    assert done.returncode == 0, done.stdout + done.stderr
    assert reads_of(calls, "CLM-0001") == 3
    order = [(call[0], call[1]) for call in calls if call[0] in ("POST", "GET")]
    assert order[:5] == [
        ("POST", "CLM-0001"),
        ("GET", "CLM-0001"),
        ("GET", "CLM-0001"),
        ("GET", "CLM-0001"),
        ("POST", "CLM-0002"),
    ]
    assert claim_lines(done)[0] == ["CLM-0001", "posted", "awaiting_adjuster"]


@requires_tools
def test_no_post_comes_before_every_call_about_the_claim_before_it(
    tmp_path: Path,
) -> None:
    done, calls = run_seed(
        tmp_path,
        count="10",
        states={"CLM-0004": "triaging", "CLM-0007": "triaging"},
        readings={
            "CLM-0004": ["triaging", "approved"],
            "CLM-0007": ["triaging", "rejected"],
        },
    )

    assert done.returncode == 0, done.stdout + done.stderr
    traffic = [call for call in calls if call[0] in ("POST", "GET")]
    for number in range(1, 10):
        this, following = IDS[number - 1], IDS[number]
        last_about_this = max(
            index for index, call in enumerate(traffic) if call[1] == this
        )
        post_of_next = next(
            index
            for index, call in enumerate(traffic)
            if call[:2] == ["POST", following]
        )
        assert last_about_this < post_of_next, (this, following)


@requires_tools
def test_a_claim_whose_post_already_shows_its_state_is_not_polled(
    tmp_path: Path,
) -> None:
    done, calls = run_seed(tmp_path, count="2")

    assert done.returncode == 0, done.stdout + done.stderr
    assert [call for call in calls if call[0] == "GET"] == []


@requires_tools
def test_the_pause_comes_between_two_posted_claims_and_not_after_the_last(
    tmp_path: Path,
) -> None:
    done, calls = run_seed(tmp_path, count="3", pace="5")

    assert done.returncode == 0, done.stdout + done.stderr
    assert [call[0] for call in calls] == [
        "POST",
        "SLEEP",
        "POST",
        "SLEEP",
        "POST",
    ]
    assert [call[1] for call in calls if call[0] == "SLEEP"] == ["5", "5"]


# ── what it prints ───────────────────────────────────────────────────────────


@requires_tools
def test_each_claim_has_one_line_and_the_counts_by_final_state_follow(
    tmp_path: Path,
) -> None:
    states = {
        "CLM-0001": "approved",
        "CLM-0002": "rejected",
        "CLM-0003": "documents_requested",
        "CLM-0004": "withdrawn",
        "CLM-0005": "a_new_state",
    }
    done, _ = run_seed(tmp_path, count="7", states=states)

    assert done.returncode == 0, done.stdout + done.stderr
    assert claim_lines(done) == [
        ["CLM-0001", "posted", "approved"],
        ["CLM-0002", "posted", "rejected"],
        ["CLM-0003", "posted", "documents_requested"],
        ["CLM-0004", "posted", "withdrawn"],
        ["CLM-0005", "posted", "a_new_state"],
        ["CLM-0006", "posted", "awaiting_adjuster"],
        ["CLM-0007", "posted", "awaiting_adjuster"],
    ]
    assert summary(done) == {
        "referred to an adjuster": 2,
        "approved": 1,
        "rejected": 1,
        "awaiting documents": 1,
        "withdrawn": 1,
        "another state": 1,
    }
    assert "/adjuster/claims" in done.stdout
    assert "/claimant/claims" in done.stdout


@requires_tools
def test_it_decides_nothing_a_referred_claim_gets_no_decision_post(
    tmp_path: Path,
) -> None:
    done, calls = run_seed(tmp_path, count="5")

    assert done.returncode == 0, done.stdout + done.stderr
    assert all(call[3].endswith("/claims") for call in posts_of(calls))
    assert summary(done) == {"referred to an adjuster": 5}


@requires_tools
def test_no_claim_field_but_the_id_and_the_state_reaches_the_output(
    tmp_path: Path,
) -> None:
    # The claimant's name is in every claim posted and in what the stand-in
    # services answer (the 201 body and the route's), and a refusal's detail
    # that is not a sentence carries it too.
    refusal = json.dumps({"detail": [{"input": MARKER, "msg": MARKER}]})
    done, _ = run_seed(
        tmp_path,
        count="6",
        posts={"CLM-0005": (422, refusal)},
    )

    assert done.returncode != 0  # the refusal
    assert MARKER not in done.stdout
    assert MARKER not in done.stderr
    assert "HTTP 422" in done.stderr
    for words in claim_lines(done):
        assert len(words) == 3, words  # id, what happened, state


@requires_tools
def test_what_the_api_answers_is_cleaned_of_anything_that_is_not_printable(
    tmp_path: Path,
) -> None:
    hostile = json.dumps({"detail": "bad\x1b[31m\nINJECTED"})
    done, _ = run_seed(tmp_path, count="3", posts={"CLM-0002": (409, hostile)})

    assert done.returncode != 0
    assert "\x1b" not in done.stdout + done.stderr
    assert "\nINJECTED" not in done.stderr


# ── safe to run twice, and the conflicts ─────────────────────────────────────


@requires_tools
def test_a_second_run_skips_every_claim_that_exists_and_pauses_for_none(
    tmp_path: Path,
) -> None:
    first, first_calls = run_seed(tmp_path, count="6", pace="2")
    second, calls = run_seed(tmp_path, count="6", pace="2")

    assert first.returncode == 0 and second.returncode == 0, (
        second.stdout + second.stderr
    )
    second_calls = calls[len(first_calls) :]
    assert post_ids(second_calls) == IDS[:6]  # asked, and told it is there
    assert [call for call in second_calls if call[0] == "SLEEP"] == []
    assert all(words[1] == "skipped" for words in claim_lines(second))
    # A skipped claim's line still shows its state, read from the route once.
    assert claim_lines(second)[0] == ["CLM-0001", "skipped", "awaiting_adjuster"]
    assert reads_of(second_calls, "CLM-0001") == 1
    assert summary(second) == {"skipped (already there, same content)": 6}


@requires_tools
def test_a_claim_with_other_content_under_its_id_is_counted_apart_and_named(
    tmp_path: Path,
) -> None:
    other = json.dumps({"detail": DIFFERENT_SUBMISSION_DETAIL})
    done, calls = run_seed(tmp_path, count="4", posts={"CLM-0002": (409, other)})

    assert done.returncode == 0, done.stdout + done.stderr
    assert post_ids(calls) == IDS[:4]  # it goes on
    assert ["CLM-0002", "different", "(unread)"] in claim_lines(done)
    assert summary(done)["exists with other content"] == 1
    assert summary(done)["referred to an adjuster"] == 3
    assert "other content: CLM-0002" in done.stdout


@requires_tools
def test_a_claim_that_is_being_triaged_by_another_request_is_waited_for(
    tmp_path: Path,
) -> None:
    being = json.dumps({"detail": BEING_TRIAGED_DETAIL})
    done, calls = run_seed(
        tmp_path,
        count="2",
        posts={"CLM-0001": (409, being)},
        readings={"CLM-0001": ["triaging", "approved"]},
    )

    assert done.returncode == 0, done.stdout + done.stderr
    assert ["CLM-0001", "waited", "approved"] in claim_lines(done)
    assert reads_of(calls, "CLM-0001") == 2
    assert post_ids(calls) == ["CLM-0001", "CLM-0002"]


@requires_tools
def test_a_claim_at_the_triage_cap_is_counted_apart_and_the_run_goes_on(
    tmp_path: Path,
) -> None:
    capped = json.dumps({"detail": TRIAGE_CAP_DETAIL})
    done, calls = run_seed(
        tmp_path,
        count="3",
        posts={"CLM-0002": (409, capped)},
        readings={"CLM-0002": ["triage_failed"]},
    )

    assert done.returncode == 0, done.stdout + done.stderr
    assert post_ids(calls) == IDS[:3]
    assert ["CLM-0002", "at-cap", "triage_failed"] in claim_lines(done)
    assert summary(done)["at the triage cap (an adjuster decides it)"] == 1


# ── a failed triage, a refusal, a claim that never settles ───────────────────


@requires_tools
def test_a_failed_triage_is_counted_and_shown_and_the_run_and_status_go_on(
    tmp_path: Path,
) -> None:
    failed = json.dumps({"detail": "the triage did not finish", "claim_id": "CLM-0002"})
    done, calls = run_seed(
        tmp_path,
        count="4",
        posts={"CLM-0002": (502, failed)},
        readings={"CLM-0002": ["triage_failed"]},
    )

    assert done.returncode == 0, done.stdout + done.stderr
    assert post_ids(calls) == IDS[:4]
    assert ["CLM-0002", "posted", "triage_failed"] in claim_lines(done)
    assert summary(done)["triage failed"] == 1
    assert summary(done)["referred to an adjuster"] == 3
    # It says what to do about it, and does not when nothing failed.
    assert "triaged again when this is run again" in done.stdout
    assert "PACE_SECONDS" in done.stdout
    # It names both windows of the gateway's tenant and the default pause.
    assert "tenant-request-rate (10 requests in 10 seconds)" in done.stdout
    assert "tenant-token-rate (10,000 tokens a minute)" in done.stdout
    assert "default pause of 10 seconds" in done.stdout
    assert MARKER not in done.stdout
    clean, _ = run_seed(tmp_path / "clean", count="2")
    assert "triaged again" not in clean.stdout


@requires_tools
def test_a_post_the_edge_answered_for_a_claim_that_is_not_stored_is_a_refusal(
    tmp_path: Path,
) -> None:
    # A 503 with no claim behind it (the route answers 404): not a failed
    # triage, and the run stops rather than repeating it forty times.
    unavailable = json.dumps({"detail": "the upstream is unavailable"})
    done, calls = run_seed(tmp_path, count="5", posts={"CLM-0002": (503, unavailable)})

    assert done.returncode == 1
    assert post_ids(calls) == ["CLM-0001", "CLM-0002"]
    # The post's own sentence is the one printed, not the route's "no such claim".
    # The post's own answer is the one named (an answer the script does not
    # know, so not its sentence), not the route's "no such claim".
    assert (
        "CLM-0002: HTTP 503 (an answer this script does not know), "
        "and the claim is not stored" in done.stderr
    )
    assert "upstream is unavailable" not in done.stderr
    assert "no such claim" not in done.stderr
    assert summary(done)["referred to an adjuster"] == 1  # what happened so far


@requires_tools
def test_a_refusal_for_another_reason_stops_the_run_with_a_non_zero_status(
    tmp_path: Path,
) -> None:
    other = json.dumps({"detail": "something else is wrong"})
    done, calls = run_seed(tmp_path, count="5", posts={"CLM-0003": (409, other)})

    assert done.returncode == 1
    assert post_ids(calls) == IDS[:3]
    assert "CLM-0003: HTTP 409 (an answer this script does not know)" in done.stderr
    assert "something else is wrong" not in done.stderr
    assert summary(done)["referred to an adjuster"] == 2


@requires_tools
def test_a_claim_that_never_settles_is_counted_after_the_bound_and_the_run_goes_on(
    tmp_path: Path,
) -> None:
    done, calls = run_seed(
        tmp_path,
        count="3",
        states={"CLM-0002": "triaging"},
        readings={"CLM-0002": ["triaging"]},
    )

    assert done.returncode == 0, done.stdout + done.stderr
    # Readings at 0, 10 ... 100 seconds of the script's own clock: the bound. No
    # exact count: the clock also moves with real time, as demo's tests say.
    assert 2 <= reads_of(calls, "CLM-0002") <= MAX_READINGS
    assert post_ids(calls) == IDS[:3]
    assert ["CLM-0002", "not-settled", "triaging"] in claim_lines(done)
    assert summary(done)["not settled"] == 1
    assert f"{SETTLE_TIMEOUT_SECONDS}s" in done.stdout


@requires_tools
def test_a_claim_whose_state_cannot_be_read_is_not_settled_with_no_state(
    tmp_path: Path,
) -> None:
    # The post is answered with no state we trust, and the route has the claim
    # but never says anything the script accepts.
    unreadable = json.dumps({"detail": "x", "claim_id": "CLM-0001"})
    done, _ = run_seed(
        tmp_path,
        count="2",
        posts={"CLM-0001": (502, unreadable)},
        readings={"CLM-0001": ["Not A State!"]},
    )

    assert done.returncode == 0, done.stdout + done.stderr
    assert ["CLM-0001", "not-settled", "(unread)"] in claim_lines(done)
    assert "Not A State" not in done.stdout


@requires_tools
def test_exit_is_non_zero_when_no_claim_could_be_posted_and_zero_when_all_exist(
    tmp_path: Path,
) -> None:
    other = json.dumps({"detail": DIFFERENT_SUBMISSION_DETAIL})
    posts = {claim_id: (409, other) for claim_id in IDS[:3]}
    nothing, _ = run_seed(tmp_path / "a", count="3", posts=posts)
    again_first, _ = run_seed(tmp_path / "b", count="3")
    again, _ = run_seed(tmp_path / "b", count="3")

    assert nothing.returncode == 1
    assert "no claim could be posted" in nothing.stderr
    assert again_first.returncode == 0
    assert again.returncode == 0  # every claim skipped is not a failure


# ── the refusals before anything is posted ───────────────────────────────────
